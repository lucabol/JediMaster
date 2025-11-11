"""Test script to fetch merge conflict info including main branch versions."""
import os
from github import Github
from dotenv import load_dotenv

load_dotenv()

def get_merge_conflict_info(repo_name: str, pr_number: int):
    """Fetch merge conflict diff and main branch versions of conflicting files."""
    token = os.getenv('GITHUB_TOKEN')
    g = Github(token)
    repo = g.get_repo(repo_name)
    pr = repo.get_pull(pr_number)
    
    print(f"PR #{pr_number}: {pr.title}")
    print(f"Base branch: {pr.base.ref}")
    print(f"Head branch: {pr.head.ref}")
    print(f"Mergeable: {pr.mergeable}")
    print(f"Mergeable state: {pr.mergeable_state}")
    print("\n" + "="*80 + "\n")
    
    # Get the PR diff (shows conflicts)
    print("FETCHING PR DIFF (with conflicts):")
    print("-" * 80)
    try:
        files = list(pr.get_files())
        print(f"Found {len(files)} modified files\n")
        
        for file in files:
            print(f"\n{'='*80}")
            print(f"File: {file.filename}")
            print(f"Status: {file.status}")
            print(f"Changes: +{file.additions} -{file.deletions}")
            print(f"{'='*80}")
            
            # Get the patch (diff) for this file from the PR
            if hasattr(file, 'patch') and file.patch:
                print("\nPR DIFF (with conflicts if any):")
                print("-" * 80)
                print(file.patch)
                print("-" * 80)
            else:
                print("\nNo patch available for this file")
            
            # Get the version from the base branch (main)
            print(f"\n\nMAIN BRANCH VERSION ({pr.base.ref}):")
            print("-" * 80)
            try:
                # Get file content from base branch
                base_content = repo.get_contents(file.filename, ref=pr.base.ref)
                if base_content.encoding == 'base64':
                    import base64
                    content = base64.b64decode(base_content.content).decode('utf-8')
                    # Show first 50 lines max
                    lines = content.split('\n')
                    if len(lines) > 50:
                        print('\n'.join(lines[:50]))
                        print(f"\n... ({len(lines) - 50} more lines)")
                    else:
                        print(content)
                else:
                    print(base_content.decoded_content.decode('utf-8')[:2000])
                print("-" * 80)
            except Exception as e:
                print(f"Could not fetch base version: {e}")
                print("-" * 80)
            
            print("\n")
    
    except Exception as e:
        print(f"Error fetching files: {e}")
    
    print("\n" + "="*80 + "\n")

if __name__ == "__main__":
    get_merge_conflict_info("gim-home/JediTestRepoV3", 1981)
