#!/usr/bin/env python3
"""
Example usage of JediMaster as a library.
"""

import os
import argparse
import base64
from dotenv import load_dotenv
from jedimaster import JediMaster
from creator import CreatorAgent
from reset_utils import reset_repository

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

def delete_all_branches_except_main(token, owner, repo):
    """Delete all branches in the repository except 'main'."""
    url = f"https://api.github.com/repos/{owner}/{repo}/branches"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    
    # First get the list of all branches
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        print(f"Failed to fetch branches: {response.status_code} {response.text}")
        return
    
    branches = response.json()
    print(f"Found {len(branches)} branches in {owner}/{repo}")
    
    # Delete all branches except 'main'
    for branch in branches:
        branch_name = branch['name']
        if branch_name == 'main':
            print(f"Skipping main branch: {branch_name}")
            continue
        
        # Delete the branch
        delete_url = f"https://api.github.com/repos/{owner}/{repo}/git/refs/heads/{branch_name}"
        delete_resp = requests.delete(delete_url, headers=headers)
        
        if delete_resp.status_code == 204:
            print(f"Deleted branch: {branch_name}")
        elif delete_resp.status_code == 422:
            # Branch might be protected or be the default branch
            print(f"Cannot delete branch {branch_name}: likely protected or default branch")
        else:
            print(f"Failed to delete branch {branch_name}: {delete_resp.status_code} {delete_resp.text}")

def update_github_file(token, owner, repo, path, new_content, commit_message):
    """Update a file in a GitHub repository."""
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    headers = {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}

    # Get the current file to get its SHA
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        # If the file doesn't exist, we can't get a SHA, but we can create it.
        # The API for creation is the same, just without the 'sha' field.
        file_sha = None
    else:
        file_sha = response.json()['sha']

    # Encode the new content in base64
    encoded_content = base64.b64encode(new_content.encode('utf-8')).decode('utf-8')

    # Prepare the data for the update
    data = {
        "message": commit_message,
        "content": encoded_content,
    }
    if file_sha:
        data["sha"] = file_sha

    # Make the PUT request to update the file
    update_response = requests.put(url, headers=headers, json=data)

    if update_response.status_code == 200 or update_response.status_code == 201:
        return True, None
    else:
        return False, f"Error {update_response.status_code}: {update_response.text}"

