#!/usr/bin/env python3
"""
Create a test issue and then test the GraphQL functionality
"""

import os
from dotenv import load_dotenv
from github import Github
from jedimaster import JediMaster

def create_test_issue_and_test():
    """Create a test issue and test GraphQL functionality."""
    load_dotenv()
    
    github_token = os.getenv('GITHUB_TOKEN')
    openai_api_key = os.getenv('OPENAI_API_KEY')
    
    if not github_token or not openai_api_key:
        print("Missing required environment variables")
        return
    
    github = Github(github_token)
    jedi = JediMaster(github_token, openai_api_key)
    
    try:
        repo = github.get_repo("lucabol/JediMaster")
        
        # Create a test issue
        issue = repo.create_issue(
            title="Test issue for GraphQL functionality",
            body="This is a test issue to verify the GraphQL assignment functionality works correctly."
        )
        
        print(f"✅ Created test issue #{issue.number}: {issue.title}")
        
        # Now test the GraphQL functionality
        print(f"Testing GraphQL functionality with issue #{issue.number}")
        
        issue_id, bot_id = jedi._get_issue_id_and_bot_id("lucabol", "JediMaster", issue.number)
        
        if issue_id:
            print(f"✅ Successfully got issue ID: {issue_id}")
        else:
            print("❌ Failed to get issue ID")
            
        if bot_id:
            print(f"✅ Successfully found bot ID: {bot_id}")
        else:
            print("⚠️  No suitable bot found - this is expected")
            
        # Clean up - close the test issue
        issue.edit(state='closed')
        print(f"✅ Closed test issue #{issue.number}")
        
        return issue_id is not None
        
    except Exception as e:
        print(f"❌ Error during test: {e}")
        return False

if __name__ == "__main__":
    create_test_issue_and_test()
