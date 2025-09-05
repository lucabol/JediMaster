#!/usr/bin/env python3
"""
Example usage of JediMaster as a library.
"""

import os
import argparse
from dotenv import load_dotenv
from jedimaster import JediMaster
from creator import CreatorAgent

# Utility functions for repo/issue management


import requests


# Only create issues, no repo deletion/creation
def create_github_issue(token, owner, repo, title, body=""):
    url = f"https://api.github.com/repos/{owner}/{repo}/issues"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    data = {"title": title, "body": body}
    response = requests.post(url, headers=headers, json=data)
    if response.status_code == 201:
        return True, None
    else:
        return False, f"Error {response.status_code}: {response.text}"

def close_all_open_issues(token, owner, repo):
    """Close all open issues in the specified repo."""
    url = f"https://api.github.com/repos/{owner}/{repo}/issues?state=open&per_page=100"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        print(f"Failed to fetch issues: {response.status_code} {response.text}")
        return
    issues = response.json()
    for issue in issues:
        # Skip pull requests
        if 'pull_request' in issue:
            continue
        issue_number = issue['number']
        close_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}"
        close_resp = requests.patch(close_url, headers=headers, json={"state": "closed"})
        if close_resp.status_code == 200:
            print(f"Closed issue #{issue_number}")
        else:
            print(f"Failed to close issue #{issue_number}: {close_resp.status_code} {close_resp.text}")

def populate_repo_with_issues():
    """Close all existing issues, then add 10 issues (5 good for Copilot, 5 not) to lucabol/Hello-World."""
    github_token = os.getenv('GITHUB_TOKEN')
    if not github_token:
        print("GITHUB_TOKEN not set. Skipping issue creation.")
        exit(1)
    owner = "lucabol"
    repo = "Hello-World"
    print("Closing all open issues in the repo...")
    close_all_open_issues(github_token, owner, repo)
    print("Populating repo with issues...")
    # 5 issues suitable for Copilot (code modifications)
    copilot_issues = [
        ("Change the greeting to Italian in hello.c", "Modify hello.c so that it prints 'Ciao, Mondo!' instead of 'Hello, World!'."),
        ("Add a newline after the greeting in hello.c", "Update hello.c so that the output ends with a newline character."),
        ("Add a function to print a custom message in hello.c", "Refactor hello.c to include a function that prints a custom message passed as an argument."),
        ("Print the program's exit code in hello.c", "Modify hello.c to print the return value of main before exiting."),
        ("Use puts instead of printf in hello.c", "Change hello.c to use puts for printing the greeting instead of printf."),
    ]
    # 5 issues NOT suitable for Copilot (vague or complicated requests)
    non_copilot_issues = [
        ("Integrate a visual block-based editor for C code", "Add a web-based visual editor to the project that allows users to create and modify C code using drag-and-drop blocks, and then export the result to hello.c."),
        ("Implement a spreadsheet-like interface for code metrics", "Create a spreadsheet tool within the project that can analyze hello.c and display various code metrics in a tabular, interactive format."),
        ("Enable real-time collaborative editing for hello.c", "Allow multiple users to edit hello.c simultaneously in real time, with live updates and conflict resolution."),
        ("Add support for voice-driven code editing in hello.c", "Integrate a feature that lets users edit hello.c using voice commands, including code insertion, navigation, and refactoring."),
        ("Create a plugin system for extending hello.c functionality", "Design and implement a plugin architecture so that external developers can add new features or transformations to hello.c without modifying the core file directly."),
    ]

    expected_issues = len(copilot_issues) + len(non_copilot_issues)
    for title, body in copilot_issues + non_copilot_issues:
        ok, err = create_github_issue(github_token, owner, repo, title, body)
        print(f"Created issue '{title}': {'OK' if ok else 'FAILED'}{f' - {err}' if err else ''}")

    # Wait until all issues are present in the repo
    import time
    def get_open_issues_count(token, owner, repo):
        url = f"https://api.github.com/repos/{owner}/{repo}/issues?state=open&per_page=100"
        headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            print(f"Failed to fetch issues: {response.status_code} {response.text}")
            return None
        # Exclude pull requests
        issues = [issue for issue in response.json() if 'pull_request' not in issue]
        return len(issues)

    print(f"Waiting for all {expected_issues} issues to appear in the repo...")
    retries = 0
    while True:
        count = get_open_issues_count(github_token, owner, repo)
        if count is not None and count >= expected_issues:
            print(f"All {expected_issues} issues are now present in the repo.")
            break
        retries += 1
        if retries > 20:
            print(f"Timeout waiting for issues to appear. Only {count} found.")
            break
        print(f"Found {count} issues, waiting...")
        time.sleep(2)

