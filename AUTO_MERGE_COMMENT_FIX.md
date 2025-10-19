# Fix: Remove Misleading "Auto-merge Disabled" Comments

## Issue

Users seeing this comment on PRs:
```
[copilot:auto-merge-disabled]
Auto-merge is disabled; waiting for a maintainer to merge this PR manually.
```

**Problem**: This comment is misleading and incorrect.

## Why It's Wrong

1. **In orchestrated mode**: The orchestrator **will** merge ready PRs automatically (we fixed this in commit 0158739)
2. **In regular mode**: If `manage_prs=False`, the system shouldn't be adding confusing comments to PRs
3. **Confusion**: Users think manual action is needed when it's not

## Root Cause

In `_handle_ready_to_merge_state()` at line 358-360:

```python
if not self.manage_prs:
    message = "Auto-merge is disabled; waiting for a maintainer to merge this PR manually."
    self._ensure_comment_with_tag(pr, 'copilot:auto-merge-disabled', message)
    # ...
    return results
```

This code adds the comment whenever `manage_prs=False`.

### When Does This Happen?

The comment was being added in this scenario:

1. **Regular run with orchestrator**:
   - JediMaster created with `manage_prs=False` (default for orchestrator mode)
   - Orchestrator reviews PRs to classify them
   - PR classified as `ready_to_merge`
   - Handler called with `manage_prs=False`
   - **Comment added** ❌ (misleading!)
   - Orchestrator will merge later, but comment says manual merge needed

2. **Regular run without orchestrator**:
   - JediMaster created with `manage_prs=False`
   - PR reaches `ready_to_merge` state
   - Handler adds comment saying manual merge needed
   - But orchestrator could merge it later!

## The Fix

### 1. Remove the Misleading Comment

Changed the `manage_prs=False` check to just record state without adding comments:

```python
if not self.manage_prs:
    # When manage_prs is disabled, don't interfere with ready-to-merge PRs
    # Just record the state and return (orchestrator or manual merge will handle it)
    results.append(
        PRRunResult(
            repo=repo_full,
            pr_number=pr.number,
            title=pr.title,
            status='ready_to_merge',
            details='PR ready to merge (managed externally)',
            state_before=STATE_READY_TO_MERGE,
            state_after=STATE_READY_TO_MERGE,
            action='ready_external_merge',
        )
    )
    return results
```

**No more misleading comment!**

### 2. Added Method to Remove Old Comments

Created `_remove_comment_with_tag()` method:

```python
def _remove_comment_with_tag(self, pr, tag: str) -> None:
    """Remove comments with a specific tag."""
    marker = f"[{tag}]"
    try:
        existing = pr.get_issue_comments()
        for comment in existing:
            body = comment.body or ''
            if marker in body:
                try:
                    comment.delete()
                    self.logger.info(f"Removed comment with tag '{tag}' from PR #{pr.number}")
                except Exception as exc:
                    self.logger.error(f"Failed to delete comment {comment.id} from PR #{pr.number}: {exc}")
    except Exception as exc:
        self.logger.error(f"Failed to enumerate comments for PR #{pr.number}: {exc}")
```

### 3. Clean Up Existing Comments

Added cleanup call at the start of `_handle_ready_to_merge_state()`:

```python
# Clean up any old auto-merge-disabled comments (no longer used)
self._remove_comment_with_tag(pr, 'copilot:auto-merge-disabled')
```

This removes old comments from PRs that were processed before this fix.

## Impact

### Before Fix

```
User sees PR with comment:
  "[copilot:auto-merge-disabled]
   Auto-merge is disabled; waiting for a maintainer to merge this PR manually."

User thinks: "I need to merge this manually"
Reality: Orchestrator will merge it automatically
Result: Confusion ❌
```

### After Fix

```
User sees PR: (no misleading comment)

Orchestrator sees ready_to_merge PR:
  - Executes merge workflow
  - Temporarily sets manage_prs=True
  - Merges the PR
  
Result: PR merged automatically, no confusion ✅
```

## Why This Approach Works

**The Right Behavior**:

1. **Orchestrated mode** (`--orchestrate`):
   - JediMaster created with `manage_prs=False`
   - Reviews PRs to classify them
   - When PR classified as `ready_to_merge`, just records it (no comment)
   - Orchestrator's merge workflow temporarily enables `manage_prs` and merges
   - **No misleading comments, merges happen correctly** ✅

2. **Regular mode** (no orchestrator):
   - JediMaster created with `manage_prs=True` (user wants direct merging)
   - PR reaches `ready_to_merge` state
   - Handler merges immediately (skips the `manage_prs=False` check)
   - **Direct merging works as expected** ✅

3. **Monitoring mode** (`manage_prs=False`, no orchestrator):
   - JediMaster created with `manage_prs=False` (user just wants to monitor)
   - PR reaches `ready_to_merge` state
   - Handler records state and returns (no comment, no merge)
   - **User knows they need to merge manually** (because they set manage_prs=False)
   - **No confusing comments needed** ✅

## Testing

```bash
# Test imports
python -c "from jedimaster import JediMaster; print('✓ OK')"

# Test orchestrator (should merge without misleading comments)
python example.py lucabol/Hello-World --orchestrate
```

Expected results:
- No new "auto-merge-disabled" comments on ready PRs
- Old comments removed when PRs are processed
- Orchestrator merges ready PRs as expected

## Summary

**Problem**: Misleading "auto-merge disabled" comments confused users

**Solution**: 
- Removed the comment creation
- Added cleanup method to remove old comments
- Let orchestrator handle merging without confusing messages

**Result**: Clear behavior, no misleading comments, automatic merging works correctly
