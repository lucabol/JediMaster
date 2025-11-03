#!/usr/bin/env python3
"""
Verify that fixes from PR_PIPELINE_ANALYSIS.md would solve stuck PRs
"""
import os
import sys
from dotenv import load_dotenv
from github import Github

load_dotenv()

# Test PRs from the analysis that were stuck
TEST_PRS = [710, 707, 704, 614, 703]
REPO_NAME = "gim-home/JediTestRepoV3"

def check_pr_state(pr):
    """Check if PR would be skipped with new logic"""
    results = []
    
    # Check 1: Merge conflicts
    mergeable = getattr(pr, 'mergeable', None)
    mergeable_state = getattr(pr, 'mergeable_state', None)
    is_draft = getattr(pr, 'draft', False)
    
    if mergeable is False or mergeable_state == 'dirty':
        results.append(f"✅ Would SKIP - Has merge conflicts (mergeable={mergeable}, state={mergeable_state})")
    
    # Check 2: Copilot working status
    copilot_start = None
    copilot_finish = None
    
    try:
        timeline = list(pr.as_issue().get_timeline())
        for event in timeline:
            event_type = getattr(event, 'event', None)
            if event_type == 'commented':
                body = getattr(event, 'body', '') or ''
                if 'copilot started work on behalf of' in body.lower():
                    copilot_start = getattr(event, 'created_at', None)
                elif 'copilot finished' in body.lower():
                    copilot_finish = getattr(event, 'created_at', None)
        
        if copilot_start and (not copilot_finish or copilot_finish < copilot_start):
            results.append(f"✅ Would SKIP - Copilot is actively working")
    except Exception as e:
        results.append(f"⚠️  Could not check Copilot status: {e}")
    
    # Check 3: Review cycles
    try:
        reviews = list(pr.get_reviews())
        changes_requested_count = sum(1 for r in reviews if getattr(r, 'state', '').upper() == 'CHANGES_REQUESTED')
        if changes_requested_count > 5:
            results.append(f"✅ Would ESCALATE - Too many review cycles ({changes_requested_count})")
    except Exception as e:
        results.append(f"⚠️  Could not count reviews: {e}")
    
    # Check 4: Draft status
    if is_draft:
        results.append(f"ℹ️  Draft PR - Would be processed with LOWER PRIORITY")
    
    return results

def main():
    token = os.getenv('GITHUB_TOKEN')
    if not token:
        print("❌ GITHUB_TOKEN not found in environment")
        sys.exit(1)
    
    github = Github(token)
    repo = github.get_repo(REPO_NAME)
    
    print(f"Analyzing stuck PRs in {REPO_NAME}...\n")
    print("=" * 80)
    
    for pr_number in TEST_PRS:
        try:
            pr = repo.get_pull(pr_number)
            print(f"\nPR #{pr_number}: {pr.title[:60]}")
            print(f"  State: {pr.state}, Merged: {pr.merged}")
            print(f"  Mergeable: {pr.mergeable}, State: {pr.mergeable_state}")
            print(f"  Draft: {getattr(pr, 'draft', False)}")
            
            checks = check_pr_state(pr)
            for check in checks:
                print(f"  {check}")
            
            if not checks:
                print(f"  ⚠️  Would still be PROCESSED (no skip conditions met)")
                
        except Exception as e:
            print(f"\nPR #{pr_number}: ❌ Error: {e}")
    
    print("\n" + "=" * 80)
    print("\nSummary:")
    print("✅ = Fix would prevent stuck PR")
    print("⚠️  = Needs investigation")
    print("ℹ️  = Informational")

if __name__ == "__main__":
    main()
