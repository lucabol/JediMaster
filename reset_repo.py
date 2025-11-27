#!/usr/bin/env python3
"""
Reset a GitHub repository: close all issues/PRs and delete all files except README.md
"""

import os
import sys
import requests
from github import Github
from dotenv import load_dotenv


def close_all_prs(repo, headers):
    """Close all open pull requests."""
    print("\n🔄 Closing all open pull requests...")
    pr_count = 0
    page = 1
    
    while True:
        url = f"https://api.github.com/repos/{repo.full_name}/pulls?state=open&per_page=100&page={page}"
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        prs = response.json()
        
        if not prs:
            break
        
        for pr in prs:
            try:
                close_url = f"https://api.github.com/repos/{repo.full_name}/pulls/{pr['number']}"
                close_data = {"state": "closed"}
                close_resp = requests.patch(close_url, headers=headers, json=close_data)
                close_resp.raise_for_status()
                pr_count += 1
                print(f"  ❌ Closed PR #{pr['number']}: {pr['title'][:60]}")
            except Exception as e:
                print(f"  ⚠️  Failed to close PR #{pr['number']}: {e}")
        
        if len(prs) < 100:
            break
        page += 1
    
    print(f"✅ Closed {pr_count} pull requests")
    return pr_count


def close_all_issues(repo, headers):
    """Close all open issues (excluding PRs)."""
    print("\n🗑️  Closing all open issues...")
    issue_count = 0
    page = 1
    
    while True:
        url = f"https://api.github.com/repos/{repo.full_name}/issues?state=open&per_page=100&page={page}"
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        issues = response.json()
        
        if not issues:
            break
        
        # Filter out PRs (issues with pull_request key)
        issues_only = [issue for issue in issues if 'pull_request' not in issue]
        
        for issue in issues_only:
            try:
                close_url = f"https://api.github.com/repos/{repo.full_name}/issues/{issue['number']}"
                close_data = {"state": "closed"}
                close_resp = requests.patch(close_url, headers=headers, json=close_data)
                close_resp.raise_for_status()
                issue_count += 1
                print(f"  ❌ Closed issue #{issue['number']}: {issue['title'][:60]}")
            except Exception as e:
                print(f"  ⚠️  Failed to close issue #{issue['number']}: {e}")
        
        if len(issues) < 100:
            break
        page += 1
    
    print(f"✅ Closed {issue_count} issues")
    return issue_count


def delete_all_files_except_readme(repo, github_token):
    """Delete all files and directories except README.md and .github directory using REST API."""
    print("\n🗂️  Deleting all files except README.md and .github/...")
    deleted_count = 0
    
    try:
        # Get the current commit
        default_branch = repo.default_branch
        repo_full_name = repo.full_name
        
        headers = {
            "Authorization": f"token {github_token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "Python-GitHub-Reset"
        }
        
        # Get current branch reference
        branch_uri = f"https://api.github.com/repos/{repo_full_name}/git/refs/heads/{default_branch}"
        branch_response = requests.get(branch_uri, headers=headers)
        branch_response.raise_for_status()
        current_commit_sha = branch_response.json()['object']['sha']
        
        print(f"  Current commit: {current_commit_sha[:7]}")
        
        # Get current commit details
        commit_uri = f"https://api.github.com/repos/{repo_full_name}/git/commits/{current_commit_sha}"
        commit_response = requests.get(commit_uri, headers=headers)
        commit_response.raise_for_status()
        base_tree_sha = commit_response.json()['tree']['sha']
        
        print(f"  Base tree: {base_tree_sha[:7]}")
        
        # Get the current tree recursively
        tree_uri = f"https://api.github.com/repos/{repo_full_name}/git/trees/{base_tree_sha}?recursive=1"
        tree_response = requests.get(tree_uri, headers=headers)
        tree_response.raise_for_status()
        current_tree = tree_response.json()['tree']
        
        print(f"  Found {len(current_tree)} items in repository")
        
        # Build tree items list with only files to keep (README.md and .github/*)
        tree_items = []
        kept_count = 0
        
        for item in current_tree:
            # Skip tree objects (directories)
            if item['type'] == 'tree':
                continue
            
            # Keep README.md (case-insensitive)
            if item['path'].lower() == 'readme.md':
                tree_items.append({
                    'path': item['path'],
                    'mode': item['mode'],
                    'type': item['type'],
                    'sha': item['sha']
                })
                kept_count += 1
                print(f"  ✅ Keeping: {item['path']}")
                continue
            
            # Keep everything in .github directory
            if item['path'].startswith('.github/'):
                tree_items.append({
                    'path': item['path'],
                    'mode': item['mode'],
                    'type': item['type'],
                    'sha': item['sha']
                })
                kept_count += 1
                continue
            
            # Everything else will be deleted
            deleted_count += 1
            print(f"  🗑️  Deleting: {item['path']}")
        
        print(f"\n  Creating new tree with {kept_count} items (deleting {deleted_count} items)...")
        
        # Create new tree WITHOUT base_tree (to replace entire tree, not modify it)
        create_tree_body = {
            'tree': tree_items
        }
        create_tree_uri = f"https://api.github.com/repos/{repo_full_name}/git/trees"
        tree_create_response = requests.post(create_tree_uri, headers=headers, json=create_tree_body)
        tree_create_response.raise_for_status()
        new_tree_sha = tree_create_response.json()['sha']
        
        print(f"  New tree created: {new_tree_sha[:7]}")
        
        # Create new commit
        commit_message = f"Reset repository: Keep only README.md and .github/\n\nDeleted {deleted_count} files/directories"
        create_commit_body = {
            'message': commit_message,
            'tree': new_tree_sha,
            'parents': [current_commit_sha]
        }
        create_commit_uri = f"https://api.github.com/repos/{repo_full_name}/git/commits"
        commit_create_response = requests.post(create_commit_uri, headers=headers, json=create_commit_body)
        commit_create_response.raise_for_status()
        new_commit_sha = commit_create_response.json()['sha']
        
        print(f"  New commit created: {new_commit_sha[:7]}")
        
        # Update branch reference
        update_ref_body = {
            'sha': new_commit_sha,
            'force': True
        }
        update_ref_uri = f"https://api.github.com/repos/{repo_full_name}/git/refs/heads/{default_branch}"
        ref_update_response = requests.patch(update_ref_uri, headers=headers, json=update_ref_body)
        ref_update_response.raise_for_status()
        
        print(f"  Branch {default_branch} updated to new commit")
        
    except requests.exceptions.HTTPError as e:
        print(f"  ⚠️  HTTP Error during file deletion: {e}")
        print(f"  Response: {e.response.text}")
        return 0
    except Exception as e:
        print(f"  ⚠️  Error during file deletion: {e}")
        import traceback
        traceback.print_exc()
        return 0
    
    print(f"✅ Deleted {deleted_count} files/directories, kept {kept_count} items")
    return deleted_count


