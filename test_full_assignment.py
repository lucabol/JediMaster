#!/usr/bin/env python3
"""
Test the full assignment process including the GraphQL mutation
"""

import os
from dotenv import load_dotenv
from github import Github
from jedimaster import JediMaster

def test_full_assignment():
    """Test the complete assignment process."""
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
            title="Test issue for full assignment process",
            body="This is a test issue to verify the complete GraphQL assignment process works."
        )
        
        print(f"✅ Created test issue #{issue.number}: {issue.title}")
        
        # Test the full assignment process
        print(f"Testing full assignment process with issue #{issue.number}")
        
        result = jedi.assign_to_copilot(issue)
        
        if result:
            print("✅ Assignment process completed successfully")
            
            # Refresh the issue to see if it was assigned
            issue = repo.get_issue(issue.number)
            assignees = [assignee.login for assignee in issue.assignees]
            print(f"Issue assignees: {assignees}")
            
            # Check labels
            labels = [label.name for label in issue.labels]
            print(f"Issue labels: {labels}")
            
        else:
            print("❌ Assignment process failed")
        
        # Clean up - close the test issue
        issue.edit(state='closed')
        print(f"✅ Closed test issue #{issue.number}")
        
        return result
        
    except Exception as e:
        print(f"❌ Error during test: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    test_full_assignment()
