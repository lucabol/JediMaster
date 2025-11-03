# PR Pipeline Simplification - Using Only MAX_COMMENTS

## Summary

Simplified the PR review and merge pipeline by removing all retry count checks (`MERGE_MAX_RETRIES`) and using only the total comment count (`MAX_COMMENTS`) to determine when to escalate PRs to human review.

## Changes Made

### 1. Removed Environment Variable
- Removed `MERGE_MAX_RETRIES` environment variable and `_get_merge_max_retries()` method
- Kept only `MAX_COMMENTS` (default: 35) as the single escalation threshold

### 2. Simplified Merge Conflict Handling
**Before:** Tracked merge conflict retry attempts via labels, escalated after N retries
**After:** Always ask Copilot to fix merge conflicts, escalate only if total comments exceed `MAX_COMMENTS`

```python
# Old: Complex retry counting
if merge_conflict_attempts >= self.merge_max_retries:
    escalate()
else:
    increment_retry_count()
    ask_copilot_to_fix()

# New: Simple comment counting  
if total_comments > self.max_comments:
    escalate()
else:
    ask_copilot_to_fix()
```

### 3. Simplified Copilot Error Handling
**Before:** Tracked error retry attempts via labels, escalated after N retries
**After:** Always ask Copilot to retry, escalate only if total comments exceed `MAX_COMMENTS`

### 4. Removed Review Cycle Counting
**Before:** Escalated PRs after N review cycles
**After:** Removed review cycle check entirely - rely only on total comment count

### 5. Removed Merge Attempt Counting  
**Before:** Escalated PRs after N failed merge attempts
**After:** Removed this check - rely only on total comment count

### 6. Fixed Missing Constants
Added missing module-level constants that were referenced but not defined:
- `MERGE_ATTEMPT_LABEL_PREFIX`
- `COPILOT_STATE_LABEL_PREFIX`
- `STATE_PENDING_REVIEW`, `STATE_CHANGES_REQUESTED`, `STATE_READY_TO_MERGE`, `STATE_BLOCKED`
- `COPILOT_LABEL_PALETTE`

## Benefits

1. **Simpler Logic:** One clear escalation criterion instead of multiple retry counters
2. **Fewer Labels:** No need to track retry counts via labels (though label-related code remains for cleanup)
3. **More Flexible:** Copilot can attempt fixes multiple times as long as total comments stay reasonable
4. **Clear Threshold:** Total comment count is a good proxy for "this PR is stuck in a loop"

## Behavior

- **Merge conflicts:** Copilot is asked to fix them every time, no retry limit
- **Copilot errors:** Copilot is asked to retry every time, no retry limit  
- **Review cycles:** No limit on review cycles
- **Escalation trigger:** Only when total PR comments > `MAX_COMMENTS` (default 35)

## Configuration

Set the escalation threshold via environment variable:
```bash
export MAX_COMMENTS=35  # Default value
```

## Note

While retry count helper methods (`_get_merge_conflict_retry_count`, `_increment_merge_conflict_retry_count`, etc.) are no longer used, they remain in the codebase for now. They can be removed in a future cleanup if the label prefixes are also removed.
