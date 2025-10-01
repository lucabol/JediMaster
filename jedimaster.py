#!/usr/bin/env python3
"""
JediMaster - A tool to automatically assign GitHub issues to GitHub Copilot
based on LLM evaluation of issue suitability.
"""

import os
import json
import logging
from collections import Counter
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, asdict, field
from datetime import datetime
import argparse
import requests

from github import Github, GithubException
from dotenv import load_dotenv


from decider import DeciderAgent, PRDeciderAgent
from creator import CreatorAgent
from reporting import format_table







@dataclass
class IssueResult:
    """Represents the result of processing a single issue."""
    repo: str
    issue_number: int
    title: str
    url: str
    status: str  # 'assigned', 'not_assigned', 'already_assigned', 'labeled', 'error'
    reasoning: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class PRRunResult:
    """Represents the result of processing or merging a pull request."""
    repo: str
    pr_number: int
    title: str
    status: str
    details: Optional[str] = None
    attempts: Optional[int] = None


@dataclass
class ProcessingReport:
    """Summary report of the entire processing run."""
    total_issues: int = 0
    assigned: int = 0
    not_assigned: int = 0
    already_assigned: int = 0
    labeled: int = 0
    errors: int = 0
    results: List[IssueResult] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    pr_results: List[PRRunResult] = field(default_factory=list)



