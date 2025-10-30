"""
Test script to check if Copilot is actively working on PRs in a repository.
Reads configuration from .env file (REPOS, GITHUB_TOKEN, AZURE_AI_FOUNDRY_ENDPOINT).
Usage: python test_copilot_status.py
"""
import sys
import os
from github import Github
from dotenv import load_dotenv

# Import JediMaster class
from jedimaster import JediMaster

def main():
    # Load environment variables from .env file (override existing ones)
    load_dotenv(override=True)
    
    github_token = os.getenv("GITHUB_TOKEN")
    azure_foundry_endpoint = os.getenv("AZURE_AI_FOUNDRY_ENDPOINT")
    repos_input = os.getenv("AUTOMATION_REPOS")
    
    if not github_token:
        print("Error: GITHUB_TOKEN not found in .env file")
        sys.exit(1)
    
    if not azure_foundry_endpoint:
        print("Error: AZURE_AI_FOUNDRY_ENDPOINT not found in .env file")
        sys.exit(1)
    
    if not repos_input:
        print("Error: AUTOMATION_REPOS not found in .env file")
        sys.exit(1)
    
    # Parse repos (take the first one if multiple)
    repos = [r.strip() for r in repos_input.split(',')]
    repo_full = repos[0]
    
    if '/' not in repo_full:
        print(f"Error: Repository must be in format owner/repo, got: {repo_full}")
        sys.exit(1)
    
    owner, repo_name = repo_full.split('/', 1)
    
    # Initialize JediMaster
    jm = JediMaster(
        github_token=github_token,
        azure_foundry_endpoint=azure_foundry_endpoint,
        just_label=True,
        use_topic_filter=True
    )
    
    # Get repository using JediMaster's github client
    try:
        repository = jm.github.get_repo(repo_full)
    except Exception as e:
        print(f"Error: Could not access repository {repo_full}")
        print(f"Details: {e}")
        print("\nMake sure:")
        print("1. The repository exists")
        print("2. Your GitHub token has access to it")
        print("3. The format is correct: owner/repo")
        sys.exit(1)
    
    print(f"\nChecking Copilot status for PRs in {repo_full}\n")
    print("=" * 80)
    
    # Get all open PRs
    prs = repository.get_pulls(state='open', sort='created', direction='desc')
    
    pr_count = 0
    active_count = 0
    
    for pr in prs:
        pr_count += 1
        is_active = jm._is_copilot_actively_working(pr.number, repository)
        status = "🔵 ACTIVELY WORKING" if is_active else "⚪ Not working"
        
        if is_active:
            active_count += 1
        
        print(f"\nPR #{pr.number}: {pr.title[:60]}")
        print(f"  Status: {status}")
        print(f"  URL: {pr.html_url}")
        print(f"  Draft: {pr.draft}")
        print(f"  Review requested: {'Yes' if pr.requested_reviewers else 'No'}")
        print("-" * 80)
    
    print(f"\n{'=' * 80}")
    print(f"Summary:")
    print(f"  Total PRs: {pr_count}")
    print(f"  Copilot actively working: {active_count}")
    print(f"  Not actively working: {pr_count - active_count}")
    print(f"{'=' * 80}\n")

if __name__ == "__main__":
    main()
