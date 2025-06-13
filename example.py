#!/usr/bin/env python3
"""
Example usage of JediMaster as a library.
"""

import os
from dotenv import load_dotenv
from jedimaster import JediMaster

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
    for title, body in copilot_issues + non_copilot_issues:
        ok, err = create_github_issue(github_token, owner, repo, title, body)
        print(f"Created issue '{title}': {'OK' if ok else 'FAILED'}{f' - {err}' if err else ''}")

def main():
    """Example of using JediMaster programmatically."""
    
    # Load environment variables from .env file (if it exists)
    load_dotenv()
    
    # Get API keys from environment (either from .env or system environment)
    github_token = os.getenv('GITHUB_TOKEN')
    openai_api_key = os.getenv('OPENAI_API_KEY')
    
    if not github_token or not openai_api_key:
        print("Please set GITHUB_TOKEN and OPENAI_API_KEY environment variables")
        print("Either in a .env file or as system environment variables")
        return
    
    # Initialize JediMaster
    jedimaster = JediMaster(github_token, openai_api_key)
    
    # Example repositories (replace with your own)
    repositories = [
        "lucabol/Hello-World",  # Public test repository
        # Add more repositories here
    ]
    
    print(f"Processing {len(repositories)} repositories...")
    
    # Process repositories
    report = jedimaster.process_repositories(repositories)
    
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
    populate_repo_with_issues()
    main()
