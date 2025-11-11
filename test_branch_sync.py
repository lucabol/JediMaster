"""
Test program to sync a PR branch with main/master
"""
import os
from github import Github
from dotenv import load_dotenv

def test_branch_sync():
    # Load environment variables
    load_dotenv()
    github_token = os.getenv('GITHUB_TOKEN')
    
    if not github_token:
        print("ERROR: GITHUB_TOKEN not found in .env")
        return
    
    # Initialize GitHub client
    g = Github(github_token)
    
    # Get the repository and PR
    repo = g.get_repo("gim-home/JediTestRepoV3")
    pr_number = 1915
    
    try:
        pr = repo.get_pull(pr_number)
        print(f"PR #{pr_number}: {pr.title}")
        print(f"State: {pr.state}")
        print(f"Head branch: {pr.head.ref}")
        print(f"Base branch: {pr.base.ref}")
        print(f"Mergeable: {pr.mergeable}")
        print(f"Mergeable state: {pr.mergeable_state}")
        
        # Get the head branch
        head_branch = pr.head.ref
        base_branch = pr.base.ref
        
        print(f"\nAttempting to sync '{head_branch}' with '{base_branch}'...")
        
        # Method 1: Update branch using GitHub API's update-branch endpoint
        # This is the recommended way to sync a PR branch
        try:
            result = pr.update_branch()
            print(f"✓ Successfully synced branch using update_branch()")
            print(f"Result: {result}")
        except Exception as e:
            print(f"✗ Failed to sync using update_branch(): {e}")
            
            # Method 2: Try merging base into head
            try:
                print(f"\nTrying alternative: merge '{base_branch}' into '{head_branch}'...")
                
                # Get base branch SHA
                base_ref = repo.get_git_ref(f"heads/{base_branch}")
                base_sha = base_ref.object.sha
                
                # Merge base into head
                merge_result = repo.merge(
                    base=head_branch,
                    head=base_sha,
                    commit_message=f"Merge {base_branch} into {head_branch}"
                )
                print(f"✓ Successfully merged using repo.merge()")
                print(f"Merge SHA: {merge_result.sha}")
            except Exception as e2:
                print(f"✗ Failed to merge: {e2}")
    
    except Exception as e:
        print(f"ERROR: {e}")

if __name__ == "__main__":
    test_branch_sync()