def main():
    """Example of using JediMaster programmatically."""
      # Parse command-line arguments
    parser = argparse.ArgumentParser(description='Example usage of JediMaster')
    parser.add_argument('--just-label', action='store_true', default=True,
                       help='Only add labels to issues, do not assign them (default: True)')
    parser.add_argument('--assign', action='store_true',
                       help='Assign issues to Copilot instead of just labeling')
    parser.add_argument('--use-file-filter', action='store_true',
                       help='Use .coding_agent file filtering instead of topic filtering (slower but backwards compatible)')
    parser.add_argument('--process-prs', action='store_true',
                       help='Process open pull requests with PRDeciderAgent (add comments or log check-in readiness)')
    parser.add_argument('--repos', type=str, default='lucabol/Hello-World',
                       help='Comma-separated list of repositories to process (default: lucabol/Hello-World)')
    parser.add_argument('--user', type=str, default=None,
                       help='Process all repositories for a given user (overrides --repos if provided)')
    parser.add_argument('--auto-merge-reviewed', action='store_true',
                       help='Automatically merge reviewed PRs with no conflicts')
    parser.add_argument('--create-issues', action='store_true',
                       help='Use CreatorAgent to suggest and open new issues in the specified repositories')
    parser.add_argument('--populate-repo', action='store_true',
                          help='Populate the repo with example issues before running.')
    args = parser.parse_args()

    if args.populate_repo:
        populate_repo_with_issues()
        return

    # Determine just_label value (--assign overrides the default)
    just_label = not args.assign if args.assign else args.just_label

    # Determine filtering method
    use_topic_filter = not args.use_file_filter  # Default to topic filtering unless file filtering is explicitly requested

    # Load environment variables from .env file (if it exists)
    load_dotenv()

    # Get API keys from environment (either from .env or system environment)
    github_token = os.getenv('GITHUB_TOKEN')
    openai_api_key = os.getenv('OPENAI_API_KEY')

    if not github_token or not openai_api_key:
        print("Please set GITHUB_TOKEN and OPENAI_API_KEY environment variables")
        print("Either in a .env file or as system environment variables")
        return

    # If --create-issues is set, use CreatorAgent for each repo
    if getattr(args, 'create_issues', False):
        if args.user:
            print("--create-issues does not support --user mode. Please specify repositories explicitly.")
            return
        repo_names = [r.strip() for r in args.repos.split(',') if r.strip()]
        for repo_full_name in repo_names:
            print(f"\n[CreatorAgent] Suggesting and opening issues for {repo_full_name}...")
            creator = CreatorAgent(github_token, openai_api_key, repo_full_name)
            results = creator.create_issues()
            for res in results:
                if res.get('status') == 'created':
                    print(f"  - Created: {res['title']} -> {res['url']}")
                else:
                    print(f"  - Failed: {res['title']} ({res.get('error', 'Unknown error')})")
        return

    # Initialize JediMaster
    jedimaster = JediMaster(
        github_token,
        openai_api_key,
        just_label=just_label,
        use_topic_filter=use_topic_filter,
        process_prs=args.process_prs,
        auto_merge_reviewed=getattr(args, 'auto_merge_reviewed', False)
    )

    # Show which mode we're using
    mode = "labeling only" if just_label else "assigning"
    filter_method = "topic 'managed-by-coding-agent'" if use_topic_filter else ".coding_agent file"
    print(f"JediMaster mode: {mode}")
    print(f"Filtering method: {filter_method}")

    # Decide processing mode
    if args.user:
        username = args.user
        print(f"Processing user: {username}")
        print(f"Looking for repositories with {filter_method}...")
        report = jedimaster.process_user(username)
        repo_names = [r.repo for r in report.results] if report.results else []
    else:
        repo_names = [r.strip() for r in args.repos.split(',') if r.strip()]
        print(f"Processing repositories: {repo_names}")
        report = jedimaster.process_repositories(repo_names)

    # Auto-merge reviewed PRs if requested
    if getattr(args, 'auto_merge_reviewed', False):
        print("\nChecking for reviewed PRs to auto-merge...")
        for repo_name in repo_names:
            merge_results = jedimaster.merge_reviewed_pull_requests(repo_name)
            for res in merge_results:
                if res['status'] == 'merged':
                    print(f"  - Merged PR #{res['pr_number']} in {repo_name}")
                elif res['status'] == 'merge_error':
                    print(f"  - Failed to merge PR #{res['pr_number']} in {repo_name}: {res['error']}")

    # Save report
    filename = jedimaster.save_report(report, "example_report.json")

    # Print summary
    jedimaster.print_summary(report)

    print(f"\nReport saved to: {filename}")

    # Example of accessing individual results
    print(f"\nDetailed results:")
    for result in report.results:
        print(f"  {result.repo}#{result.issue_number}: {result.status}")
        if result.reasoning:
            print(f"    Reasoning: {result.reasoning[:100]}...")

if __name__ == '__main__':
    main()
