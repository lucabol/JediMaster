#!/usr/bin/env python3
"""
Find available issues in the repository
"""

import os
from dotenv import load_dotenv
from github import Github

def list_issues():
    """List available issues."""
    load_dotenv()
    
    github_token = os.getenv('GITHUB_TOKEN')
    
    if not github_token:
        print("Missing GITHUB_TOKEN environment variable")
        return
    
    github = Github(github_token)
    
    try:
        repo = github.get_repo("lucabol/JediMaster")
        print(f"Repository: {repo.full_name}")
        
        issues = list(repo.get_issues(state='open'))
        print(f"Found {len(issues)} open issues:")
        
        for issue in issues:
            print(f"  #{issue.number}: {issue.title}")
            
        if not issues:
            # Try closed issues
            closed_issues = list(repo.get_issues(state='closed'))
            print(f"Found {len(closed_issues)} closed issues:")
            for issue in closed_issues[:5]:  # Show first 5
                print(f"  #{issue.number}: {issue.title}")
                
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    list_issues()
