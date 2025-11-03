# Draft PR Handling Simplification

## Overview
Simplified draft PR handling by removing redundant checks and relying more on timeline events rather than the `draft` status flag.

## Changes Made

### 1. Removed Draft Filtering in PR Collection (lines 1040-1058)
**Before:** PRs were separated into `ready_prs` and `draft_prs`, with ready PRs prioritized
**After:** All open PRs are processed equally, relying on timeline events to determine their state

```python
# Simplified from:
ready_prs = [pr for pr in pulls if not getattr(pr, 'draft', False)]
draft_prs = [pr for pr in pulls if getattr(pr, 'draft', False)]
all_prs = ready_prs + draft_prs

# To:
pulls = list(repo.get_pulls(state='open'))
if batch_size:
    pulls = pulls[:batch_size]
```

### 2. Removed Draft-Specific State Classification Logic (lines 1858-1944)
**Before:** Complex logic for draft states including:
- `draft_ready_for_review`
- `draft_in_progress`
- `draft_stale_needs_review`
- `draft_needs_conflict_resolution`
- `copilot_finished_needs_ready`

**After:** Simplified state determination based on:
- Review requests (regardless of draft status)
- Timeline events (Copilot working, errors, finished)
- Commit activity and review status

### 3. Removed Redundant Draft Check in Ready-to-Merge Handler (lines 413-440)
**Before:** Checked draft status and converted to ready before merge attempt
**After:** Removed this check - if a PR reaches ready-to-merge state, it should already be ready
- Draft-to-ready conversion (if needed) still happens in `_merge_pr()` method where GitHub requires it

### 4. Simplified Changes-Requested State Handler (lines 295-350)
**Before:** Special handling for `draft_in_progress` reason with custom message
**After:** Removed draft-specific logic - all changes-requested PRs handled uniformly

### 5. Added Missing STATE_DONE Constant
Added `STATE_DONE = "done"` to state constants for completeness

## Rationale

### Why This Simplification Works

1. **Timeline Events Are More Reliable**
   - `Copilot started working` / `Copilot finished working` events tell us the actual state
   - Review requests tell us when Copilot is done and wants human review
   - These work regardless of draft status

2. **Draft Status Is Less Important**
   - GitHub's draft status is just a UI flag
   - What matters is: Is Copilot working? Are there reviewers? Is it approved?
   - Timeline events give us this information directly

3. **Reduces Edge Cases**
   - No need to handle "draft with review requests" as special case
   - No need to detect "stale drafts"
   - No need to check if "Copilot finished but PR still draft"

4. **Cleaner Code Flow**
   - PR collection: Simple list, no prioritization
   - State classification: Based on actual work status, not draft flag
   - State handlers: Uniform logic for all PRs

## What Still Uses Draft Status

1. **_mark_pr_ready_for_review()** - GraphQL method to convert draft to ready (kept for when needed)
2. **_merge_pr()** - Converts draft to ready before merge if GitHub requires it
3. **Metadata collection** - Still tracks `is_draft` but doesn't use it for decision-making

## Benefits

- **Fewer lines of code** (~80 lines removed)
- **Simpler logic** - no draft-specific branches
- **More reliable** - based on actual events, not status flags
- **Easier to maintain** - fewer edge cases to handle

## Testing Recommendations

Test with repositories that have:
- Draft PRs with Copilot assigned
- Draft PRs where Copilot finished and requested review
- Mix of draft and ready PRs
- PRs that transition from draft to ready during processing
