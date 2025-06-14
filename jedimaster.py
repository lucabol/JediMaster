#!/usr/bin/env python3
"""
JediMaster - A tool to automatically assign GitHub issues to GitHub Copilot
based on LLM evaluation of issue suitability.
"""

import os
import json
import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, asdict
from datetime import datetime
import argparse
import requests

from github import Github, GithubException
from dotenv import load_dotenv

from decider import DeciderAgent


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
    total_issues: int
    assigned: int
    not_assigned: int
    already_assigned: int
    labeled: int
    errors: int
    results: List[IssueResult]
    timestamp: str


class JediMaster:
    """Main class for processing GitHub issues and assigning them to Copilot."""
    
    def __init__(self, github_token: str, openai_api_key: str, just_label: bool = False, use_topic_filter: bool = True):
        """Initialize JediMaster with required API keys."""
        self.github_token = github_token
        self.github = Github(github_token)
        self.decider = DeciderAgent(openai_api_key)
        self.just_label = just_label
        self.use_topic_filter = use_topic_filter
        self.logger = self._setup_logger()
        
    def _setup_logger(self) -> logging.Logger:
        """Set up logging configuration."""
        logger = logging.getLogger('jedimaster')
        logger.setLevel(logging.INFO)
        
        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            
        return logger
    
    def _graphql_request(self, query: str, variables: Optional[Dict] = None) -> Dict:
        """Make a GraphQL request to GitHub API."""
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
    
    def _get_issue_id_and_bot_id(self, repo_owner: str, repo_name: str, issue_number: int) -> tuple[Optional[str], Optional[str]]:
        """Get the GitHub node ID for an issue and find the Copilot bot using suggestedActors."""
        # Query to get issue ID and suggested actors that can be assigned
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
            
            # Look for the Copilot coding agent
            bot_id = None
            suggested_actors = data["repository"]["suggestedActors"]["nodes"]
            
            # Check if Copilot coding agent is enabled - it should be the first suggested actor
            if suggested_actors:
                first_actor = suggested_actors[0]
                if first_actor["login"] == "copilot-swe-agent":
                    bot_id = first_actor["id"]
                    self.logger.info(f"Found Copilot coding agent: {first_actor['login']} (type: {first_actor.get('__typename', 'Unknown')})")
                else:
                    # If first actor is not copilot-swe-agent, search through all suggested actors
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
            
            # Check if assignment was successful
            assignees = result["data"]["replaceActorsForAssignable"]["assignable"]["assignees"]["nodes"]
            assigned_logins = [assignee["login"] for assignee in assignees]
            
            self.logger.info(f"Successfully assigned issue. Current assignees: {assigned_logins}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error assigning issue via GraphQL: {e}")
            return False
    
    def fetch_issues(self, repo_name: str) -> List[Any]:
        """Fetch all open issues from a GitHub repository."""
        try:
            repo = self.github.get_repo(repo_name)
            issues = list(repo.get_issues(state='open'))
            self.logger.info(f"Fetched {len(issues)} issues from {repo_name}")
            return issues
        except GithubException as e:
            self.logger.error(f"Error fetching issues from {repo_name}: {e}")
            raise
    
    def is_already_assigned_to_copilot(self, issue) -> bool:
        """Check if the issue is already assigned to GitHub Copilot."""
        try:
            # Check if any assignee contains 'copilot' (case insensitive)
            for assignee in issue.assignees:
                if 'copilot' in assignee.login.lower():
                    return True
            
            # Check labels for copilot-related labels
            for label in issue.labels:
                if 'copilot' in label.name.lower():
                    return True                    
            return False
        except Exception as e:
            self.logger.error(f"Error checking assignment status for issue #{issue.number}: {e}")
            return False
    
    def assign_to_copilot(self, issue) -> bool:
        """Assign the issue to GitHub Copilot."""
        try:
            # Try to find a copilot user/bot in the repository
            repo = issue.repository

            # Add Copilot as an assignee using GraphQL API
            try:
                # Extract repo owner and name from the repository
                repo_full_name = repo.full_name.split('/')
                repo_owner = repo_full_name[0]
                repo_name = repo_full_name[1]
                
                # Get issue ID and bot ID using GraphQL
                issue_id, bot_id = self._get_issue_id_and_bot_id(repo_owner, repo_name, issue.number)
                
                if issue_id and bot_id:
                    # Assign using GraphQL mutation
                    success = self._assign_issue_via_graphql(issue_id, bot_id)
                    if success:
                        self.logger.info(f"Successfully added copilot as assignee for issue #{issue.number}")
                    else:
                        self.logger.warning(f"GraphQL assignment failed for issue #{issue.number}")
                else:
                    self.logger.warning(f"Could not find issue ID or suitable bot for issue #{issue.number}")
                    
            except Exception as e:
                self.logger.warning(f"Could not add copilot as assignee for issue #{issue.number}: {e}")

            # Add a label indicating this issue is for Copilot
            try:
                # Check if the label exists, create if it doesn't
                copilot_label = None
                for label in repo.get_labels():
                    if label.name.lower() == 'github-copilot':
                        copilot_label = label
                        break

                if not copilot_label:
                    copilot_label = repo.create_label(
                        name="github-copilot",
                        color="0366d6",
                        description="Issue assigned to GitHub Copilot"
                    )

                # Add the label to the issue
                issue.add_to_labels(copilot_label)

                # Add a comment indicating the assignment
                comment = ("🤖 This issue has been automatically assigned to GitHub Copilot "
                          "based on LLM evaluation of its suitability for AI assistance.")
                issue.create_comment(comment)

                self.logger.info(f"Successfully processed issue #{issue.number} for Copilot")
                return True

            except GithubException as e:
                self.logger.error(f"Error processing issue #{issue.number} for Copilot: {e}")
                return False

        except Exception as e:
            self.logger.error(f"Unexpected error assigning issue #{issue.number}: {e}")
            return False
    
    def add_copilot_label(self, issue) -> bool:
        """Add the GitHub Copilot label to an issue without assigning it."""
        try:
            repo = issue.repository
            
            # Check if the label exists, create if it doesn't
            copilot_label = None
            for label in repo.get_labels():
                if label.name.lower() == 'github-copilot':
                    copilot_label = label
                    break

            if not copilot_label:
                copilot_label = repo.create_label(
                    name="github-copilot",
                    color="0366d6",
                    description="Issue suitable for GitHub Copilot"
                )

            # Add the label to the issue
            issue.add_to_labels(copilot_label)

            # Add a comment indicating the labeling
            comment = ("🤖 This issue has been automatically labeled for GitHub Copilot "
                      "based on LLM evaluation of its suitability for AI assistance.")
            issue.create_comment(comment)

            self.logger.info(f"Successfully labeled issue #{issue.number} for Copilot")
            return True

        except GithubException as e:
            self.logger.error(f"Error labeling issue #{issue.number} for Copilot: {e}")
            return False
        except Exception as e:
            self.logger.error(f"Unexpected error labeling issue #{issue.number}: {e}")
            return False
    
    def process_issue(self, issue, repo_name: str) -> IssueResult:
        """Process a single issue and return the result."""
        try:
            # Check if already assigned
            if self.is_already_assigned_to_copilot(issue):
                return IssueResult(
                    repo=repo_name,
                    issue_number=issue.number,
                    title=issue.title,
                    url=issue.html_url,
                    status='already_assigned'
                )
            
            # Get issue details for the decider
            issue_data = {
                'title': issue.title,
                'body': issue.body or '',
                'labels': [label.name for label in issue.labels],
                'comments': []
            }
            
            # Fetch comments (limit to last 10 to avoid too much data)
            try:
                comments = list(issue.get_comments())[-10:]
                issue_data['comments'] = [comment.body for comment in comments]
            except Exception as e:
                self.logger.warning(f"Could not fetch comments for issue #{issue.number}: {e}")
              # Use the decider agent to evaluate the issue
            decision_result = self.decider.evaluate_issue(issue_data)
            
            if decision_result['decision'].lower() == 'yes':
                if self.just_label:
                    # Just add label, don't assign
                    if self.add_copilot_label(issue):
                        return IssueResult(
                            repo=repo_name,
                            issue_number=issue.number,
                            title=issue.title,
                            url=issue.html_url,
                            status='labeled',
                            reasoning=decision_result['reasoning']
                        )
                    else:
                        return IssueResult(
                            repo=repo_name,
                            issue_number=issue.number,
                            title=issue.title,
                            url=issue.html_url,
                            status='error',
                            reasoning=decision_result['reasoning'],
                            error_message='Failed to label for Copilot'
                        )
                else:
                    # Assign to Copilot
                    if self.assign_to_copilot(issue):
                        return IssueResult(
                            repo=repo_name,
                            issue_number=issue.number,
                            title=issue.title,
                            url=issue.html_url,
                            status='assigned',
                            reasoning=decision_result['reasoning']
                        )
                    else:
                        return IssueResult(
                            repo=repo_name,
                            issue_number=issue.number,
                            title=issue.title,
                            url=issue.html_url,
                            status='error',
                            reasoning=decision_result['reasoning'],
                            error_message='Failed to assign to Copilot'
                        )
            else:
                return IssueResult(
                    repo=repo_name,
                    issue_number=issue.number,
                    title=issue.title,
                    url=issue.html_url,
                    status='not_assigned',
                    reasoning=decision_result['reasoning']
                )
                
        except Exception as e:
            error_msg = f"Error processing issue #{issue.number}: {e}"
            self.logger.error(error_msg)
            return IssueResult(
                repo=repo_name,
                issue_number=issue.number,
                title=getattr(issue, 'title', 'Unknown'),
                url=getattr(issue, 'html_url', ''),
                status='error',
                error_message=error_msg
            )
    
    def _repo_has_topic(self, repo, topic: str) -> bool:
        """
        Check if a repository has a specific topic.
        This is much faster than file existence checks as topics are already loaded.
        """
        try:
            topics = repo.get_topics()
            return topic in topics
        except Exception as e:
            self.logger.warning(f"Error checking topics in {repo.full_name}: {e}")
            return False

    def _file_exists_in_repo(self, repo, file_path: str) -> bool:
        """
        Efficiently check if a file exists in a repository using HEAD request.
        This is much faster than get_contents() as it doesn't download file content.
        """
        try:
            # Use HEAD request to check file existence without downloading content
            url = f"https://api.github.com/repos/{repo.full_name}/contents/{file_path}"
            headers = {
                "Authorization": f"Bearer {self.github_token}",
                "Accept": "application/vnd.github.v3+json"
            }
            response = requests.head(url, headers=headers)
            return response.status_code == 200
        except Exception as e:
            self.logger.warning(f"Error checking {file_path} in {repo.full_name}: {e}")
            return False

    def process_user(self, username: str) -> ProcessingReport:
        """
        Process all repositories for a given user based on the configured filter method.
        
        Args:
            username: GitHub username to process
            
        Returns:
            ProcessingReport: Summary of all issues processed across user's repositories
        """
        filter_method = "topic 'managed-by-coding-agent'" if self.use_topic_filter else ".coding_agent file"
        self.logger.info(f"Processing user: {username} (filtering by {filter_method})")
        
        try:
            # Get all repositories for the user
            user = self.github.get_user(username)
            all_repos = user.get_repos()
            
            # Filter repositories based on configured method
            filtered_repos = []
            
            for repo in all_repos:
                if self.use_topic_filter:
                    # Use topic-based filtering (faster)
                    if self._repo_has_topic(repo, "managed-by-coding-agent"):
                        filtered_repos.append(repo.full_name)
                        self.logger.info(f"Found topic 'managed-by-coding-agent' in repository: {repo.full_name}")
                else:
                    # Use file-based filtering (slower but backwards compatible)
                    if self._file_exists_in_repo(repo, ".coding_agent"):
                        filtered_repos.append(repo.full_name)
                        self.logger.info(f"Found .coding_agent file in repository: {repo.full_name}")            
            if not filtered_repos:
                filter_desc = "topic 'managed-by-coding-agent'" if self.use_topic_filter else ".coding_agent file"
                self.logger.info(f"No repositories found with {filter_desc} for user {username}")
                return ProcessingReport(
                    total_issues=0,
                    assigned=0,
                    not_assigned=0,
                    already_assigned=0,
                    labeled=0,
                    errors=0,
                    results=[],
                    timestamp=datetime.now().isoformat()
                )
            
            filter_desc = "topic 'managed-by-coding-agent'" if self.use_topic_filter else ".coding_agent file"
            self.logger.info(f"Found {len(filtered_repos)} repositories with {filter_desc}")
            
            # Process the filtered repositories
            return self.process_repositories(filtered_repos)
            
        except GithubException as e:
            error_msg = f"Error accessing user {username}: {e}"
            self.logger.error(error_msg)
            return ProcessingReport(
                total_issues=0,
                assigned=0,
                not_assigned=0,
                already_assigned=0,
                labeled=0,
                errors=1,
                results=[IssueResult(
                    repo=f"user/{username}",
                    issue_number=0,
                    title=f"User Error: {username}",
                    url='',
                    status='error',
                    error_message=error_msg
                )],
                timestamp=datetime.now().isoformat()
            )
        except Exception as e:
            error_msg = f"Unexpected error processing user {username}: {e}"
            self.logger.error(error_msg)
            return ProcessingReport(
                total_issues=0,
                assigned=0,
                not_assigned=0,
                already_assigned=0,
                labeled=0,
                errors=1,
                results=[IssueResult(
                    repo=f"user/{username}",
                    issue_number=0,
                    title=f"User Error: {username}",
                    url='',
                    status='error',
                    error_message=error_msg
                )],
                timestamp=datetime.now().isoformat()
            )

    def process_repositories(self, repo_names: List[str]) -> ProcessingReport:
        """Process all issues from the given repositories."""
        all_results = []
        
        for repo_name in repo_names:
            self.logger.info(f"Processing repository: {repo_name}")
            
            try:
                issues = self.fetch_issues(repo_name)
                
                for issue in issues:
                    # Skip pull requests (GitHub API returns PRs as issues)
                    if issue.pull_request:
                        continue
                        
                    result = self.process_issue(issue, repo_name)
                    all_results.append(result)
                    
            except Exception as e:
                self.logger.error(f"Failed to process repository {repo_name}: {e}")
                # Add an error result for the repository
                all_results.append(IssueResult(
                    repo=repo_name,
                    issue_number=0,
                    title=f"Repository Error: {repo_name}",
                    url='',
                    status='error',
                    error_message=str(e)
                ))
          # Generate summary report
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
            results=all_results,
            timestamp=datetime.now().isoformat()
        )
        
        return report
    
    def save_report(self, report: ProcessingReport, filename: Optional[str] = None) -> str:
        """Save the processing report to a JSON file."""
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
        """Print a summary of the processing results."""
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
    parser = argparse.ArgumentParser(description='JediMaster - Label or assign GitHub issues to Copilot')
    
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
    
    try:        # Initialize JediMaster
        use_topic_filter = not args.use_file_filter  # Default to topic filtering unless file filtering is explicitly requested
        jedimaster = JediMaster(github_token, openai_api_key, just_label=args.just_label, use_topic_filter=use_topic_filter)
        
        # Process based on input type
        if args.user:
            print(f"Processing user: {args.user}")
            report = jedimaster.process_user(args.user)
        else:
            print(f"Processing {len(args.repositories)} repositories...")
            report = jedimaster.process_repositories(args.repositories)
        
        # Save and display results
        filename = jedimaster.save_report(report, args.output)
        jedimaster.print_summary(report)
        
        print(f"\nDetailed report saved to: {filename}")
        return 0
        
    except Exception as e:
        print(f"Fatal error: {e}")
        return 1


if __name__ == '__main__':
    exit(main())
