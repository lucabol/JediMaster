#!/usr/bin/env python3
"""
JediMaster - A tool to automatically assign GitHub issues to GitHub Copilot
based on LLM evaluation of issue suitability.
"""

import os
import json
import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, asdict, field
from datetime import datetime
import argparse
import requests

from github import Github, GithubException
from dotenv import load_dotenv


from decider import DeciderAgent, PRDeciderAgent
from creator import CreatorAgent







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

    def fetch_issues(self, repo_name: str):
        """Fetch all open issues for a repository."""
        repo = self.github.get_repo(repo_name)
        return repo.get_issues(state='open')

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
    def __init__(self, github_token: str, openai_api_key: str, just_label: bool = False, use_topic_filter: bool = True, process_prs: bool = False, auto_merge_reviewed: bool = False):
        self.github_token = github_token
        self.github = Github(github_token)
        self.decider = DeciderAgent(openai_api_key)
        self.pr_decider = PRDeciderAgent(openai_api_key)
        self.just_label = just_label
        self.use_topic_filter = use_topic_filter
        self.process_prs = process_prs
        self.auto_merge_reviewed = auto_merge_reviewed
        self.logger = self._setup_logger()

    def merge_reviewed_pull_requests(self, repo_name: str):
        """Merge PRs that are approved and have no conflicts with the base branch. If PR is a draft, mark as ready for review first."""
        results = []
        try:
            repo = self.github.get_repo(repo_name)
            pulls = list(repo.get_pulls(state='open'))
            self.logger.info(f"Found {len(pulls)} open PRs in {repo_name}")
            
            for pr in pulls:
                self.logger.info(f"Checking PR #{pr.number}: '{pr.title}' (draft: {getattr(pr, 'draft', 'unknown')}, mergeable: {pr.mergeable})")
                
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
                            results.append({'repo': repo_name, 'pr_number': pr.number, 'status': 'merge_error', 'error': f'Failed to get PR node ID: {query_result["errors"]}'})
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
                                results.append({'repo': repo_name, 'pr_number': pr.number, 'status': 'merge_error', 'error': f'Failed to mark as ready: {mutation_result["errors"]}'})
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
                        results.append({'repo': repo_name, 'pr_number': pr.number, 'status': 'merge_error', 'error': f'Failed to mark as ready for review: {e}'})
                        continue
                
                # Check for mergeability (no conflicts)
                if pr.mergeable is False:
                    self.logger.info(f"PR #{pr.number} in {repo_name} is approved but has conflicts, skipping merge")
                    # Add a comment to the PR about conflicts
                    try:
                        pr.create_issue_comment("Please resolve conflicts")
                        self.logger.info(f"Added conflict comment to PR #{pr.number} in {repo_name}")
                    except Exception as e:
                        self.logger.error(f"Failed to comment on PR #{pr.number} about conflicts: {e}")
                    results.append({'repo': repo_name, 'pr_number': pr.number, 'status': 'merge_error', 'error': 'Has merge conflicts'})
                    continue
                elif pr.mergeable is None:
                    self.logger.warning(f"PR #{pr.number} in {repo_name} has unknown mergeable state, skipping merge")
                    results.append({'repo': repo_name, 'pr_number': pr.number, 'status': 'merge_error', 'error': 'Unknown merge state'})
                    continue
                
                # Final check to ensure PR is not a draft before merging
                pr_draft_status = getattr(pr, 'draft', False)
                if pr_draft_status:
                    self.logger.error(f"PR #{pr.number} is still in draft state after processing, skipping merge")
                    results.append({'repo': repo_name, 'pr_number': pr.number, 'status': 'merge_error', 'error': 'PR still in draft state'})
                    continue
                
                self.logger.info(f"Attempting to merge PR #{pr.number} in {repo_name} (approved={approved}, mergeable={pr.mergeable}, draft={pr_draft_status})")
                try:
                    merge_result = pr.merge(merge_method='squash', commit_message=f"Auto-merged by JediMaster: {pr.title}")
                    if merge_result.merged:
                        self.logger.info(f"Successfully auto-merged PR #{pr.number} in {repo_name}")
                        results.append({'repo': repo_name, 'pr_number': pr.number, 'status': 'merged'})
                    else:
                        self.logger.error(f"Merge failed for PR #{pr.number} in {repo_name}: {merge_result.message}")
                        results.append({'repo': repo_name, 'pr_number': pr.number, 'status': 'merge_error', 'error': merge_result.message})
                except Exception as e:
                    self.logger.error(f"Failed to auto-merge PR #{pr.number} in {repo_name}: {e}")
                    results.append({'repo': repo_name, 'pr_number': pr.number, 'status': 'merge_error', 'error': str(e)})
        except Exception as e:
            self.logger.error(f"Error merging reviewed PRs in {repo_name}: {e}")
            results.append({'repo': repo_name, 'pr_number': 0, 'status': 'merge_error', 'error': str(e)})
        return results

    def process_pull_requests(self, repo_name: str):
        results = []
        try:
            repo = self.github.get_repo(repo_name)
            pulls = list(repo.get_pulls(state='open'))
            self.logger.info(f"Fetched {len(pulls)} open PRs from {repo_name}")
            for pr in pulls:
                # Get detailed review state information using GraphQL
                pr_data = self._get_pr_review_states(repo_name, pr.number)
                
                # Only process PRs that truly need review based on their current state
                if not self._should_process_pr(pr_data):
                    self.logger.info(f"Skipping PR #{pr.number} - not waiting for review or already has decision")
                    continue
                
                self.logger.info(f"Processing PR #{pr.number} - needs review")
                pr_text = f"Title: {pr.title}\n\nDescription:\n{pr.body or ''}\n\n"
                try:
                    diff = pr.diff_url
                    diff_content = ''
                    try:
                        diff_resp = requests.get(diff)
                        if diff_resp.status_code == 200:
                            diff_content = diff_resp.text
                    except Exception as e:
                        self.logger.warning(f"Could not fetch diff for PR #{pr.number}: {e}")
                    pr_text += f"Diff:\n{diff_content[:5000]}"
                except Exception as e:
                    self.logger.warning(f"Could not get diff for PR #{pr.number}: {e}")
                result = self.pr_decider.evaluate_pr(pr_text)
                print(result)
                
                # Double-check review state before taking action to avoid conflicts
                current_pr_data = self._get_pr_review_states(repo_name, pr.number)
                if not self._should_process_pr(current_pr_data):
                    self.logger.info(f"PR #{pr.number} state changed during processing, skipping action")
                    results.append({'repo': repo_name, 'pr_number': pr.number, 'status': 'skipped', 'reason': 'state_changed'})
                    continue
                
                if 'comment' in result:
                    try:
                        # Submit a CHANGES_REQUESTED review with the comment instead of just commenting
                        pr.create_review(event='REQUEST_CHANGES', body=result['comment'])
                        self.logger.info(f"Requested changes on PR #{pr.number} in {repo_name}")
                        results.append({'repo': repo_name, 'pr_number': pr.number, 'status': 'changes_requested', 'comment': result['comment']})
                    except Exception as e:
                        self.logger.error(f"Failed to request changes on PR #{pr.number}: {e}")
                        results.append({'repo': repo_name, 'pr_number': pr.number, 'status': 'error', 'error': str(e)})
                elif result.get('decision') == 'accept':
                    # Check if PR is already approved by someone else
                    reviews = current_pr_data.get('reviews', {}).get('nodes', [])
                    already_approved = any(review.get('state') == 'APPROVED' for review in reviews)
                    is_draft = current_pr_data.get('isDraft', False)
                    
                    if already_approved:
                        self.logger.info(f"PR #{pr.number} already has approval, skipping duplicate approval")
                        results.append({'repo': repo_name, 'pr_number': pr.number, 'status': 'skipped', 'reason': 'already_approved'})
                    else:
                        self.logger.info(f"PR #{pr.number} in {repo_name} can be accepted as-is.")
                        try:
                            # If it's a draft PR, mark as ready for review first
                            if is_draft:
                                self.logger.info(f"PR #{pr.number} is a draft, marking as ready for review before approval")
                                try:
                                    # Use the existing GraphQL mutation from merge_reviewed_pull_requests
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
                                            self.logger.error(f"GraphQL mutation errors: {mutation_result['errors']}")
                                        else:
                                            self.logger.info(f"Successfully marked draft PR #{pr.number} as ready for review")
                                    else:
                                        self.logger.warning(f"Could not get PR ID for draft PR #{pr.number}")
                                except Exception as e:
                                    self.logger.error(f"Failed to mark draft PR #{pr.number} as ready for review: {e}")
                                    # Continue with approval anyway
                            
                            pr.create_review(event='APPROVE', body='Automatically approved by JediMaster.')
                            self.logger.info(f"Approved PR #{pr.number} in {repo_name}.")
                            results.append({'repo': repo_name, 'pr_number': pr.number, 'status': 'approved', 'was_draft': is_draft})
                        except Exception as e:
                            self.logger.error(f"Failed to submit review for PR #{pr.number}: {e}")
                            results.append({'repo': repo_name, 'pr_number': pr.number, 'status': 'error', 'error': str(e)})
                else:
                    self.logger.warning(f"Unexpected PRDeciderAgent result for PR #{pr.number}: {result}")
                    results.append({'repo': repo_name, 'pr_number': pr.number, 'status': 'unknown', 'result': result})
        except Exception as e:
            self.logger.error(f"Error processing PRs in {repo_name}: {e}")
            results.append({'repo': repo_name, 'pr_number': 0, 'status': 'error', 'error': str(e)})
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
        
        # Check if there are pending review requests
        review_requests = pr_data.get('reviewRequests', {})
        has_pending_requests = review_requests.get('totalCount', 0) > 0
        self.logger.debug(f"PR is draft: {is_draft}, has {review_requests.get('totalCount', 0)} pending review requests")
        
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
        should_process = has_pending_requests or any(
            review.get('state') in ['COMMENTED', 'PENDING'] for review in latest_reviews.values()
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
            if pr_results:
                print("\nPULL REQUEST PROCESSING RESULTS:")
                for prr in pr_results:
                    print(prr)
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
        with open(out_filename, 'w') as f:
            json.dump(asdict(report), f, indent=2, ensure_ascii=False)
        self.logger.info(f"Report saved to {out_filename}")
        return out_filename

    def print_summary(self, report: ProcessingReport):
        print("\n" + "="*60)
        print("JEDIMASTER PROCESSING SUMMARY")
        print("="*60)
        print(f"Timestamp: {report.timestamp}")
        print(f"Total Issues Processed: {report.total_issues}")
        print(f"Assigned to Copilot: {report.assigned}")
        print(f"Labeled for Copilot: {report.labeled}")
        print(f"Not Assigned: {report.not_assigned}")
        print(f"Already Assigned: {report.already_assigned}")
        print(f"Errors: {report.errors}")
        print("="*60)
        if report.assigned > 0:
            print("\nISSUES ASSIGNED TO COPILOT:")
            for result in report.results:
                if result.status == 'assigned':
                    print(f"  - {result.repo}#{result.issue_number}: {result.title}")
                    print(f"    URL: {result.url}")
                    if result.reasoning:
                        print(f"    Reasoning: {result.reasoning}")
                    print()
        if report.labeled > 0:
            print("\nISSUES LABELED FOR COPILOT:")
            for result in report.results:
                if result.status == 'labeled':
                    print(f"  • {result.repo}#{result.issue_number}: {result.title}")
                    print(f"    URL: {result.url}")
                    if result.reasoning:
                        print(f"    Reasoning: {result.reasoning}")
                    print()
        if report.errors > 0:
            print("\nERRORS ENCOUNTERED:")
            for result in report.results:
                if result.status == 'error':
                    print(f"  • {result.repo}#{result.issue_number}: {result.error_message}")



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

    args = parser.parse_args()

    # Validate arguments
    if not args.user and not args.repositories:
        parser.error("Either specify repositories or use --user option")

    # Load environment variables from .env file (if it exists)
    load_dotenv()

    # Get API keys from environment (either from .env or system environment)
    github_token = os.getenv('GITHUB_TOKEN')
    openai_api_key = os.getenv('OPENAI_API_KEY')

    if not github_token:
        print("Error: GITHUB_TOKEN environment variable is required")
        print("Set it in .env file or as a system environment variable")
        return 1

    if not openai_api_key:
        print("Error: OPENAI_API_KEY environment variable is required")
        print("Set it in .env file or as a system environment variable")
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
                creator = CreatorAgent(github_token, openai_api_key, repo_full_name)
                results = creator.create_issues()
                # Always print the LLM conversation, even if error or invalid JSON, before any error/empty result message
                conv = getattr(creator, 'last_conversation', None)
                print("\n--- LLM Conversation ---")
                if conv:
                    print("[System Prompt]:\n" + conv.get("system", ""))
                    print("\n[User Prompt]:\n" + conv.get("user", ""))
                    print("\n[LLM Response]:\n" + str(conv.get("llm_response", "")))
                else:
                    print("[No conversation captured]")
                print("--- End Conversation ---\n")
                if not results:
                    print("No issues suggested by LLM.")
                for res in results:
                    if res.get('status') == 'created':
                        print(f"  - Created: {res['title']} -> {res['url']}")
                    else:
                        print(f"  - Failed: {res['title']} ({res.get('error', 'Unknown error')})")
            return 0

        jedimaster = JediMaster(
            github_token,
            openai_api_key,
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

        # Auto-merge reviewed PRs if requested
        if args.auto_merge_reviewed:
            print("\nChecking for reviewed PRs to auto-merge...")
            for repo_name in repo_names:
                merge_results = jedimaster.merge_reviewed_pull_requests(repo_name)
                for res in merge_results:
                    if res['status'] == 'merged':
                        print(f"  - Merged PR #{res['pr_number']} in {repo_name}")
                    elif res['status'] == 'merge_error':
                        print(f"  - Failed to merge PR #{res['pr_number']} in {repo_name}: {res['error']}")

        # Save and display results
        filename = jedimaster.save_report(report, args.output)
        jedimaster.print_summary(report)

        print(f"\nDetailed report saved to: {filename}")
        return 0

    except Exception as e:
        print(f"Fatal error: {e}")
        return 1

def process_issues_api(input_data: dict) -> dict:
    """API function to process all issues from a list of repositories via Azure Functions or other callers."""
    github_token = os.getenv('GITHUB_TOKEN')
    openai_api_key = os.getenv('OPENAI_API_KEY')
    if not github_token or not openai_api_key:
        return {"error": "Missing GITHUB_TOKEN or OPENAI_API_KEY in environment"}
    try:
        just_label = _get_issue_action_from_env()
    except Exception as e:
        return {"error": str(e)}
    jm = JediMaster(github_token, openai_api_key, just_label=just_label)
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
    openai_api_key = os.getenv('OPENAI_API_KEY')
    if not github_token or not openai_api_key:
        return {"error": "Missing GITHUB_TOKEN or OPENAI_API_KEY in environment"}
    try:
        just_label = _get_issue_action_from_env()
    except Exception as e:
        return {"error": str(e)}
    jm = JediMaster(github_token, openai_api_key, just_label=just_label)
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
