# PR Approval Simplification - Summary

## Changes Made

Simplified the PR approval and merge flow to remove pre-merge mergeability checks and instead try to merge immediately when approved, handling errors by reassigning to Copilot.

## Key Changes

### 1. Simplified approval flow (lines 891-897)
- **Before**: Check if decision == 'accept' AND mergeable == True, then merge OR handle merge conflicts separately
- **After**: If decision == 'accept', immediately attempt to merge (pass to _merge_pr)

### 2. Removed pre-merge mergeable check (line 775-777)
- **Before**: Check if PR is approved by us AND mergeable == True before merging
- **After**: If PR is approved by us, try to merge (let merge attempt itself handle any errors)

### 3. Enhanced _merge_pr function
- **New signature**: Added copilot_slots_tracker parameter to track Copilot assignments
- **New error handling**: When merge fails (any exception):
  1. Check if comment limit exceeded → escalate to human with error message
  2. Check if Copilot slots are full → skip PR
  3. Otherwise → reassign to Copilot with full error text in the comment

### 4. Error message format
When reassigning after merge failure, the comment includes:
`
@copilot This PR is approved but merge failed with the following error:

`
<full error text>
`

Please fix the issue and update the PR so it can be merged.
`

## Benefits

1. **Simpler flow**: No need to pre-check mergeable status - just try to merge
2. **Better error handling**: Full error text is provided to Copilot for context
3. **Unified approach**: All merge failures handled the same way (reassign or escalate)
4. **Respects limits**: Still respects MAX_COMMENTS and MAX_COPILOT_SLOTS constraints

## Testing Recommendations

Test scenarios:
1. Approved PR that merges successfully ✓
2. Approved PR with merge conflicts → should reassign to Copilot with error
3. Approved PR with merge conflicts + comment limit exceeded → should escalate to human
4. Approved PR that fails to merge when Copilot slots full → should skip
5. Approved PR with other merge errors (e.g., CI failing) → should reassign to Copilot with error