def populate_repo_with_issues():
    """Add 10 example issues (5 good for Copilot, 5 not) to lucabol/Hello-World. Does not close or reset repo state."""
    github_token = os.getenv('GITHUB_TOKEN')
    if not github_token:
        print("GITHUB_TOKEN not set. Skipping issue creation.")
        exit(1)
    owner = "lucabol"
    repo = "Hello-World"


    # Issue deletion is now handled by --delete-issues, not here
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
    parser = argparse.ArgumentParser(description='Example usage of JediMaster - Label or assign GitHub issues to Copilot and optionally process PRs')

    # Create mutually exclusive group for repositories vs user (similar to jedimaster.py)
    group = parser.add_mutually_exclusive_group(required=False)  # Not required for example script
    group.add_argument('repositories', nargs='*', default=['lucabol/Hello-World'],
                       help='GitHub repositories to process (format: owner/repo)')
    group.add_argument('--user', '-u',
                       help='GitHub username to process (will process repos with topic "managed-by-coding-agent" or .coding_agent file)')

    # Core parameters matching jedimaster.py
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

    # Example-specific parameters
    parser.add_argument('--assign', action='store_true',
                       help='Assign issues to Copilot instead of just labeling (overrides --just-label)')
    parser.add_argument('--populate-issues', action='store_true',
                       help='Populate the repo with example issues before running.')
    parser.add_argument('--reset-repo', action='store_true',
                       help='Reset the repo: close all issues and PRs, delete all branches except main, reset hello.c, and delete all files except hello.c, .gitignore, README.md, and .github directory.')

    args = parser.parse_args()

    # Validate arguments (similar to jedimaster.py)
    if not args.user and not args.repositories:
        # For example.py, we set a default repository, so this shouldn't happen
        # but we'll keep the check for consistency
        pass

    if args.reset_repo:
        if args.user:
            print("--reset-repo does not support --user mode. Please specify repositories explicitly.")
            return
        github_token = os.getenv('GITHUB_TOKEN')
        if not github_token:
            print("GITHUB_TOKEN not set. Cannot reset repo.")
            return
        repo_names = args.repositories
        import logging
        logger = logging.getLogger('reset')
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            logger.addHandler(logging.StreamHandler())
        for repo_full_name in repo_names:
            print(f"Resetting {repo_full_name}...")
            summary = reset_repository(github_token, repo_full_name, logger)
            print(summary)
        return

    if args.populate_issues:
        populate_repo_with_issues()
        return

    # Determine just_label value (--assign overrides --just-label)
    just_label = not args.assign if args.assign else args.just_label

    # Determine filtering method
    use_topic_filter = not args.use_file_filter  # Default to topic filtering unless file filtering is explicitly requested

    # Load environment variables from .env file (if it exists)
    load_dotenv()

    # Get credentials from environment (either from .env or system environment)
    github_token = os.getenv('GITHUB_TOKEN')
    azure_foundry_endpoint = os.getenv('AZURE_AI_FOUNDRY_ENDPOINT')

    if not github_token or not azure_foundry_endpoint:
        print("Please set GITHUB_TOKEN and AZURE_AI_FOUNDRY_ENDPOINT environment variables")
        print("Either in a .env file or as system environment variables")
        print("Authentication to Azure AI Foundry will use managed identity (DefaultAzureCredential)")
        return

    # Set up logging level (like jedimaster.py)
    if args.verbose:
        import logging
        logging.getLogger('jedimaster').setLevel(logging.DEBUG)

    # If --create-issues is set, use CreatorAgent for each repo
    if getattr(args, 'create_issues', False):
        if args.user:
            print("--create-issues does not support --user mode. Please specify repositories explicitly.")
            return
        repo_names = args.repositories  # Now using positional argument
        for repo_full_name in repo_names:
            print(f"\n[CreatorAgent] Suggesting and opening issues for {repo_full_name}...")
            creator = CreatorAgent(github_token, azure_foundry_endpoint, None, repo_full_name)
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
        azure_foundry_endpoint,
        None,  # No API key needed with managed authentication
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

    # Auto-merge reviewed PRs if requested (PR-only operation, skips issue processing)
    if getattr(args, 'auto_merge_reviewed', False):
        print("Auto-merge mode: Only checking PRs for auto-merge, skipping issue processing...")
        
        # Determine repos to check without processing issues
        if args.user:
            username = args.user
            print(f"Finding repositories for user: {username}")
            print(f"Looking for repositories with {filter_method}...")
            try:
                user = jedimaster.github.get_user(username)
                all_repos = user.get_repos()
                repo_names = []
                for repo in all_repos:
                    if jedimaster.use_topic_filter:
                        if jedimaster._repo_has_topic(repo, "managed-by-coding-agent"):
                            repo_names.append(repo.full_name)
                            print(f"Found topic 'managed-by-coding-agent' in repository: {repo.full_name}")
                    else:
                        if jedimaster._file_exists_in_repo(repo, ".coding_agent"):
                            repo_names.append(repo.full_name)
                            print(f"Found .coding_agent file in repository: {repo.full_name}")
                if not repo_names:
                    filter_desc = "topic 'managed-by-coding-agent'" if jedimaster.use_topic_filter else ".coding_agent file"
                    print(f"No repositories found with {filter_desc} for user {username}")
                    return
            except Exception as e:
                print(f"Error accessing user {username}: {e}")
                return
        else:
            repo_names = args.repositories  # Now using positional argument
        
        print(f"Checking {len(repo_names)} repositories for auto-merge candidates...")
        
        # Only do auto-merge, no issue processing
        for repo_name in repo_names:
            merge_results = jedimaster.merge_reviewed_pull_requests(repo_name)
            for res in merge_results:
                if res['status'] == 'merged':
                    pr_title = res.get('pr_title', 'Unknown Title')
                    print(f"  - Merged PR #{res['pr_number']}: {pr_title} in {repo_name}")
                elif res['status'] == 'merge_error':
                    pr_title = res.get('pr_title', 'Unknown Title')
                    print(f"  - Failed to merge PR #{res['pr_number']}: {pr_title} in {repo_name}: {res['error']}")
        
        print(f"\nAuto-merge complete.")
        return
    
    # Normal processing mode (issues/PRs based on other flags)
    if args.user:
        username = args.user
        print(f"Processing user: {username}")
        print(f"Looking for repositories with {filter_method}...")
        report = jedimaster.process_user(username)
        repo_names = [r.repo for r in report.results] if report.results else []
    else:
        repo_names = args.repositories  # Now using positional argument
        print(f"Processing repositories: {repo_names}")
        report = jedimaster.process_repositories(repo_names)

    # Save report
    filename = jedimaster.save_report(report, args.output)  # Use --output parameter

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
