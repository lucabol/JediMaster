#!/usr/bin/env python3
"""
Test script for the GraphQL functionality in JediMaster
"""

import os
from dotenv import load_dotenv
from jedimaster import JediMaster

def test_graphql_methods():
    """Test the GraphQL methods."""
    load_dotenv()
    
    github_token = os.getenv('GITHUB_TOKEN')
    openai_api_key = os.getenv('OPENAI_API_KEY')
    
    if not github_token or not openai_api_key:
        print("Missing required environment variables: GITHUB_TOKEN and/or OPENAI_API_KEY")
        return
    
    # Initialize JediMaster
    jedi = JediMaster(github_token, openai_api_key)
    
    # Test GraphQL request (simple query)
    test_query = """
    query {
      viewer {
        login
      }
    }
    """
    
    try:
        result = jedi._graphql_request(test_query)
        print("GraphQL connection test successful!")
        print(f"Logged in as: {result['data']['viewer']['login']}")
        return True
    except Exception as e:
        print(f"GraphQL connection test failed: {e}")
        return False

if __name__ == "__main__":
    test_graphql_methods()