# Main class for processing GitHub issues and PRs for Copilot
class JediMaster:
    def _get_issue_id_and_bot_id(self, repo_owner: str, repo_name: str, issue_number: int) -> tuple[Optional[str], Optional[str]]:
        """Get the GitHub node ID for an issue and find the Copilot bot using suggestedActors."""
        query = """
        query($owner: String!, $name: String!, $issueNumber: Int!) {
          repository(owner: $owner, name: $name) {
            issue(number: $issueNumber) {
              id
            }
            suggestedActors(capabilities: [CAN_BE_ASSIGNED], first: 100) {
              nodes {
                login
                __typename
                ... on Bot {
                  id
                }
                ... on User {
                  id
                }
              }
            }
          }
        }
        """
        variables = {
            "owner": repo_owner,
            "name": repo_name,
            "issueNumber": issue_number
        }
        try:
            result = self._graphql_request(query, variables)
            if "errors" in result:
                self.logger.error(f"GraphQL errors: {result['errors']}")
                return None, None
            data = result["data"]
            issue_id = data["repository"]["issue"]["id"]
            bot_id = None
            suggested_actors = data["repository"]["suggestedActors"]["nodes"]
            for actor in suggested_actors:
                login = actor["login"]
                if login == "copilot-swe-agent" or "copilot" in login.lower():
                    bot_id = actor["id"]
                    self.logger.info(f"Found Copilot actor: {login} (type: {actor.get('__typename', 'Unknown')})")
                    break
            if not bot_id:
                self.logger.warning(f"No Copilot coding agent found in suggested actors for {repo_owner}/{repo_name}")
                if suggested_actors:
                    actor_logins = [actor["login"] for actor in suggested_actors]
                    self.logger.info(f"Available suggested actors: {actor_logins}")
                else:
                    self.logger.info("No suggested actors found - Copilot may not be enabled for this repository")
            return issue_id, bot_id
        except Exception as e:
            self.logger.error(f"Error getting issue and bot IDs: {e}")
            return None, None

    def _assign_issue_via_graphql(self, issue_id: str, bot_id: str) -> bool:
        """Assign an issue to a bot using GraphQL mutation."""
        mutation = """
        mutation($assignableId: ID!, $actorIds: [ID!]!) {
          replaceActorsForAssignable(input: {assignableId: $assignableId, actorIds: $actorIds}) {
            assignable {
              ... on Issue {
                id
                title
                assignees(first: 10) {
                  nodes {
                    login
                  }
                }
              }
            }
          }
        }
        """
        variables = {
            "assignableId": issue_id,
            "actorIds": [bot_id]
        }
        try:
            result = self._graphql_request(mutation, variables)
            if "errors" in result:
                self.logger.error(f"GraphQL mutation errors: {result['errors']}")
                return False
            assignees = result["data"]["replaceActorsForAssignable"]["assignable"]["assignees"]["nodes"]
            assigned_logins = [assignee["login"] for assignee in assignees]
            self.logger.info(f"Successfully assigned issue. Current assignees: {assigned_logins}")
            return True
        except Exception as e:
            self.logger.error(f"Error assigning issue via GraphQL: {e}")
            return False

    def _get_pr_id_and_bot_id(self, repo_owner: str, repo_name: str, pr_number: int) -> tuple[Optional[str], Optional[str]]:
        """Get the GitHub node ID for a PR and find the Copilot bot using suggestedActors."""
        query = """
        query($owner: String!, $name: String!, $prNumber: Int!) {
          repository(owner: $owner, name: $name) {
            pullRequest(number: $prNumber) {
              id
            }
            suggestedActors(capabilities: [CAN_BE_ASSIGNED], first: 100) {
              nodes {
                login
                __typename
                ... on Bot {
                  id
                }
                ... on User {
                  id
                }
              }
            }
          }
        }
        """
        variables = {
            "owner": repo_owner,
            "name": repo_name,
            "prNumber": pr_number
        }
        try:
            result = self._graphql_request(query, variables)
            if "errors" in result:
                self.logger.error(f"GraphQL errors: {result['errors']}")
                return None, None
            data = result["data"]
            pr_id = data["repository"]["pullRequest"]["id"]
            bot_id = None
            suggested_actors = data["repository"]["suggestedActors"]["nodes"]
            for actor in suggested_actors:
                login = actor["login"]
                if login == "copilot-swe-agent" or "copilot" in login.lower():
                    bot_id = actor["id"]
                    self.logger.info(f"Found Copilot actor: {login} (type: {actor.get('__typename', 'Unknown')})")
                    break
            if not bot_id:
                self.logger.warning(f"No Copilot coding agent found in suggested actors for {repo_owner}/{repo_name}")
                if suggested_actors:
                    actor_logins = [actor["login"] for actor in suggested_actors]
                    self.logger.info(f"Available suggested actors: {actor_logins}")
                else:
                    self.logger.info("No suggested actors found - Copilot may not be enabled for this repository")
            return pr_id, bot_id
        except Exception as e:
            self.logger.error(f"Error getting PR and bot IDs: {e}")
            return None, None

    def _assign_pr_via_graphql(self, pr_id: str, bot_id: str) -> bool:
        """Assign a PR to a bot using GraphQL mutation."""
        mutation = """
        mutation($assignableId: ID!, $actorIds: [ID!]!) {
          replaceActorsForAssignable(input: {assignableId: $assignableId, actorIds: $actorIds}) {
            assignable {
              ... on PullRequest {
                id
                title
                assignees(first: 10) {
                  nodes {
                    login
                  }
                }
              }
            }
          }
        }
        """
        variables = {
            "assignableId": pr_id,
            "actorIds": [bot_id]
        }
        try:
            result = self._graphql_request(mutation, variables)
            if "errors" in result:
                self.logger.error(f"GraphQL mutation errors: {result['errors']}")
                return False
            assignees = result["data"]["replaceActorsForAssignable"]["assignable"]["assignees"]["nodes"]
            assigned_logins = [assignee["login"] for assignee in assignees]
            self.logger.info(f"Successfully assigned PR. Current assignees: {assigned_logins}")
            return True
        except Exception as e:
            self.logger.error(f"Error assigning PR via GraphQL: {e}")
            return False

    def _is_copilot_already_assigned_to_pr(self, pr) -> bool:
        """Check if Copilot is already assigned to this PR."""
        try:
            return any('copilot' in (assignee.login or '').lower() for assignee in pr.assignees)
        except Exception as e:
            self.logger.error(f"Error checking PR assignees for PR #{getattr(pr, 'number', '?')}: {e}")
            return False

    def _close_linked_issues(self, repo, pr_number: int, pr_title: str) -> List[int]:
        """Find and close issues linked to a PR using GraphQL, returning list of closed issue numbers."""
        closed_issues = []
        
        try:
            # Use GraphQL to get the PR's closing issue references
            repo_parts = repo.full_name.split('/')
            query = """
            query($owner: String!, $name: String!, $number: Int!) {
              repository(owner: $owner, name: $name) {
                pullRequest(number: $number) {
                  id
                  url
                  closingIssuesReferences(first: 50) {
                    edges {
                      node {
                        id
                        body
                        number
                        title
                        state
                      }
                    }
                  }
                }
              }
            }
            """
            
            variables = {
                "owner": repo_parts[0],
                "name": repo_parts[1],
                "number": pr_number
            }
            
            result = self._graphql_request(query, variables)
            if "errors" in result:
                self.logger.error(f"GraphQL errors getting closing issues: {result['errors']}")
                return closed_issues
            
            pr_data = result["data"]["repository"]["pullRequest"]
            pr_url = pr_data["url"]
            closing_issues = pr_data["closingIssuesReferences"]["edges"]
            
            self.logger.info(f"Found {len(closing_issues)} closing issue references for PR #{pr_number}")
            
            # Close each linked issue
            for edge in closing_issues:
                issue_data = edge["node"]
                issue_number = issue_data["number"]
                issue_state = issue_data["state"]
                
                try:
                    if issue_state == 'OPEN':
                        issue = repo.get_issue(issue_number)
                        
                        # Add a comment before closing
                        close_comment = f"Closed by PR #{pr_number}: {pr_url}"
                        issue.create_comment(close_comment)
                        
                        # Close the issue
                        issue.edit(state='closed')
                        
                        self.logger.info(f"Closed issue #{issue_number} linked to PR #{pr_number}")
                        closed_issues.append(issue_number)
                    else:
                        self.logger.info(f"Issue #{issue_number} linked to PR #{pr_number} was already closed")
                        
                except Exception as e:
                    self.logger.error(f"Failed to close linked issue #{issue_number} for PR #{pr_number}: {e}")
            
            if closed_issues:
                self.logger.info(f"Successfully closed {len(closed_issues)} issues linked to PR #{pr_number}: {closed_issues}")
            else:
                self.logger.debug(f"No open linked issues found for PR #{pr_number}")
                
        except Exception as e:
            self.logger.error(f"Error processing linked issues for PR #{pr_number}: {e}")
        
        return closed_issues
    
    def _delete_pr_branch(self, pr) -> bool:
        """Delete the branch associated with a pull request after successful merge."""
        try:
            head_repo = pr.head.repo
            head_branch_name = pr.head.ref
            base_repo = pr.base.repo
            
            # Only delete the branch if it's from the same repository (not a fork)
            if head_repo.full_name != base_repo.full_name:
                self.logger.info(f"PR #{pr.number} is from a fork ({head_repo.full_name}), skipping branch deletion")
                return False
            
            # Don't delete protected branches (main, master, develop, etc.)
            protected_branches = ['main', 'master', 'develop', 'development', 'staging', 'production']
            if head_branch_name.lower() in protected_branches:
                self.logger.info(f"PR #{pr.number} branch '{head_branch_name}' is a protected branch, skipping deletion")
                return False
            
            # Delete the branch
            git_ref = head_repo.get_git_ref(f"heads/{head_branch_name}")
            git_ref.delete()
            
            self.logger.info(f"Successfully deleted branch '{head_branch_name}' for PR #{pr.number}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error deleting branch for PR #{pr.number}: {e}")
            return False
    
    def _repo_has_topic(self, repo, topic: str) -> bool:
        """Check if a repository has a specific topic."""
        try:
            topics = repo.get_topics()
            return topic in topics
        except Exception as e:
            self.logger.warning(f"Could not fetch topics for {repo.full_name}: {e}")
            return False

    def _file_exists_in_repo(self, repo, filename: str) -> bool:
        """Check if a file exists in the root of the repository."""
        try:
            repo.get_contents(filename)
            return True
        except Exception:
            return False

    def _shorten_text(self, text: Optional[str], limit: int = 80) -> str:
        if not text:
            return ""
        cleaned = " ".join(text.strip().split())
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: limit - 1] + "…"

    def _friendly_issue_status(self, status: str) -> str:
        mapping = {
            'assigned': 'assigned ✅',
            'labeled': 'labeled 🏷️',
            'not_assigned': 'not assigned',
            'already_assigned': 'already assigned 🔁',
            'error': 'error ⚠️',
        }
        return mapping.get(status, status.replace('_', ' '))

    def _friendly_pr_status(self, status: str) -> str:
        mapping = {
            'approved': 'approved ✅',
            'changes_requested': 'changes requested ✏️',
            'skipped': 'skipped',
            'error': 'error ⚠️',
            'unknown': 'unknown',
            'merged': 'merged ✅',
            'merge_error': 'merge error ⚠️',
            'max_retries_exceeded': 'max retries 🚫',
            'state_changed': 'state changed',
        }
        return mapping.get(status, status.replace('_', ' '))

    def fetch_issues(self, repo_name: str, batch_size: int = 15):
        """Fetch open issues that haven't been processed yet.
        
        Args:
            repo_name: The repository name in format 'owner/repo'
            batch_size: Maximum number of unprocessed issues to return (default 15)
            
        Returns:
            List of unprocessed issues (limited by batch_size)
        """
        repo = self.github.get_repo(repo_name)
        all_issues = repo.get_issues(state='open')
        
        unprocessed_issues = []
        processed_labels = {'copilot-candidate', 'no-github-copilot'}
        
        for issue in all_issues:
            # Skip pull requests
            if issue.pull_request:
                continue
                
            # Check if already processed (has our labels)
            issue_label_names = {label.name.lower() for label in issue.labels}
            if issue_label_names.intersection(processed_labels):
                continue  # Skip already processed issues
                
            unprocessed_issues.append(issue)
            
            # Stop when we have enough for this batch
            if len(unprocessed_issues) >= batch_size:
                break
                
        self.logger.info(f"Found {len(unprocessed_issues)} unprocessed issues (batch size: {batch_size})")
        return unprocessed_issues

    def process_issue(self, issue, repo_name: str) -> IssueResult:
        """Process a single issue and return an IssueResult."""
        try:
            # Skip if already assigned to Copilot
            if any('copilot' in (assignee.login or '').lower() for assignee in issue.assignees):
                return IssueResult(
                    repo=repo_name,
                    issue_number=issue.number,
                    title=issue.title,
                    url=issue.html_url,
                    status='already_assigned',
                    reasoning="Already assigned to Copilot."
                )
            # Evaluate with DeciderAgent
            result = self.decider.evaluate_issue({'title': issue.title, 'body': issue.body or ''})
            if result.get('decision', '').lower() == 'yes':
                if not self.just_label:
                    try:
                        repo = issue.repository
                        repo_full_name = repo.full_name.split('/')
                        repo_owner = repo_full_name[0]
                        repo_name_only = repo_full_name[1]
                        issue_id, bot_id = self._get_issue_id_and_bot_id(repo_owner, repo_name_only, issue.number)
                        if issue_id and bot_id:
                            success = self._assign_issue_via_graphql(issue_id, bot_id)
                            if success:
                                status = 'assigned'
                            else:
                                self.logger.warning(f"GraphQL assignment failed for issue #{issue.number}")
                                status = 'labeled'
                        else:
                            self.logger.warning(f"Could not find issue ID or suitable bot for issue #{issue.number}")
                            status = 'labeled'
                    except Exception as e:
                        self.logger.warning(f"Failed to assign Copilot to issue #{issue.number}: {e}")
                        status = 'labeled'
                else:
                    status = 'labeled'
                # Add label
                try:
                    issue.add_to_labels('copilot-candidate')
                except Exception as e:
                    self.logger.warning(f"Failed to add label to issue #{issue.number}: {e}")
                return IssueResult(
                    repo=repo_name,
                    issue_number=issue.number,
                    title=issue.title,
                    url=issue.html_url,
                    status=status,
                    reasoning=result.get('reasoning')
                )
            else:
                # Add 'no-github-copilot' label if not suitable
                try:
                    repo = issue.repository
                    no_copilot_label = None
                    for label in repo.get_labels():
                        if label.name.lower() == 'no-github-copilot':
                            no_copilot_label = label
                            break
                    if not no_copilot_label:
                        no_copilot_label = repo.create_label(
                            name="no-github-copilot",
                            color="ededed",
                            description="Issue not suitable for GitHub Copilot"
                        )
                    issue.add_to_labels(no_copilot_label)
                    self.logger.info(f"Added 'no-github-copilot' label to issue #{issue.number}")
                except Exception as e:
                    self.logger.error(f"Could not add 'no-github-copilot' label to issue #{issue.number}: {e}")
                    return IssueResult(
                        repo=repo_name,
                        issue_number=issue.number,
                        title=issue.title,
                        url=issue.html_url,
                        status='error',
                        reasoning=result.get('reasoning'),
                        error_message=f"Failed to add 'no-github-copilot' label: {e}"
                    )
                return IssueResult(
                    repo=repo_name,
                    issue_number=issue.number,
                    title=issue.title,
                    url=issue.html_url,
                    status='not_assigned',
                    reasoning=result.get('reasoning')
                )
        except Exception as e:
            self.logger.error(f"Error processing issue #{getattr(issue, 'number', '?')}: {e}")
            return IssueResult(
                repo=repo_name,
                issue_number=getattr(issue, 'number', 0),
                title=getattr(issue, 'title', 'Unknown'),
                url=getattr(issue, 'html_url', ''),
                status='error',
                error_message=str(e)
            )
    def __init__(self, github_token: str, azure_foundry_endpoint: str, just_label: bool = False, use_topic_filter: bool = True, process_prs: bool = False, auto_merge_reviewed: bool = False):
        self.github_token = github_token
        self.azure_foundry_endpoint = azure_foundry_endpoint
        self.github = Github(github_token)
        self.decider = DeciderAgent(azure_foundry_endpoint)
        self.pr_decider = PRDeciderAgent(azure_foundry_endpoint)
        self.just_label = just_label
        self.use_topic_filter = use_topic_filter
        self.process_prs = process_prs
        self.auto_merge_reviewed = auto_merge_reviewed
        self.logger = self._setup_logger()
        # Get merge retry limit from environment
        self.merge_max_retries = self._get_merge_max_retries()

    def _get_merge_max_retries(self) -> int:
        """Get the maximum number of merge retry attempts from environment variable."""
        try:
            max_retries = int(os.getenv('MERGE_MAX_RETRIES', '3'))
            if max_retries < 1:
                self.logger.warning(f"MERGE_MAX_RETRIES must be >= 1, using default of 3")
                return 3
            return max_retries
        except ValueError:
            self.logger.warning(f"Invalid MERGE_MAX_RETRIES value, using default of 3")
            return 3

    def _get_merge_attempt_count(self, pr) -> int:
        """Get the current merge attempt count from PR labels."""
        try:
            labels = [label.name for label in pr.labels]
            for label in labels:
                if label.startswith('merge-attempt-'):
                    try:
                        return int(label.split('-')[-1])
                    except ValueError:
                        continue
            return 0
        except Exception as e:
            self.logger.error(f"Error getting merge attempt count for PR #{pr.number}: {e}")
            return 0

    def _increment_merge_attempt_count(self, pr) -> int:
        """Increment the merge attempt counter and return the new count."""
        try:
            current_count = self._get_merge_attempt_count(pr)
            new_count = current_count + 1
            
            # Remove old attempt label if it exists
            if current_count > 0:
                old_label_name = f'merge-attempt-{current_count}'
                try:
                    pr.remove_from_labels(old_label_name)
                except Exception as e:
                    self.logger.debug(f"Could not remove old label {old_label_name}: {e}")
            
            # Add new attempt label
            new_label_name = f'merge-attempt-{new_count}'
            
            # Create label if it doesn't exist
            try:
                repo = pr.repository if hasattr(pr, 'repository') else pr.base.repo
                try:
                    repo.get_label(new_label_name)
                except:
                    repo.create_label(
                        name=new_label_name,
                        color="ff9500",
                        description=f"This PR has had {new_count} merge attempt(s)"
                    )
                
                pr.add_to_labels(new_label_name)
                self.logger.info(f"Incremented merge attempt count to {new_count} for PR #{pr.number}")
                
            except Exception as e:
                self.logger.error(f"Failed to add merge attempt label to PR #{pr.number}: {e}")
            
            return new_count
        except Exception as e:
            self.logger.error(f"Error incrementing merge attempt count for PR #{pr.number}: {e}")
            return 1  # Default to 1 if we can't track properly

    def _mark_pr_max_retries_exceeded(self, pr):
        """Mark a PR as having exceeded maximum merge retry attempts."""
        try:
            repo = pr.repository if hasattr(pr, 'repository') else pr.base.repo
            
            # Create the max retries exceeded label if it doesn't exist
            max_retries_label_name = "merge-failed-max-retries"
            try:
                max_retries_label = repo.get_label(max_retries_label_name)
            except:
                max_retries_label = repo.create_label(
                    name=max_retries_label_name,
                    color="d73a49",
                    description=f"Merge failed after {self.merge_max_retries} attempts - manual intervention required"
                )
            
            pr.add_to_labels(max_retries_label)
            
            # Add a comment explaining the situation
            comment_body = f"""⚠️ **Auto-merge failed after {self.merge_max_retries} attempts**

This PR has exceeded the maximum number of automatic merge retry attempts. Please review and merge manually if appropriate.

Possible reasons for merge failures:
- Merge conflicts that need manual resolution  
- Branch protection rules blocking the merge
- Required status checks not passing
- Permission issues

The auto-merge system will no longer attempt to merge this PR automatically."""
            
            pr.create_issue_comment(comment_body)
            
            self.logger.warning(f"PR #{pr.number} marked as max retries exceeded after {self.merge_max_retries} attempts")
            
        except Exception as e:
            self.logger.error(f"Failed to mark PR #{pr.number} as max retries exceeded: {e}")

    def merge_reviewed_pull_requests(self, repo_name: str, batch_size: int = 10):
        """Merge PRs that are approved and have no conflicts with the base branch. If PR is a draft, mark as ready for review first.
        
        Args:
            repo_name: The repository name in format 'owner/repo'
            batch_size: Maximum number of PRs to process (default 10)
        """
        results: List[PRRunResult] = []
        try:
            repo = self.github.get_repo(repo_name)
            all_pulls = list(repo.get_pulls(state='open'))
            self.logger.info(f"Found {len(all_pulls)} open PRs in {repo_name}")
            
            # Limit to batch size
            pulls = all_pulls[:batch_size]
            if len(all_pulls) > batch_size:
                self.logger.info(f"Processing first {batch_size} PRs (batch size limit)")
            
            for pr in pulls:
                # Check if PR has already exceeded max retry attempts
                if any(label.name == "merge-failed-max-retries" for label in pr.labels):
                    self.logger.info(f"PR #{pr.number} in {repo_name} has exceeded max merge retries, skipping")
                    results.append(
                        PRRunResult(
                            repo=repo_name,
                            pr_number=pr.number,
                            title=pr.title,
                            status='max_retries_exceeded',
                            details='Already flagged as max retries',
                        )
                    )
                    continue
                
                # Check current attempt count
                current_attempts = self._get_merge_attempt_count(pr)
                if current_attempts >= self.merge_max_retries:
                    self.logger.warning(f"PR #{pr.number} in {repo_name} has reached max merge attempts ({current_attempts}), marking as failed")
                    self._mark_pr_max_retries_exceeded(pr)
                    results.append(
                        PRRunResult(
                            repo=repo_name,
                            pr_number=pr.number,
                            title=pr.title,
                            status='max_retries_exceeded',
                            attempts=current_attempts,
                            details='Exceeded retry budget',
                        )
                    )
                    continue
                
                self.logger.info(f"Checking PR #{pr.number}: '{pr.title}' (draft: {getattr(pr, 'draft', 'unknown')}, mergeable: {pr.mergeable}, attempts: {current_attempts})")
                
                # Check if PR is approved (reviewed)
                reviews = list(pr.get_reviews())
                approved = any(r.state == 'APPROVED' for r in reviews)
                if not approved:
                    self.logger.info(f"PR #{pr.number} in {repo_name} is not approved, skipping")
                    continue
                
                # Check if the current user (bot token owner) is one of the reviewers
                # This can cause issues in some GitHub configurations
                try:
                    current_user = self.github.get_user()
                    current_username = current_user.login
                    reviewer_usernames = [r.user.login for r in reviews if r.user and r.state == 'APPROVED']
                    
                    if current_username in reviewer_usernames:
                        self.logger.warning(f"PR #{pr.number} was approved by the same user ({current_username}) attempting to merge. This may cause permission issues.")
                        # Continue anyway, but log the potential issue
                    
                    self.logger.info(f"PR #{pr.number} approved by: {reviewer_usernames}, merging as: {current_username}")
                except Exception as e:
                    self.logger.warning(f"Could not determine current user for PR #{pr.number}: {e}")
                    # Continue anyway
                
                # Refresh PR data to get latest state
                pr.update()
                
                # If PR is a draft, mark as ready for review first
                if getattr(pr, 'draft', False):
                    self.logger.info(f"PR #{pr.number} is a draft, marking as ready for review")
                    try:
                        # Try using GraphQL API to mark as ready for review
                        mutation = """
                        mutation($pullRequestId: ID!) {
                          markPullRequestReadyForReview(input: {pullRequestId: $pullRequestId}) {
                            pullRequest {
                              id
                              isDraft
                              number
                            }
                          }
                        }
                        """
                        
                        # First get the PR's node ID
                        pr_query = """
                        query($owner: String!, $name: String!, $number: Int!) {
                          repository(owner: $owner, name: $name) {
                            pullRequest(number: $number) {
                              id
                              isDraft
                            }
                          }
                        }
                        """
                        
                        repo_parts = repo_name.split('/')
                        query_vars = {
                            "owner": repo_parts[0],
                            "name": repo_parts[1], 
                            "number": pr.number
                        }
                        
                        query_result = self._graphql_request(pr_query, query_vars)
                        if "errors" in query_result:
                            self.logger.error(f"GraphQL query errors: {query_result['errors']}")
                            results.append(
                                PRRunResult(
                                    repo=repo_name,
                                    pr_number=pr.number,
                                    title=pr.title,
                                    status='merge_error',
                                    details=f"GraphQL query failed: {self._shorten_text(str(query_result['errors']))}",
                                )
                            )
                            continue
                            
                        pr_node_id = query_result["data"]["repository"]["pullRequest"]["id"]
                        is_draft = query_result["data"]["repository"]["pullRequest"]["isDraft"]
                        
                        self.logger.info(f"PR #{pr.number} node ID: {pr_node_id}, isDraft: {is_draft}")
                        
                        if is_draft:
                            # Use the mutation to mark as ready
                            mutation_vars = {"pullRequestId": pr_node_id}
                            mutation_result = self._graphql_request(mutation, mutation_vars)
                            
                            if "errors" in mutation_result:
                                self.logger.error(f"GraphQL mutation errors: {mutation_result['errors']}")
                                results.append(
                                    PRRunResult(
                                        repo=repo_name,
                                        pr_number=pr.number,
                                        title=pr.title,
                                        status='merge_error',
                                        details=f"GraphQL mutation failed: {self._shorten_text(str(mutation_result['errors']))}",
                                    )
                                )
                                continue
                            
                            # Check the result
                            updated_draft_status = mutation_result["data"]["markPullRequestReadyForReview"]["pullRequest"]["isDraft"]
                            self.logger.info(f"Successfully marked PR #{pr.number} as ready for review. New draft status: {updated_draft_status}")
                            
                            # Give GitHub a moment to update and refresh our PR object
                            import time
                            time.sleep(2)
                            pr = repo.get_pull(pr.number)
                            self.logger.info(f"After refresh - PR #{pr.number} draft status: {getattr(pr, 'draft', 'unknown')}")
                        else:
                            self.logger.info(f"PR #{pr.number} is already marked as ready for review")
                            
                    except Exception as e:
                        self.logger.error(f"Failed to mark PR #{pr.number} as ready for review: {e}")
                        results.append(
                            PRRunResult(
                                repo=repo_name,
                                pr_number=pr.number,
                                title=pr.title,
                                status='merge_error',
                                details=f'Failed to ready draft: {self._shorten_text(str(e))}',
                            )
                        )
                        continue
                
                # Check for mergeability (no conflicts)
                if pr.mergeable is False:
                    self.logger.info(f"PR #{pr.number} in {repo_name} is approved but has conflicts, skipping merge")
                    # Increment attempt count before handling conflicts
                    attempt_count = self._increment_merge_attempt_count(pr)
                    
                    # Add a comment to the PR about conflicts
                    try:
                        pr.create_issue_comment("@copilot please fix merge conflicts")
                        self.logger.info(f"Added conflict comment to PR #{pr.number} in {repo_name}")
                    except Exception as e:
                        self.logger.error(f"Failed to comment on PR #{pr.number} about conflicts: {e}")
                    
                    # Only reassign to Copilot if not already assigned to avoid interrupting ongoing work
                    if not self._is_copilot_already_assigned_to_pr(pr):
                        try:
                            repo_parts = repo_name.split('/')
                            repo_owner = repo_parts[0]
                            repo_name_only = repo_parts[1]
                            pr_id, bot_id = self._get_pr_id_and_bot_id(repo_owner, repo_name_only, pr.number)
                            if pr_id and bot_id:
                                success = self._assign_pr_via_graphql(pr_id, bot_id)
                                if success:
                                    self.logger.info(f"Successfully reassigned PR #{pr.number} to Copilot due to merge conflicts")
                                else:
                                    self.logger.warning(f"Failed to reassign PR #{pr.number} to Copilot after merge conflict")
                            else:
                                self.logger.warning(f"Could not find PR ID or suitable bot for reassigning PR #{pr.number}")
                        except Exception as e:
                            self.logger.error(f"Failed to reassign PR #{pr.number} to Copilot: {e}")
                    else:
                        self.logger.info(f"PR #{pr.number} is already assigned to Copilot, not reassigning to avoid interrupting ongoing work")
                    
                    results.append(
                        PRRunResult(
                            repo=repo_name,
                            pr_number=pr.number,
                            title=pr.title,
                            status='merge_error',
                            details='Has merge conflicts',
                            attempts=attempt_count,
                        )
                    )
                    continue
                elif pr.mergeable is None:
                    self.logger.warning(f"PR #{pr.number} in {repo_name} has unknown mergeable state, skipping merge")
                    # Increment attempt count before recording error
                    attempt_count = self._increment_merge_attempt_count(pr)
                    results.append(
                        PRRunResult(
                            repo=repo_name,
                            pr_number=pr.number,
                            title=pr.title,
                            status='merge_error',
                            details='Unknown mergeability',
                            attempts=attempt_count,
                        )
                    )
                    continue
                
                # Final check to ensure PR is not a draft before merging
                pr_draft_status = getattr(pr, 'draft', False)
                if pr_draft_status:
                    self.logger.error(f"PR #{pr.number} is still in draft state after processing, skipping merge")
                    # Increment attempt count before recording error
                    attempt_count = self._increment_merge_attempt_count(pr)
                    results.append(
                        PRRunResult(
                            repo=repo_name,
                            pr_number=pr.number,
                            title=pr.title,
                            status='merge_error',
                            details='PR still in draft state',
                            attempts=attempt_count,
                        )
                    )
                    continue
                
                # Increment attempt count before trying to merge
                attempt_count = self._increment_merge_attempt_count(pr)
                
                self.logger.info(f"Attempting to merge PR #{pr.number} in {repo_name} (approved={approved}, mergeable={pr.mergeable}, draft={pr_draft_status}, attempt={attempt_count})")
                try:
                    merge_result = pr.merge(merge_method='squash', commit_message=f"Auto-merged by JediMaster: {pr.title}")
                    if merge_result.merged:
                        self.logger.info(f"Successfully auto-merged PR #{pr.number} in {repo_name} on attempt {attempt_count}")
                        
                        # Remove merge attempt labels since merge was successful
                        try:
                            attempt_labels = [label for label in pr.labels if label.name.startswith('merge-attempt-')]
                            for label in attempt_labels:
                                pr.remove_from_labels(label)
                            self.logger.info(f"Removed merge attempt labels from successfully merged PR #{pr.number}")
                        except Exception as e:
                            self.logger.debug(f"Could not remove merge attempt labels from PR #{pr.number}: {e}")
                        
                        # Close linked issues after successful merge
                        try:
                            closed_issues = self._close_linked_issues(repo, pr.number, pr.title)
                            if closed_issues:
                                self.logger.info(f"Closed {len(closed_issues)} linked issues: {closed_issues}")
                        except Exception as e:
                            self.logger.error(f"Failed to close linked issues for PR #{pr.number}: {e}")
                        
                        # Delete the branch associated with the PR after successful merge
                        try:
                            self._delete_pr_branch(pr)
                        except Exception as e:
                            self.logger.error(f"Failed to delete branch for PR #{pr.number}: {e}")
                        
                        results.append(
                            PRRunResult(
                                repo=repo_name,
                                pr_number=pr.number,
                                title=pr.title,
                                status='merged',
                                details='Auto-merged successfully',
                                attempts=attempt_count,
                            )
                        )
                    else:
                        self.logger.error(f"Merge failed for PR #{pr.number} in {repo_name} on attempt {attempt_count}: {merge_result.message}")
                        
                        # Add a comment about merge failure and reassign to Copilot
                        try:
                            pr.create_issue_comment(f"Auto-merge failed (attempt {attempt_count}): {merge_result.message}. Please investigate.")
                            self.logger.info(f"Added merge failure comment to PR #{pr.number} in {repo_name}")
                        except Exception as e:
                            self.logger.error(f"Failed to comment on PR #{pr.number} about merge failure: {e}")
                        
                        # Only reassign to Copilot if not already assigned to avoid interrupting ongoing work
                        if not self._is_copilot_already_assigned_to_pr(pr):
                            try:
                                repo_parts = repo_name.split('/')
                                repo_owner = repo_parts[0]
                                repo_name_only = repo_parts[1]
                                pr_id, bot_id = self._get_pr_id_and_bot_id(repo_owner, repo_name_only, pr.number)
                                if pr_id and bot_id:
                                    success = self._assign_pr_via_graphql(pr_id, bot_id)
                                    if success:
                                        self.logger.info(f"Successfully reassigned PR #{pr.number} to Copilot due to merge failure")
                                    else:
                                        self.logger.warning(f"Failed to reassign PR #{pr.number} to Copilot after merge failure")
                                else:
                                    self.logger.warning(f"Could not find PR ID or suitable bot for reassigning PR #{pr.number}")
                            except Exception as e:
                                self.logger.error(f"Failed to reassign PR #{pr.number} to Copilot: {e}")
                        else:
                            self.logger.info(f"PR #{pr.number} is already assigned to Copilot, not reassigning to avoid interrupting ongoing work")
                        
                        results.append(
                            PRRunResult(
                                repo=repo_name,
                                pr_number=pr.number,
                                title=pr.title,
                                status='merge_error',
                                details=f"Merge failed: {self._shorten_text(merge_result.message)}",
                                attempts=attempt_count,
                            )
                        )
                except Exception as e:
                    self.logger.error(f"Failed to auto-merge PR #{pr.number} in {repo_name} on attempt {attempt_count}: {e}")
                    
                    # Add a comment about exception and reassign to Copilot
                    try:
                        pr.create_issue_comment(f"Auto-merge exception (attempt {attempt_count}): {str(e)}. Please investigate.")
                        self.logger.info(f"Added merge exception comment to PR #{pr.number} in {repo_name}")
                    except Exception as comment_e:
                        self.logger.error(f"Failed to comment on PR #{pr.number} about merge exception: {comment_e}")
                    
                    # Only reassign to Copilot if not already assigned to avoid interrupting ongoing work
                    if not self._is_copilot_already_assigned_to_pr(pr):
                        try:
                            repo_parts = repo_name.split('/')
                            repo_owner = repo_parts[0]
                            repo_name_only = repo_parts[1]
                            pr_id, bot_id = self._get_pr_id_and_bot_id(repo_owner, repo_name_only, pr.number)
                            if pr_id and bot_id:
                                success = self._assign_pr_via_graphql(pr_id, bot_id)
                                if success:
                                    self.logger.info(f"Successfully reassigned PR #{pr.number} to Copilot due to merge exception")
                                else:
                                    self.logger.warning(f"Failed to reassign PR #{pr.number} to Copilot after merge exception")
                            else:
                                self.logger.warning(f"Could not find PR ID or suitable bot for reassigning PR #{pr.number}")
                        except Exception as assign_e:
                            self.logger.error(f"Failed to reassign PR #{pr.number} to Copilot: {assign_e}")
                    else:
                        self.logger.info(f"PR #{pr.number} is already assigned to Copilot, not reassigning to avoid interrupting ongoing work")
                    
                    results.append(
                        PRRunResult(
                            repo=repo_name,
                            pr_number=pr.number,
                            title=pr.title,
                            status='merge_error',
                            details=f"Merge exception: {self._shorten_text(str(e))}",
                            attempts=attempt_count,
                        )
                    )
        except Exception as e:
            self.logger.error(f"Error merging reviewed PRs in {repo_name}: {e}")
            results.append(
                PRRunResult(
                    repo=repo_name,
                    pr_number=0,
                    title='Merge processing error',
                    status='merge_error',
                    details=self._shorten_text(str(e)),
                )
            )
        return results

    def process_pull_requests(self, repo_name: str, batch_size: int = 15):
        """Process open pull requests with PRDeciderAgent.
        
        Args:
            repo_name: The repository name in format 'owner/repo'
            batch_size: Maximum number of PRs to process (default 15)
        """
        results: List[PRRunResult] = []
        processed_prs = []
        try:
            repo = self.github.get_repo(repo_name)
            all_pulls = list(repo.get_pulls(state='open'))
            self.logger.info(f"Found {len(all_pulls)} open PRs in {repo_name}")

            for pr in all_pulls:
                if len(processed_prs) >= batch_size:
                    break

                pr_data = self._get_pr_review_states(repo_name, pr.number)
                if not pr_data:
                    error_msg = "Failed to retrieve PR review metadata"
                    self.logger.error(f"{error_msg} for PR #{pr.number}")
                    results.append(
                        PRRunResult(
                            repo=repo_name,
                            pr_number=pr.number,
                            title=pr.title,
                            status='error',
                            details=error_msg,
                        )
                    )
                    continue

                if not self._should_process_pr(pr_data):
                    self.logger.info(
                        f"Skipping PR #{pr.number} - not waiting for review or already decided"
                    )
                    continue

                self.logger.info(f"Processing PR #{pr.number} - needs review")
                processed_prs.append(pr)

                pr_text = f"Title: {pr.title}\n\nDescription:\n{pr.body or ''}\n\n"
                try:
                    # Use PyGithub's built-in diff access instead of manual requests
                    # This automatically handles authentication and private repos
                    diff_content = ""
                    
                    # Get the files changed in the PR
                    try:
                        files = list(pr.get_files())
                        
                        # Check if we got 0 files
                        if len(files) == 0:
                            # Inspect recent issue comments to see if Copilot previously reported a rate limit
                            last_comment = None
                            try:
                                comments = list(pr.get_issue_comments())
                                if comments:
                                    last_comment = comments[-1]
                            except Exception as c_e:
                                self.logger.warning(f"Failed to fetch comments for PR #{pr.number}: {c_e}")

                            def _is_copilot_rate_limit_comment_from_body(body: str) -> bool:
                                if not body:
                                    return False
                                b = body.lower()
                                if 'copilot stopped work due to an error' in b:
                                    return True
                                if 'premium requests' in b or 'premium request' in b:
                                    return True
                                if 'rate limit' in b or 'rate-limited' in b or 'rate-limit' in b:
                                    return True
                                if 'session could not start' in b or 'used up the' in b:
                                    return True
                                return False

                            def _is_copilot_rate_limit_comment(comment) -> bool:
                                if not comment:
                                    return False
                                try:
                                    author = getattr(comment.user, 'login', '') or ''
                                    author_l = author.lower()
                                    body = comment.body or ''
                                    known_bot_logins = [
                                        'github-copilot-reviewer[bot]',
                                        'github-copilot-reviewer',
                                        'github-actions[bot]',
                                        'copilot-swe-agent',
                                    ]
                                    is_bot = any(k in author_l for k in known_bot_logins) or 'copilot' in author_l or author_l.endswith('[bot]')
                                    if is_bot and _is_copilot_rate_limit_comment_from_body(body):
                                        return True
                                    if 'copilot' in body.lower() and _is_copilot_rate_limit_comment_from_body(body):
                                        return True
                                except Exception:
                                    return False
                                return False

                            if _is_copilot_rate_limit_comment(last_comment):
                                # Copilot previously reported rate limit; reassign so Copilot can retry with elevated privileges
                                try:
                                    self.logger.warning(f"PR #{pr.number} returned 0 files and Copilot previously reported rate limits (comment by {detected_rate_limit_comment.user.login}). Reassigning to Copilot.")
                                except Exception:
                                    self.logger.warning(f"PR #{pr.number} returned 0 files and Copilot previously reported rate limits. Reassigning to Copilot.")

                                try:
                                    pr.create_issue_comment("@copilot I'm reassigning this PR to you to retry fetching the file contents due to previous rate limits.")
                                    self.logger.info(f"Added rate limit override comment to PR #{pr.number} in {repo_name}")
                                except Exception as comment_e:
                                    self.logger.error(f"Failed to comment on PR #{pr.number} about rate limits: {comment_e}")

                                # Reassign to Copilot if not already assigned
                                if not self._is_copilot_already_assigned_to_pr(pr):
                                    try:
                                        repo_parts = repo_name.split('/')
                                        repo_owner = repo_parts[0]
                                        repo_name_only = repo_parts[1]
                                        pr_id, bot_id = self._get_pr_id_and_bot_id(repo_owner, repo_name_only, pr.number)
                                        if pr_id and bot_id:
                                            success = self._assign_pr_via_graphql(pr_id, bot_id)
                                            if success:
                                                self.logger.info(f"Successfully reassigned PR #{pr.number} to Copilot due to prior rate limit comment")
                                            else:
                                                self.logger.warning(f"Failed to reassign PR #{pr.number} to Copilot after prior rate limit comment")
                                        else:
                                            self.logger.warning(f"Could not find PR ID or suitable bot for reassigning PR #{pr.number}")
                                    except Exception as assign_e:
                                        self.logger.error(f"Failed to reassign PR #{pr.number} to Copilot: {assign_e}")
                                else:
                                    self.logger.info(f"PR #{pr.number} is already assigned to Copilot, not reassigning")

                                results.append(
                                    PRRunResult(
                                        repo=repo_name,
                                        pr_number=pr.number,
                                        title=pr.title,
                                        status='skipped',
                                        details='Rate limit reported by Copilot previously, reassigned to Copilot',
                                    )
                                )
                                continue
                            else:
                                # No Copilot rate-limit message - treat as a PR with no file changes. Comment and skip for human review.
                                self.logger.info(f"PR #{pr.number} returned 0 files from get_files() and no Copilot rate-limit comment was detected.")
                                try:
                                    pr.create_issue_comment("No files were detected in this PR. If this PR requires review, please push changes or re-open with changes. Skipping for now.")
                                    self.logger.info(f"Commented on PR #{pr.number} noting no file changes in {repo_name}")
                                except Exception as comment_e:
                                    self.logger.error(f"Failed to comment on PR #{pr.number} about empty file list: {comment_e}")

                                results.append(
                                    PRRunResult(
                                        repo=repo_name,
                                        pr_number=pr.number,
                                        title=pr.title,
                                        status='skipped',
                                        details='No files changed in PR - skipped',
                                    )
                                )
                                continue
                        
                        for file in files:
                            if hasattr(file, 'patch') and file.patch:
                                diff_content += f"\n--- {file.filename} ---\n"
                                diff_content += file.patch + "\n"
                        
                        # If no patches found, fall back to manual diff fetch
                        if not diff_content.strip():
                            raise RuntimeError("No file patches available, falling back to manual fetch")
                            
                    except Exception as file_e:
                        self.logger.warning(f"Failed to get files for PR #{pr.number}, trying manual diff fetch: {file_e}")
                        
                        # Fallback to manual diff URL fetch
                        diff_url = pr.diff_url
                        diff_headers = {
                            "Accept": "application/vnd.github.v3.diff",
                            "Authorization": f"Bearer {self.github_token}",
                            "X-GitHub-Api-Version": "2022-11-28"
                        }
                        diff_resp = requests.get(diff_url, headers=diff_headers, timeout=15)
                        if diff_resp.status_code == 401:
                            raise RuntimeError(f"Authentication failed (401). Check your GitHub token permissions for private repositories.")
                        elif diff_resp.status_code == 403:
                            raise RuntimeError(f"Access forbidden (403). Your token may lack 'repo' scope for private repositories.")
                        elif diff_resp.status_code == 404:
                            raise RuntimeError(f"PR not found (404). Repository may be private and token lacks access.")
                        elif diff_resp.status_code != 200:
                            raise RuntimeError(f"GitHub returned status {diff_resp.status_code}: {diff_resp.text[:200]}")
                        diff_content = diff_resp.text
                except Exception as e:
                    error_msg = f"Failed to fetch diff: {e}"
                    self.logger.error(f"{error_msg} for PR #{pr.number}")
                    results.append(
                        PRRunResult(
                            repo=repo_name,
                            pr_number=pr.number,
                            title=pr.title,
                            status='error',
                            details=error_msg,
                        )
                    )
                    continue

                pr_text += f"Diff:\n{diff_content[:5000]}"
                agent_result = self.pr_decider.evaluate_pr(pr_text)

                current_pr_data = self._get_pr_review_states(repo_name, pr.number)
                if not current_pr_data:
                    error_msg = "Failed to refresh PR review metadata"
                    self.logger.error(f"{error_msg} for PR #{pr.number}")
                    results.append(
                        PRRunResult(
                            repo=repo_name,
                            pr_number=pr.number,
                            title=pr.title,
                            status='error',
                            details=error_msg,
                        )
                    )
                    continue

                if not self._should_process_pr(current_pr_data):
                    self.logger.info(
                        f"PR #{pr.number} state changed during processing, skipping action"
                    )
                    results.append(
                        PRRunResult(
                            repo=repo_name,
                            pr_number=pr.number,
                            title=pr.title,
                            status='state_changed',
                            details='State changed during review',
                        )
                    )
                    continue

                if 'comment' in agent_result:
                    comment_body = f"@copilot {agent_result['comment']}"
                    try:
                        pr.create_review(event='REQUEST_CHANGES', body=comment_body)
                        self.logger.info(f"Requested changes on PR #{pr.number} in {repo_name}")
                        results.append(
                            PRRunResult(
                                repo=repo_name,
                                pr_number=pr.number,
                                title=pr.title,
                                status='changes_requested',
                                details=self._shorten_text(comment_body),
                            )
                        )
                    except Exception as e:
                        error_msg = str(e)
                        self.logger.error(f"Failed to request changes on PR #{pr.number}: {error_msg}")
                        results.append(
                            PRRunResult(
                                repo=repo_name,
                                pr_number=pr.number,
                                title=pr.title,
                                status='error',
                                details=error_msg,
                            )
                        )
                elif agent_result.get('decision') == 'accept':
                    reviews = current_pr_data.get('reviews', {}).get('nodes', [])
                    already_approved = any(review.get('state') == 'APPROVED' for review in reviews)
                    is_draft = current_pr_data.get('isDraft', False)

                    if already_approved:
                        self.logger.info(
                            f"PR #{pr.number} already has approval, skipping duplicate approval"
                        )
                        results.append(
                            PRRunResult(
                                repo=repo_name,
                                pr_number=pr.number,
                                title=pr.title,
                                status='skipped',
                                details='Already approved',
                            )
                        )
                    else:
                        self.logger.info(f"PR #{pr.number} in {repo_name} can be accepted as-is.")
                        try:
                            if is_draft:
                                self.logger.info(
                                    f"PR #{pr.number} is a draft, marking as ready for review"
                                )
                                try:
                                    mutation = """
                                    mutation($pullRequestId: ID!) {
                                      markPullRequestReadyForReview(input: {pullRequestId: $pullRequestId}) {
                                        pullRequest {
                                          isDraft
                                        }
                                      }
                                    }
                                    """
                                    pr_id = current_pr_data.get('id')
                                    if pr_id:
                                        mutation_vars = {"pullRequestId": pr_id}
                                        mutation_result = self._graphql_request(mutation, mutation_vars)
                                        if 'errors' in mutation_result:
                                            self.logger.error(
                                                f"GraphQL mutation errors: {mutation_result['errors']}"
                                            )
                                        else:
                                            self.logger.info(
                                                f"Successfully marked draft PR #{pr.number} as ready for review"
                                            )
                                    else:
                                        self.logger.warning(
                                            f"Could not get PR ID for draft PR #{pr.number}"
                                        )
                                except Exception as e:
                                    self.logger.error(
                                        f"Failed to mark draft PR #{pr.number} as ready for review: {e}"
                                    )

                            pr.create_review(
                                event='APPROVE',
                                body='Automatically approved by JediMaster.',
                            )
                            self.logger.info(f"Approved PR #{pr.number} in {repo_name}.")
                            details = 'Auto-approved'
                            if is_draft:
                                details += ' (draft readied)'
                            results.append(
                                PRRunResult(
                                    repo=repo_name,
                                    pr_number=pr.number,
                                    title=pr.title,
                                    status='approved',
                                    details=details,
                                )
                            )
                        except Exception as e:
                            error_msg = str(e)
                            self.logger.error(
                                f"Failed to submit review for PR #{pr.number}: {error_msg}"
                            )
                            results.append(
                                PRRunResult(
                                    repo=repo_name,
                                    pr_number=pr.number,
                                    title=pr.title,
                                    status='error',
                                    details=error_msg,
                                )
                            )
                else:
                    self.logger.warning(
                        f"Unexpected PRDeciderAgent result for PR #{pr.number}: {agent_result}"
                    )
                    results.append(
                        PRRunResult(
                            repo=repo_name,
                            pr_number=pr.number,
                            title=pr.title,
                            status='unknown',
                            details=self._shorten_text(str(agent_result)),
                        )
                    )
        except Exception as e:
            error_msg = str(e)
            self.logger.error(f"Error processing PRs in {repo_name}: {error_msg}")
            results.append(
                PRRunResult(
                    repo=repo_name,
                    pr_number=0,
                    title='Processing error',
                    status='error',
                    details=error_msg,
                )
            )
        return results

    def _setup_logger(self) -> logging.Logger:
        logger = logging.getLogger('jedimaster')
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        return logger

    def _check_rate_limit_status(self) -> tuple[bool, str]:
        """Check if we're hitting GitHub API rate limits.
        
        Returns:
            tuple: (is_rate_limited, status_message)
        """
        try:
            rate_limit = self.github.get_rate_limit()
            core_remaining = rate_limit.core.remaining
            core_limit = rate_limit.core.limit
            
            # Consider it rate limited if we have less than 10% remaining
            rate_limit_threshold = max(10, core_limit * 0.1)
            
            if core_remaining <= rate_limit_threshold:
                reset_time = rate_limit.core.reset.strftime('%H:%M:%S')
                return True, f"Rate limit: {core_remaining}/{core_limit} remaining, resets at {reset_time}"
            
            return False, f"Rate limit OK: {core_remaining}/{core_limit} remaining"
            
        except Exception as e:
            self.logger.warning(f"Failed to check rate limit status: {e}")
            return False, "Rate limit check failed"

    def _graphql_request(self, query: str, variables: Optional[Dict] = None) -> Dict:
        url = "https://api.github.com/graphql"
        headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        response = requests.post(url, json=payload, headers=headers)
        response.raise_for_status()
        return response.json()

    def _get_pr_review_states(self, repo_name: str, pr_number: int) -> Dict[str, Any]:
        """Get the review states for a PR using GraphQL."""
        owner, name = repo_name.split('/')
        query = """
        query($owner: String!, $name: String!, $number: Int!) {
            repository(owner: $owner, name: $name) {
                pullRequest(number: $number) {
                    id
                    isDraft
                    reviewDecision
                    reviewRequests(first: 10) {
                        totalCount
                    }
                    reviews(first: 20, states: [APPROVED, CHANGES_REQUESTED, COMMENTED, DISMISSED, PENDING]) {
                        nodes {
                            id
                            state
                            author {
                                login
                            }
                            createdAt
                        }
                    }
                }
            }
        }
        """
        variables = {
            "owner": owner,
            "name": name,
            "number": pr_number
        }
        try:
            result = self._graphql_request(query, variables)
            if 'errors' in result:
                self.logger.error(f"GraphQL errors fetching PR review states: {result['errors']}")
                return {}
            return result.get('data', {}).get('repository', {}).get('pullRequest', {})
        except Exception as e:
            self.logger.error(f"Error fetching PR review states for #{pr_number}: {e}")
            return {}

    def _should_process_pr(self, pr_data: Dict[str, Any]) -> bool:
        """Determine if a PR should be processed based on its review state."""
        if not pr_data:
            self.logger.debug("No PR data available, skipping")
            return False
        
        # Check if PR is in draft state
        is_draft = pr_data.get('isDraft', False)
        review_decision = pr_data.get('reviewDecision')
        
        # Check if there are pending review requests
        review_requests = pr_data.get('reviewRequests', {})
        has_pending_requests = review_requests.get('totalCount', 0) > 0
        self.logger.debug(f"PR is draft: {is_draft}, reviewDecision: {review_decision}, pending review requests: {review_requests.get('totalCount', 0)}")
        
        if review_decision == 'CHANGES_REQUESTED':
            self.logger.debug("Aggregate review decision is CHANGES_REQUESTED, skipping")
            return False
        if review_decision == 'APPROVED' and not has_pending_requests:
            self.logger.debug("Aggregate review decision is APPROVED with no pending requests, skipping")
            return False
        
        # For draft PRs, only process if they have review requests
        # This indicates the author wants review despite being in draft
        if is_draft:
            if has_pending_requests:
                self.logger.debug("Draft PR with review requests - processing")
                return True
            else:
                self.logger.debug("Draft PR without review requests - skipping")
                return False
        
        # For non-draft PRs, continue with existing logic
        # Check existing review states
        reviews = pr_data.get('reviews', {}).get('nodes', [])
        if not reviews:
            # No reviews yet, process if there are review requests
            self.logger.debug(f"No existing reviews, processing: {has_pending_requests}")
            return has_pending_requests
        
        # Get the most recent review state from each reviewer
        latest_reviews = {}
        for review in reviews:
            author = review.get('author', {}).get('login')
            if author:
                # Keep only the most recent review per author
                if author not in latest_reviews or review['createdAt'] > latest_reviews[author]['createdAt']:
                    latest_reviews[author] = review
        
        self.logger.debug(f"Found {len(latest_reviews)} unique reviewers with states: {[r.get('state') for r in latest_reviews.values()]}")
        
        # Check if there are any blocking states
        for author, review in latest_reviews.items():
            state = review.get('state')
            if state == 'CHANGES_REQUESTED':
                # Don't process PRs that have requested changes
                self.logger.debug(f"PR has CHANGES_REQUESTED from {author}, skipping")
                return False
            elif state == 'APPROVED':
                # Don't process already approved PRs (unless there are new review requests)
                if not has_pending_requests:
                            self.logger.debug(f"PR already APPROVED by {author} with no pending requests, skipping")
                            return False
        
        # Process if:
        # 1. There are pending review requests, OR
        # 2. All existing reviews are just COMMENTED/PENDING (no approval or changes requested)
                should_process = (
                    has_pending_requests
                    or review_decision == 'REVIEW_REQUIRED'
                    or any(
            review.get('state') in ['COMMENTED', 'PENDING'] for review in latest_reviews.values()
                    )
                )
        self.logger.debug(f"Final decision to process PR: {should_process}")
        return should_process


    def process_user(self, username: str) -> ProcessingReport:
        filter_method = "topic 'managed-by-coding-agent'" if self.use_topic_filter else ".coding_agent file"
        self.logger.info(f"Processing user: {username} (filtering by {filter_method})")
        try:
            user = self.github.get_user(username)
            all_repos = user.get_repos()
            filtered_repos = []
            for repo in all_repos:
                if self.use_topic_filter:
                    if self._repo_has_topic(repo, "managed-by-coding-agent"):
                        filtered_repos.append(repo.full_name)
                        self.logger.info(f"Found topic 'managed-by-coding-agent' in repository: {repo.full_name}")
                else:
                    if self._file_exists_in_repo(repo, ".coding_agent"):
                        filtered_repos.append(repo.full_name)
                        self.logger.info(f"Found .coding_agent file in repository: {repo.full_name}")
            if not filtered_repos:
                filter_desc = "topic 'managed-by-coding-agent'" if self.use_topic_filter else ".coding_agent file"
                self.logger.info(f"No repositories found with {filter_desc} for user {username}")
                return ProcessingReport()
            filter_desc = "topic 'managed-by-coding-agent'" if self.use_topic_filter else ".coding_agent file"
            self.logger.info(f"Found {len(filtered_repos)} repositories with {filter_desc}")
            return self.process_repositories(filtered_repos)
        except GithubException as e:
            error_msg = f"Error accessing user {username}: {e}"
            self.logger.error(error_msg)
            return ProcessingReport(
                errors=1,
                results=[IssueResult(
                    repo=f"user/{username}",
                    issue_number=0,
                    title=f"User Error: {username}",
                    url='',
                    status='error',
                    error_message=error_msg
                )]
            )
        except Exception as e:
            error_msg = f"Unexpected error processing user {username}: {e}"
            self.logger.error(error_msg)
            return ProcessingReport(
                errors=1,
                results=[IssueResult(
                    repo=f"user/{username}",
                    issue_number=0,
                    title=f"User Error: {username}",
                    url='',
                    status='error',
                    error_message=error_msg
                )]
            )

    def process_repositories(self, repo_names: List[str]) -> ProcessingReport:
        all_results = []
        pr_results = []
        for repo_name in repo_names:
            self.logger.info(f"Processing repository: {repo_name}")
            try:
                if self.process_prs:
                    pr_results.extend(self.process_pull_requests(repo_name))
                else:
                    # Only process issues if not doing PR processing
                    issues = self.fetch_issues(repo_name)
                    for issue in issues:
                        if issue.pull_request:
                            continue
                        result = self.process_issue(issue, repo_name)
                        all_results.append(result)
            except Exception as e:
                self.logger.error(f"Failed to process repository {repo_name}: {e}")
                if not self.process_prs:  # Only add issue error results when processing issues
                    all_results.append(IssueResult(
                        repo=repo_name,
                        issue_number=0,
                        title=f"Repository Error: {repo_name}",
                        url='',
                        status='error',
                        error_message=str(e)
                    ))
        
        # Calculate statistics based on what was actually processed
        if self.process_prs:
            # When processing PRs, create a minimal report focused on PR results
            report = ProcessingReport(
                total_issues=0,  # No issues processed
                assigned=0,
                not_assigned=0,
                already_assigned=0,
                labeled=0,
                errors=0,
                results=[]  # No issue results
            )
            report.pr_results = pr_results
        else:
            # When processing issues, create standard issue report
            assigned_count = sum(1 for r in all_results if r.status == 'assigned')
            not_assigned_count = sum(1 for r in all_results if r.status == 'not_assigned')
            already_assigned_count = sum(1 for r in all_results if r.status == 'already_assigned')
            labeled_count = sum(1 for r in all_results if r.status == 'labeled')
            error_count = sum(1 for r in all_results if r.status == 'error')
            report = ProcessingReport(
                total_issues=len(all_results),
                assigned=assigned_count,
                not_assigned=not_assigned_count,
                already_assigned=already_assigned_count,
                labeled=labeled_count,
                errors=error_count,
                results=all_results
            )
        return report

    def save_report(self, report: ProcessingReport, filename: Optional[str] = None) -> str:
        out_filename: str
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_filename = f"jedimaster_report_{timestamp}.json"
        else:
            out_filename = filename
        with open(out_filename, 'w', encoding='utf-8') as f:
            json.dump(asdict(report), f, indent=2, ensure_ascii=False)
        self.logger.info(f"Report saved to {out_filename}")
        return out_filename

    def print_summary(
        self,
        report: ProcessingReport,
        context: str = "issues",
        pr_results: Optional[List[PRRunResult]] = None,
    ):
        print("\nJEDIMASTER PROCESSING SUMMARY")
        summary_rows = [("Timestamp", report.timestamp)]

        if context == "prs":
            results = pr_results if pr_results is not None else report.pr_results
            summary_rows.append(("Mode", "PR review"))
            summary_rows.append(("Pull requests reviewed", len(results)))
            status_counts = Counter(r.status for r in results)
            ordered_statuses = [
                "changes_requested",
                "approved",
                "skipped",
                "state_changed",
                "error",
                "unknown",
            ]
            for status in ordered_statuses:
                count = status_counts.get(status, 0)
                if count:
                    summary_rows.append((self._friendly_pr_status(status), count))
            for status, count in status_counts.items():
                if status not in ordered_statuses and count:
                    summary_rows.append((self._friendly_pr_status(status), count))
            print(format_table(["Metric", "Value"], summary_rows))
            if not results:
                print("\nNo pull requests met the criteria for review.")
            return

        if context == "merge":
            results = pr_results if pr_results is not None else report.pr_results
            summary_rows.append(("Mode", "Auto-merge"))
            summary_rows.append(("Pull requests evaluated", len(results)))
            status_counts = Counter(r.status for r in results)
            ordered_statuses = [
                "merged",
                "merge_error",
                "max_retries_exceeded",
                "skipped",
                "error",
            ]
            for status in ordered_statuses:
                count = status_counts.get(status, 0)
                if count:
                    summary_rows.append((self._friendly_pr_status(status), count))
            for status, count in status_counts.items():
                if status not in ordered_statuses and count:
                    summary_rows.append((self._friendly_pr_status(status), count))
            print(format_table(["Metric", "Value"], summary_rows))
            if not results:
                print("\nNo reviewed pull requests were eligible for auto-merge.")
            return

        summary_rows.extend([
            ("Total Issues", report.total_issues),
            ("Assigned", report.assigned),
            ("Labeled", report.labeled),
            ("Not Assigned", report.not_assigned),
            ("Already Assigned", report.already_assigned),
            ("Errors", report.errors),
        ])
        print(format_table(["Metric", "Value"], summary_rows))

        detail_rows = []
        for result in report.results:
            detail_rows.append([
                result.repo,
                f"#{result.issue_number}",
                self._friendly_issue_status(result.status),
                self._shorten_text(result.reasoning or result.error_message or ""),
            ])

        print()
        print(
            format_table(
                ["Repo", "Issue", "Status", "Details"],
                detail_rows,
                empty_message="No issues processed",
            )
        )

    def print_pr_results(self, heading: str, pr_results: List[PRRunResult]):
        print(f"\n{heading}")
        rows = []
        for result in pr_results:
            details = result.details or ""
            if result.attempts is not None:
                attempt_text = f"attempt {result.attempts}"
                details = f"{details} ({attempt_text})" if details else attempt_text
            rows.append(
                [
                    result.repo,
                    f"#{result.pr_number}",
                    self._shorten_text(result.title, 60),
                    self._friendly_pr_status(result.status),
                    self._shorten_text(details),
                ]
            )

        print(
            format_table(
                ["Repo", "PR", "Title", "Status", "Details"],
                rows,
                empty_message="No pull requests",
            )
        )



