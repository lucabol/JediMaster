#!/usr/bin/env python3
"""
JediMaster GraphQL - A tool to automatically assign GitHub issues to GitHub Copilot
using GraphQL API instead of REST API.
"""

import os
import json
import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, asdict
from datetime import datetime
import argparse

from dotenv import load_dotenv

from decider import DeciderAgent
from graphql_client import GitHubGraphQLClient
from jedimaster import IssueResult, ProcessingReport


class JediMasterGraphQL:
    """GraphQL-based version of JediMaster for processing GitHub issues."""
    
    def __init__(self, github_token: str, openai_api_key: str):
        """Initialize JediMasterGraphQL with required API keys."""
        self.graphql_client = GitHubGraphQLClient(github_token)
        self.decider = DeciderAgent(openai_api_key)
        self.logger = self._setup_logger()
        
    def _setup_logger(self) -> logging.Logger:
        """Set up logging configuration."""
        logger = logging.getLogger('jedimaster_graphql')
        logger.setLevel(logging.INFO)
        
        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            handler.setFormatter(formatter)
            logger.addHandler(handler)
            
        return logger
    
    def fetch_issues(self, repo_name: str) -> List[Dict[str, Any]]:
        """Fetch all open issues from a GitHub repository using GraphQL."""
        try:
            owner, name = repo_name.split('/')
            issues = self.graphql_client.get_issues(owner, name)
            self.logger.info(f"Fetched {len(issues)} issues from {repo_name} using GraphQL")
            return issues
        except Exception as e:
            self.logger.error(f"Error fetching issues from {repo_name} using GraphQL: {e}")
            raise
    
    def is_already_assigned_to_copilot(self, issue: Dict[str, Any]) -> bool:
        """Check if the issue is already assigned to GitHub Copilot."""
        try:
            # Check assignees
            assignees = issue.get('assignees', {}).get('nodes', [])
            for assignee in assignees:
                if 'copilot' in assignee.get('login', '').lower():
                    return True
            
            # Check labels
            labels = issue.get('labels', {}).get('nodes', [])
            for label in labels:
                if 'copilot' in label.get('name', '').lower():
                    return True
                    
            return False
        except Exception as e:
            self.logger.error(f"Error checking assignment status for issue #{issue.get('number', 'unknown')}: {e}")
            return False
    
    def assign_to_copilot_graphql(self, issue: Dict[str, Any], repo_name: str) -> bool:
        """Assign the issue to GitHub Copilot using GraphQL."""
        try:
            owner, name = repo_name.split('/')
            issue_id = issue.get('id')
            issue_number = issue.get('number')
            
            if not issue_id:
                self.logger.error(f"Issue #{issue_number} missing ID for GraphQL operations")
                return False
            
            # Get repository info for label creation
            repo_info = self.graphql_client.get_repository_info(owner, name)
            repo_id = repo_info.get('id')
            
            if not repo_id:
                self.logger.error(f"Could not get repository ID for {repo_name}")
                return False
            
            # Try to assign to Copilot user
            copilot_user_id = self.graphql_client.get_user_id("copilot")
            if copilot_user_id:
                try:
                    self.graphql_client.add_assignees_to_issue(issue_id, [copilot_user_id])
                    self.logger.info(f"Successfully added copilot as assignee for issue #{issue_number}")
                except Exception as e:
                    self.logger.warning(f"Could not add copilot as assignee for issue #{issue_number}: {e}")
            else:
                self.logger.warning(f"Could not find copilot user ID for issue #{issue_number}")
            
            # Handle GitHub Copilot label
            try:
                # Get existing labels
                existing_labels = self.graphql_client.get_repository_labels(owner, name)
                copilot_label_id = None
                
                # Look for existing github-copilot label
                for label in existing_labels:
                    if label.get('name', '').lower() == 'github-copilot':
                        copilot_label_id = label.get('id')
                        break
                
                # Create label if it doesn't exist
                if not copilot_label_id:
                    copilot_label_id = self.graphql_client.create_label(
                        repo_id=repo_id,
                        name="github-copilot",
                        color="0366d6",
                        description="Issue assigned to GitHub Copilot"
                    )
                
                # Add the label to the issue
                if copilot_label_id:
                    self.graphql_client.add_labels_to_issue(issue_id, [copilot_label_id])
                    self.logger.info(f"Successfully added github-copilot label to issue #{issue_number}")
                else:
                    self.logger.warning(f"Could not create or find github-copilot label for issue #{issue_number}")
                
                # Add a comment indicating the assignment
                comment = ("🤖 This issue has been automatically assigned to GitHub Copilot "
                          "based on LLM evaluation of its suitability for AI assistance. (via GraphQL)")
                self.graphql_client.add_comment_to_issue(issue_id, comment)
                
                self.logger.info(f"Successfully processed issue #{issue_number} for Copilot using GraphQL")
                return True
                
            except Exception as e:
                self.logger.error(f"Error processing issue #{issue_number} for Copilot using GraphQL: {e}")
                return False
                
        except Exception as e:
            self.logger.error(f"Unexpected error assigning issue #{issue.get('number', 'unknown')} using GraphQL: {e}")
            return False
    
    def process_issue(self, issue: Dict[str, Any], repo_name: str) -> IssueResult:
        """Process a single issue and return the result."""
        try:
            issue_number = issue.get('number', 0)
            issue_title = issue.get('title', 'Unknown')
            issue_url = issue.get('url', '')
            
            # Check if already assigned
            if self.is_already_assigned_to_copilot(issue):
                return IssueResult(
                    repo=repo_name,
                    issue_number=issue_number,
                    title=issue_title,
                    url=issue_url,
                    status='already_assigned'
                )
            
            # Get issue details for the decider
            issue_data = {
                'title': issue_title,
                'body': issue.get('body', ''),
                'labels': [label.get('name', '') for label in issue.get('labels', {}).get('nodes', [])],
                'comments': []
            }
            
            # Extract comments
            try:
                comments = issue.get('comments', {}).get('nodes', [])
                issue_data['comments'] = [comment.get('body', '') for comment in comments]
            except Exception as e:
                self.logger.warning(f"Could not extract comments for issue #{issue_number}: {e}")
            
            # Use the decider agent to evaluate the issue
            decision_result = self.decider.evaluate_issue(issue_data)
            
            if decision_result['decision'].lower() == 'yes':
                # Assign to Copilot using GraphQL
                if self.assign_to_copilot_graphql(issue, repo_name):
                    return IssueResult(
                        repo=repo_name,
                        issue_number=issue_number,
                        title=issue_title,
                        url=issue_url,
                        status='assigned',
                        reasoning=decision_result['reasoning']
                    )
                else:
                    return IssueResult(
                        repo=repo_name,
                        issue_number=issue_number,
                        title=issue_title,
                        url=issue_url,
                        status='error',
                        reasoning=decision_result['reasoning'],
                        error_message='Failed to assign to Copilot using GraphQL'
                    )
            else:
                return IssueResult(
                    repo=repo_name,
                    issue_number=issue_number,
                    title=issue_title,
                    url=issue_url,
                    status='not_assigned',
                    reasoning=decision_result['reasoning']
                )
                
        except Exception as e:
            error_msg = f"Error processing issue using GraphQL: {e}"
            self.logger.error(error_msg)
            return IssueResult(
                repo=repo_name,
                issue_number=issue.get('number', 0),
                title=issue.get('title', 'Unknown'),
                url=issue.get('url', ''),
                status='error',
                error_message=error_msg
            )
    
    def process_repositories(self, repo_names: List[str]) -> ProcessingReport:
        """Process all issues from the given repositories using GraphQL."""
        all_results = []
        
        for repo_name in repo_names:
            self.logger.info(f"Processing repository using GraphQL: {repo_name}")
            
            try:
                issues = self.fetch_issues(repo_name)
                
                for issue in issues:
                    result = self.process_issue(issue, repo_name)
                    all_results.append(result)
                    
            except Exception as e:
                self.logger.error(f"Failed to process repository {repo_name} using GraphQL: {e}")
                # Add an error result for the repository
                all_results.append(IssueResult(
                    repo=repo_name,
                    issue_number=0,
                    title=f"Repository Error: {repo_name} (GraphQL)",
                    url='',
                    status='error',
                    error_message=str(e)
                ))
        
        # Generate summary report
        assigned_count = sum(1 for r in all_results if r.status == 'assigned')
        not_assigned_count = sum(1 for r in all_results if r.status == 'not_assigned')
        already_assigned_count = sum(1 for r in all_results if r.status == 'already_assigned')
        error_count = sum(1 for r in all_results if r.status == 'error')
        
        report = ProcessingReport(
            total_issues=len(all_results),
            assigned=assigned_count,
            not_assigned=not_assigned_count,
            already_assigned=already_assigned_count,
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
            out_filename = f"jedimaster_graphql_report_{timestamp}.json"
        else:
            out_filename = filename
        with open(out_filename, 'w') as f:
            json.dump(asdict(report), f, indent=2, ensure_ascii=False)
        self.logger.info(f"GraphQL report saved to {out_filename}")
        return out_filename
    
    def print_summary(self, report: ProcessingReport):
        """Print a summary of the processing results."""
        print("\n" + "="*60)
        print("JEDIMASTER GRAPHQL PROCESSING SUMMARY")
        print("="*60)
        print(f"Timestamp: {report.timestamp}")
        print(f"Total Issues Processed: {report.total_issues}")
        print(f"Assigned to Copilot: {report.assigned}")
        print(f"Not Assigned: {report.not_assigned}")
        print(f"Already Assigned: {report.already_assigned}")
        print(f"Errors: {report.errors}")
        print("="*60)
        
        if report.assigned > 0:
            print("\nISSUES ASSIGNED TO COPILOT (via GraphQL):")
            for result in report.results:
                if result.status == 'assigned':
                    print(f"  • {result.repo}#{result.issue_number}: {result.title}")
                    print(f"    URL: {result.url}")
                    if result.reasoning:
                        print(f"    Reasoning: {result.reasoning[:100]}...")
                    print()
        
        if report.errors > 0:
            print("\nERRORS ENCOUNTERED:")
            for result in report.results:
                if result.status == 'error':
                    print(f"  • {result.repo}#{result.issue_number}: {result.error_message}")


def main():
    """Main entry point for the JediMaster GraphQL script."""
    parser = argparse.ArgumentParser(description='JediMaster GraphQL - Assign GitHub issues to Copilot using GraphQL')
    parser.add_argument('repositories', nargs='+', 
                       help='GitHub repositories to process (format: owner/repo)')
    parser.add_argument('--output', '-o', 
                       help='Output filename for the report (default: auto-generated)')
    parser.add_argument('--verbose', '-v', action='store_true',
                       help='Enable verbose logging')
    
    args = parser.parse_args()
    
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
        logging.getLogger('jedimaster_graphql').setLevel(logging.DEBUG)
    
    try:
        # Initialize JediMaster GraphQL
        jedimaster = JediMasterGraphQL(github_token, openai_api_key)
        
        # Process repositories
        print(f"Processing {len(args.repositories)} repositories using GraphQL...")
        report = jedimaster.process_repositories(args.repositories)
        
        # Save and display results
        filename = jedimaster.save_report(report, args.output)
        jedimaster.print_summary(report)
        
        print(f"\nDetailed GraphQL report saved to: {filename}")
        return 0
        
    except Exception as e:
        print(f"Fatal error: {e}")
        return 1


if __name__ == '__main__':
    exit(main())