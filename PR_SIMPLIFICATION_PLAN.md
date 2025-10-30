# PR State Machine Simplification Plan

## Current Problem
The current PR state machine is overly complex with:
- 6 different states (intake, pending_review, changes_requested, ready_to_merge, blocked, done)
- Multiple state labels (`copilot-state:*`)
- Complex classification logic with many edge cases
- Difficult to understand and maintain

## New Simplified Approach

### Key Insight
We can now reliably detect if Copilot is working on a PR using `_is_copilot_actively_working()`. This eliminates the need for complex state tracking.

### New Logic

**Single Label:** `copilot-human-review` (only applied when human intervention needed)

**Processing Flow:**
```
For each open PR:
  1. Skip if has `copilot-human-review` label
  2. Skip if Copilot is actively working (respect ongoing work)
  3. If PR has review requests:
     - Review it (submit to agent for review)
     - Post comments/approve/request changes
  4. If PR is approved and mergeable:
     - Attempt merge
     - On success: close, clean up
     - On conflict: request Copilot to fix
     - On max retries: apply `copilot-human-review`
  5. Otherwise: skip (nothing to do yet)
```

### Removed Complexity
- ❌ State labels (`copilot-state:*`)
- ❌ State classification logic
- ❌ State transition tracking
- ❌ Draft PR special handling (covered by work detection)
- ❌ Merge attempt labels (can track internally or in comments)

### What Remains
- ✅ `_is_copilot_actively_working()` - detects if Copilot is working
- ✅ `copilot-human-review` label - marks PRs needing human help
- ✅ Review submission to agent
- ✅ Merge attempts with retry logic
- ✅ Max Copilot capacity checking

### Benefits
1. **Simpler:** Much easier to understand and maintain
2. **Reliable:** Fewer edge cases and state inconsistencies
3. **Transparent:** Uses native GitHub PR state (review requests, approvals)
4. **Flexible:** Doesn't fight GitHub's natural PR workflow

### Implementation Changes

#### Remove:
- All `STATE_*` constants
- `COPILOT_STATE_LABEL_PREFIX`
- `_classify_pr_state()`
- `_set_state_label()`
- `_get_state_label()`
- State-specific handlers (`_handle_pending_review_state`, etc.)

#### Keep/Modify:
- `_is_copilot_actively_working()` - keep as-is
- `_collect_pr_metadata()` - simplify to only collect what's needed
- Create one unified `_process_pr()` method

#### New Method Structure:
```python
async def _process_pr(self, pr, repo) -> List[PRRunResult]:
    """Process a single PR - review if needed, merge if ready."""
    
    # Skip if needs human
    if self._has_human_review_label(pr):
        return []
    
    # Skip if Copilot working
    if self._is_copilot_actively_working(pr.number, repo):
        return []
    
    # Collect minimal metadata
    metadata = self._collect_pr_metadata(pr)
    
    # If has review requests, review it
    if metadata['has_review_requests']:
        return await self._review_pr(pr, metadata)
    
    # If approved and mergeable, try to merge
    if metadata['is_approved'] and metadata['is_mergeable']:
        return await self._merge_pr(pr, metadata)
    
    # Nothing to do
    return []
```

### Migration Notes
- Existing state labels can be left as-is (they'll be ignored)
- Or we can add cleanup to remove old state labels
- The `copilot-human-review` label concept remains the same