def main():
    # Load environment variables (override=True ensures .env takes precedence)
    load_dotenv(override=True)
    
    github_token = os.getenv('GITHUB_TOKEN')
    repo_names = os.getenv('AUTOMATION_REPOS', '').split(',')
    
    if not github_token:
        print("❌ Error: GITHUB_TOKEN not found in environment")
        print("Please set GITHUB_TOKEN in .env file")
        print("\nExpected format in .env:")
        print("GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx")
        return 1
    
    # Validate token format
    if not github_token.startswith('ghp_') and not github_token.startswith('github_pat_'):
        print("⚠️  Warning: Token doesn't start with 'ghp_' or 'github_pat_'")
        print(f"Token starts with: {github_token[:10]}")
    
    if not repo_names or not repo_names[0].strip():
        print("❌ Error: AUTOMATION_REPOS not found in environment")
        print("Please set AUTOMATION_REPOS in .env file (format: owner/repo)")
        print("\nExpected format in .env:")
        print("AUTOMATION_REPOS=owner/repo")
        return 1
    
    # Get the first repository from AUTOMATION_REPOS
    repo_name = repo_names[0].strip()
    
    print("=" * 80)
    print(f"RESET REPOSITORY: {repo_name}")
    print("=" * 80)
    print("\nThis will:")
    print("  1. Close all open pull requests")
    print("  2. Close all open issues")
    print("  3. Delete all files and directories EXCEPT README.md and .github/")
    print("\nWARNING: This action cannot be undone!")
    print("\nProceeding with reset...")
    
    # Initialize GitHub client
    print("\n🔐 Authenticating with GitHub...")
    
    # Mask token for display
    token_length = len(github_token)
    if token_length > 10:
        masked_token = github_token[:6] + "*" * (token_length - 10) + github_token[-4:]
    else:
        masked_token = "*" * token_length
    print(f"Using token: {masked_token} (length: {token_length})")
    
    g = Github(login_or_token=github_token)
    
    try:
        user = g.get_user()
        print(f"✅ Authenticated as: {user.login}")
    except Exception as e:
        print(f"❌ Authentication failed: {e}")
        print(f"Token starts with: {github_token[:10]}...")
        return 1
    
    # Get repository
    try:
        repo = g.get_repo(repo_name)
        print(f"✅ Found repository: {repo.full_name}")
    except Exception as e:
        print(f"❌ Failed to access repository '{repo_name}': {e}")
        return 1
    
    # Create headers for REST API calls
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "Python-GitHub-Reset"
    }
    
    # Step 1: Close all PRs
    print("\n" + "=" * 80)
    print("STEP 1: Closing Pull Requests")
    print("=" * 80)
    pr_count = close_all_prs(repo, headers)
    
    # Step 2: Close all issues
    print("\n" + "=" * 80)
    print("STEP 2: Closing Issues")
    print("=" * 80)
    issue_count = close_all_issues(repo, headers)
    
    # Step 3: Delete all files except README.md
    print("\n" + "=" * 80)
    print("STEP 3: Deleting Files")
    print("=" * 80)
    deleted_count = delete_all_files_except_readme(repo, github_token)
    
    # Summary
    print("\n" + "=" * 80)
    print("RESET COMPLETE")
    print("=" * 80)
    print(f"  PRs closed: {pr_count}")
    print(f"  Issues closed: {issue_count}")
    print(f"  Files/directories deleted: {deleted_count}")
    print(f"\n✅ Repository {repo_name} has been reset!")
    print("  Preserved: README.md and .github/ directory")
    print("  All other files and directories have been deleted")
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