def main():
    """Main entry point for the JediMaster script."""
    parser = argparse.ArgumentParser(description='JediMaster - Label or assign GitHub issues to Copilot and optionally process PRs')

    # Create mutually exclusive group for repositories vs user
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('repositories', nargs='*',
                       help='GitHub repositories to process (format: owner/repo)')
    group.add_argument('--user', '-u',
                       help='GitHub username to process (will process repos with topic "managed-by-coding-agent" or .coding_agent file)')

    parser.add_argument('--output', '-o',
                       help='Output filename for the report (default: auto-generated)')
    parser.add_argument('--save-report', action='store_true',
                       help='Save detailed report to JSON file (default: no)')
    parser.add_argument('--verbose', '-v', action='store_true',
                       help='Enable verbose logging')
    parser.add_argument('--just-label', action='store_true',
                       help='Only add labels to issues, do not assign them to Copilot')
    parser.add_argument('--use-file-filter', action='store_true',
                       help='Use .coding_agent file filtering instead of topic filtering (slower but backwards compatible)')

    parser.add_argument('--process-prs', action='store_true',
                       help='Process open pull requests with PRDeciderAgent (add comments or log check-in readiness)')
    parser.add_argument('--auto-merge-reviewed', action='store_true',
                       help='Automatically merge reviewed PRs with no conflicts')

    parser.add_argument('--create-issues', action='store_true',
                       help='Use CreatorAgent to suggest and open new issues in the specified repositories')
    parser.add_argument('--similarity-threshold', type=float, metavar='THRESHOLD',
                       help='Similarity threshold for duplicate detection when creating issues (0.0-1.0, default: 0.9 with OpenAI embeddings, 0.5 with local similarity)')

    args = parser.parse_args()

    # Validate arguments
    if not args.user and not args.repositories:
        parser.error("Either specify repositories or use --user option")

    # Determine similarity mode and threshold
    use_openai_similarity = args.similarity_threshold is not None
    similarity_threshold = args.similarity_threshold if args.similarity_threshold is not None else 0.9
    
    # Validate similarity threshold
    if not (0.0 <= similarity_threshold <= 1.0):
        parser.error("Similarity threshold must be between 0.0 and 1.0")

    # Load environment variables from .env file (if it exists)
    load_dotenv(override=True)

    # Get credentials from environment (either from .env or system environment)
    github_token = os.getenv('GITHUB_TOKEN')
    azure_foundry_endpoint = os.getenv('AZURE_AI_FOUNDRY_ENDPOINT')

    if not github_token:
        print("Error: GITHUB_TOKEN environment variable is required")
        print("Set it in .env file or as a system environment variable")
        return 1

    def _mask_token(token: str) -> str:
        if len(token) <= 10:
            return token
        return f"{token[:6]}...{token[-4:]}"

    print(f"Using GITHUB_TOKEN: {_mask_token(github_token)}")

    if not azure_foundry_endpoint:
        print("Error: AZURE_AI_FOUNDRY_ENDPOINT environment variable is required")
        print("Set it in .env file or as a system environment variable")
        print("Authentication to Azure AI Foundry will use managed identity (DefaultAzureCredential)")
        return 1

    # Set up logging level
    if args.verbose:
        logging.getLogger('jedimaster').setLevel(logging.DEBUG)


    try:
        use_topic_filter = not args.use_file_filter

        # If --create-issues is set, use CreatorAgent for each repo
        if args.create_issues:
            if args.user:
                print("--create-issues does not support --user mode. Please specify repositories explicitly.")
                return 1
            if not args.repositories:
                print("No repositories specified for --create-issues.")
                return 1
            for repo_full_name in args.repositories:
                print(f"\n[CreatorAgent] Suggesting and opening issues for {repo_full_name}...")
                if use_openai_similarity:
                    print(f"Using OpenAI embeddings with similarity threshold: {similarity_threshold}")
                else:
                    print(f"Using local word-based similarity detection (threshold: 0.5)")
                creator = CreatorAgent(github_token, azure_foundry_endpoint, None, repo_full_name, similarity_threshold=similarity_threshold, use_openai_similarity=use_openai_similarity)
                creator.create_issues()
            return 0

        jedimaster = JediMaster(
            github_token,
            azure_foundry_endpoint,
            just_label=args.just_label,
            use_topic_filter=use_topic_filter,
            process_prs=args.process_prs,
            auto_merge_reviewed=args.auto_merge_reviewed
        )

        # Process based on input type
        if args.user:
            print(f"Processing user: {args.user}")
            report = jedimaster.process_user(args.user)
            repo_names = [r.repo for r in report.results] if report.results else []
        else:
            print(f"Processing {len(args.repositories)} repositories...")
            report = jedimaster.process_repositories(args.repositories)
            repo_names = args.repositories

        auto_merge_results: List[PRRunResult] = []
        if args.auto_merge_reviewed:
            print("\nChecking for reviewed PRs to auto-merge...")
            for repo_name in repo_names:
                auto_merge_results.extend(jedimaster.merge_reviewed_pull_requests(repo_name))
            jedimaster.print_pr_results("AUTO-MERGE RESULTS", auto_merge_results)

        # Save and display results
        if args.save_report:
            filename = jedimaster.save_report(report, args.output)
            print(f"\nDetailed report saved to: {filename}")
        else:
            print("\nReport not saved (use --save-report to save to file)")
        summary_context = "issues"
        summary_pr_results: Optional[List[PRRunResult]] = None
        if args.process_prs:
            summary_context = "prs"
            summary_pr_results = report.pr_results
        elif args.auto_merge_reviewed:
            summary_context = "merge"
            summary_pr_results = auto_merge_results
        jedimaster.print_summary(report, context=summary_context, pr_results=summary_pr_results)
        if args.process_prs:
            jedimaster.print_pr_results("PULL REQUEST PROCESSING RESULTS", report.pr_results)
        return 0

    except Exception as e:
        print(f"Fatal error: {e}")
        return 1

