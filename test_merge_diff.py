#!/usr/bin/env python3
"""Test script to demonstrate improved merge conflict diff extraction."""

import os
import sys
from github import Github
from dotenv import load_dotenv

load_dotenv()

# Import the function we'll test
sys.path.insert(0, os.path.dirname(__file__))
from jedimaster import JediMaster

def test_diff_extraction(repo_name: str, pr_number: int):
    """Test the improved diff extraction for a PR."""
    
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("Error: GITHUB_TOKEN not found in .env")
        sys.exit(1)
    
    # Create a JediMaster instance
    jm = JediMaster(
        github_token=token,
        azure_foundry_endpoint=os.getenv("AZURE_AI_FOUNDRY_ENDPOINT", ""),
        verbose=True
    )
    
    g = Github(token)
    repo = g.get_repo(repo_name)
    pr = repo.get_pull(pr_number)
    
    print(f"\n{'='*80}")
    print(f"Testing improved diff extraction for PR #{pr.number}")
    print(f"Title: {pr.title}")
    print(f"{'='*80}\n")
    
    print(f"Base branch: {pr.base.ref}")
    print(f"Head branch: {pr.head.ref}")
    print(f"Mergeable: {pr.mergeable}")
    print(f"Mergeable state: {pr.mergeable_state}")
    print(f"Files changed: {pr.changed_files}")
    print(f"\n{'='*80}")
    print("EXTRACTED DIFF WITH BASE CONTEXT:")
    print(f"{'='*80}\n")
    
    # Use the improved function
    diff_content, base_versions, _ = jm._fetch_pr_diff_with_base_versions(pr, repo_name)
    
    if diff_content:
        # Handle Unicode encoding for console output
        try:
            print(diff_content)
        except UnicodeEncodeError:
            # If console can't handle the encoding, write to file instead
            print("[Unicode content - writing to file merge_diff_output.txt]")
            with open("merge_diff_output.txt", "w", encoding="utf-8") as f:
                f.write(diff_content)
            print("Diff written to merge_diff_output.txt")
        
        print(f"\n{'='*80}")
        print(f"Total size: {len(diff_content)} characters")
        print(f"{'='*80}\n")
    else:
        print("No diff content extracted")
    
    if base_versions:
        print("WARNING: base_versions should be None (included in diff_content now)")
    
    # Show what would be sent to Copilot
    print(f"\n{'='*80}")
    print("SAMPLE COPILOT COMMENT:")
    print(f"{'='*80}\n")
    
    sample_comment = (
        f"@copilot This PR is approved but merge failed with the following error:\n\n"
        f"```\nMerge conflict detected\n```\n\n"
        f"✓ **Branch has been synced with base branch.** Please resolve any remaining issues.\n\n"
    )
    
    if diff_content:
        # Truncate for display
        display_diff = diff_content[:2000] if len(diff_content) > 2000 else diff_content
        sample_comment += f"**Merge conflict details (including base branch context):**\n{display_diff}\n"
        if len(diff_content) > 2000:
            sample_comment += f"\n... (truncated for display, full diff is {len(diff_content)} chars)\n"
    
    sample_comment += "\nPlease fix the merge conflicts and update the PR so it can be merged."
    
    print(sample_comment)

if __name__ == "__main__":
    # Test with PR 1981 which has merge conflicts
    repo_name = "gim-home/JediTestRepoV3"
    pr_number = 1981
    
    print(f"Testing improved diff extraction for PR #{pr_number} from {repo_name}")
    
    test_diff_extraction(repo_name, pr_number)

