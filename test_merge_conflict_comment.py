"""Test the new _fetch_pr_diff_with_base_versions function."""
import os
import sys
from dotenv import load_dotenv

# Add parent directory to path to import jedimaster
sys.path.insert(0, os.path.dirname(__file__))

load_dotenv()

# Create a minimal JediMaster instance to test the function
from jedimaster import JediMaster
from github import Github

def test_fetch_with_base_versions():
    """Test fetching PR diff with base versions."""
    token = os.getenv('GITHUB_TOKEN')
    endpoint = os.getenv('AZURE_FOUNDRY_ENDPOINT')
    
    # Create minimal JediMaster instance
    jm = JediMaster(
        github_token=token,
        azure_foundry_endpoint=endpoint,
        just_label=False,
        use_topic_filter=False,
        manage_prs=True,
        verbose=True
    )
    
    # Get PR
    g = Github(token)
    repo = g.get_repo("gim-home/JediTestRepoV3")
    pr = repo.get_pull(1981)
    
    print(f"Testing PR #{pr.number}: {pr.title}")
    print(f"Mergeable: {pr.mergeable}, State: {pr.mergeable_state}")
    print("\n" + "="*80 + "\n")
    
    # Test the new function
    diff_content, base_versions, error = jm._fetch_pr_diff_with_base_versions(pr, "gim-home/JediTestRepoV3")
    
    if error:
        print("ERROR:", error)
        return
    
    print("DIFF CONTENT:")
    print("-" * 80)
    if diff_content:
        print(diff_content[:2000])
        if len(diff_content) > 2000:
            print(f"\n... (showing first 2000 of {len(diff_content)} chars)")
    else:
        print("(None)")
    
    print("\n" + "="*80 + "\n")
    print("BASE VERSIONS:")
    print("-" * 80)
    if base_versions:
        # Write to file to avoid console encoding issues
        with open('base_versions_output.txt', 'w', encoding='utf-8') as f:
            f.write(base_versions)
        print(f"Base versions written to base_versions_output.txt ({len(base_versions)} chars)")
    else:
        print("(None)")
    
    print("\n" + "="*80 + "\n")
    
    # Simulate what would be in the comment
    print("SIMULATED COMMENT TO COPILOT:")
    print("-" * 80)
    
    comment_msg = (
        f"@copilot This PR is approved but merge failed with the following error:\n\n"
        f"```\nMerge conflict detected\n```\n\n"
    )
    
    if diff_content:
        truncated_diff = diff_content[:3000]
        if len(diff_content) > 3000:
            truncated_diff += "\n\n... (diff truncated, too large)"
        comment_msg += f"**Current diff (showing merge conflicts if any):**\n```diff\n{truncated_diff}\n```\n\n"
    
    if base_versions:
        truncated_base = base_versions[:5000]
        if len(base_versions) > 5000:
            truncated_base += "\n\n... (base versions truncated, too large)"
        comment_msg += f"**Base branch versions ({pr.base.ref}) for reference:**\n```\n{truncated_base}\n```\n\n"
    
    comment_msg += "Please fix the issue and update the PR so it can be merged."
    
    print(comment_msg[:3000])
    if len(comment_msg) > 3000:
        print(f"\n... (showing first 3000 of {len(comment_msg)} chars)")

if __name__ == "__main__":
    test_fetch_with_base_versions()
