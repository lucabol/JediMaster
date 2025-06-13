#!/usr/bin/env python3
"""
Example usage of JediMaster as a library.
"""

import os
from dotenv import load_dotenv
from jedimaster import JediMaster

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
        "octocat/Hello-World",  # Public test repository
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
    main()
