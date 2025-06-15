#!/usr/bin/env python3
"""
Test script for the specific GraphQL functionality that was failing
"""

import os
from dotenv import load_dotenv
from jedimaster import JediMaster

def test_issue_bot_search():
    """Test the specific issue and bot ID functionality."""
    load_dotenv()
    
    github_token = os.getenv('GITHUB_TOKEN')
    openai_api_key = os.getenv('OPENAI_API_KEY')
    
    if not github_token or not openai_api_key:
        print("Missing required environment variables: GITHUB_TOKEN and/or OPENAI_API_KEY")
        return
    
    # Initialize JediMaster
    jedi = JediMaster(github_token, openai_api_key)
      # Test the specific method that was failing
    # Using a real repository that should have issues
    repo_owner = "lucabol"  # Your GitHub username
    repo_name = "JediMaster"  # This repository 
    issue_number = 116  # The issue number from your error message
    
    print(f"Testing issue ID and bot search for {repo_owner}/{repo_name} issue #{issue_number}")
    
    try:
        issue_id, bot_id = jedi._get_issue_id_and_bot_id(repo_owner, repo_name, issue_number)
        
        if issue_id:
            print(f"✅ Successfully got issue ID: {issue_id}")
        else:
            print("❌ Failed to get issue ID")
            
        if bot_id:
            print(f"✅ Successfully found bot ID: {bot_id}")
        else:
            print("⚠️  No suitable bot found - this is expected in most repos")
            
        return issue_id is not None
        
    except Exception as e:
        print(f"❌ Error during test: {e}")
        return False

if __name__ == "__main__":
    test_issue_bot_search()