def process_issues_api(input_data: dict) -> dict:
    """API function to process all issues from a list of repositories via Azure Functions or other callers."""
    github_token = os.getenv('GITHUB_TOKEN')
    azure_foundry_endpoint = os.getenv('AZURE_AI_FOUNDRY_ENDPOINT')
    if not github_token or not azure_foundry_endpoint:
        return {"error": "Missing GITHUB_TOKEN or AZURE_AI_FOUNDRY_ENDPOINT in environment"}
    try:
        just_label = _get_issue_action_from_env()
    except Exception as e:
        return {"error": str(e)}
    jm = JediMaster(github_token, azure_foundry_endpoint, just_label=just_label)
    repo_names = input_data.get('repo_names')
    if not repo_names or not isinstance(repo_names, list):
        return {"error": "Missing or invalid repo_names (should be a list) in input"}
    try:
        report = jm.process_repositories(repo_names)
        return asdict(report)
    except Exception as e:
        return {"error": str(e)}

def process_user_api(input_data: dict) -> dict:
    """API function to process all repositories for a user via Azure Functions or other callers."""
    github_token = os.getenv('GITHUB_TOKEN')
    azure_foundry_endpoint = os.getenv('AZURE_AI_FOUNDRY_ENDPOINT')
    if not github_token or not azure_foundry_endpoint:
        return {"error": "Missing GITHUB_TOKEN or AZURE_AI_FOUNDRY_ENDPOINT in environment"}
    try:
        just_label = _get_issue_action_from_env()
    except Exception as e:
        return {"error": str(e)}
    jm = JediMaster(github_token, azure_foundry_endpoint, just_label=just_label)
    username = input_data.get('username')
    if not username:
        return {"error": "Missing username in input"}
    try:
        report = jm.process_user(username)
        return asdict(report)
    except Exception as e:
        return {"error": str(e)}
def _get_issue_action_from_env() -> bool:
    """
    Retrieve and validate the ISSUE_ACTION environment variable.
    Returns True if action is 'label', False if 'assign'.
    Raises ValueError for invalid values.
    If not set, defaults to 'label'.
    """
    action = os.getenv('ISSUE_ACTION')
    if action is None:
        return True  # Default to labeling
    action = action.strip().lower()
    if action == 'label':
        return True
    elif action == 'assign':
        return False
    else:
        raise ValueError(f"Invalid ISSUE_ACTION: {action}. Must be 'assign' or 'label'.")

if __name__ == '__main__':
    exit(main())
