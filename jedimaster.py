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
            if result.get('assign'):
                if not self.just_label:
                    try:
                        copilot_user = self.github.get_user('github-copilot[bot]')
                        issue.add_to_assignees(copilot_user)
                        status = 'assigned'
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
    def __init__(self, github_token: str, openai_api_key: str, just_label: bool = False, use_topic_filter: bool = True, process_prs: bool = False):
        self.github_token = github_token
        self.github = Github(github_token)
        self.decider = DeciderAgent(openai_api_key)
        self.pr_decider = PRDeciderAgent(openai_api_key)
        self.just_label = just_label
        self.use_topic_filter = use_topic_filter
        self.process_prs = process_prs
        self.logger = self._setup_logger()

    def process_pull_requests(self, repo_name: str):
        results = []
        try:
            repo = self.github.get_repo(repo_name)
            pulls = list(repo.get_pulls(state='open'))
            self.logger.info(f"Fetched {len(pulls)} open PRs from {repo_name}")
            for pr in pulls:
                # Only process PRs that are in 'Waiting for review' state, approximated by having requested reviewers
                waiting_for_review = False
                try:
                    reviewers = list(getattr(pr, 'requested_reviewers', []))
                    team_reviewers = list(getattr(pr, 'requested_teams', []))
                    waiting_for_review = bool(reviewers or team_reviewers)
                except Exception as e:
                    self.logger.warning(f"Could not determine reviewers for PR #{pr.number}: {e}")
                if not waiting_for_review:
                    continue
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
                if 'comment' in result:
                    try:
                        pr.create_issue_comment(result['comment'])
                        self.logger.info(f"Commented on PR #{pr.number} in {repo_name}")
                        results.append({'repo': repo_name, 'pr_number': pr.number, 'status': 'commented', 'comment': result['comment']})
                    except Exception as e:
                        self.logger.error(f"Failed to comment on PR #{pr.number}: {e}")
                        results.append({'repo': repo_name, 'pr_number': pr.number, 'status': 'error', 'error': str(e)})
                elif result.get('decision') == 'accept':
                    self.logger.info(f"PR #{pr.number} in {repo_name} can be accepted as-is.")
                    results.append({'repo': repo_name, 'pr_number': pr.number, 'status': 'check-in'})
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
                issues = self.fetch_issues(repo_name)
                for issue in issues:
                    if issue.pull_request:
                        continue
                    result = self.process_issue(issue, repo_name)
                    all_results.append(result)
            except Exception as e:
                self.logger.error(f"Failed to process repository {repo_name}: {e}")
                all_results.append(IssueResult(
                    repo=repo_name,
                    issue_number=0,
                    title=f"Repository Error: {repo_name}",
                    url='',
                    status='error',
                    error_message=str(e)
                ))
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
        if self.process_prs and pr_results:
            print("\nPULL REQUEST PROCESSING RESULTS:")
            for prr in pr_results:
                print(prr)
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
        jedimaster = JediMaster(
            github_token,
            openai_api_key,
            just_label=args.just_label,
            use_topic_filter=use_topic_filter,
            process_prs=args.process_prs
        )

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
