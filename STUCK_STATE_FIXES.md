# Stuck State Fixes Applied

## Summary

Applied comprehensive fixes to eliminate stuck PRs in the state machine.

## Changes Made

### 1. ✅ Fixed Classification Logic (Reduce Aggressive Blocking)

**File**: `jedimaster.py` - `_classify_pr_state()` method

**Before:**
```python
if is_draft:
    if copilot_review_requested:
        return {'state': STATE_PENDING_REVIEW, ...}
    else:
        return {'state': STATE_BLOCKED, 'reason': 'draft'}  # ❌ STUCK

if mergeable is False and has_current_approval:
    return {'state': STATE_BLOCKED, 'reason': 'merge_conflict'}  # ❌ STUCK

return {'state': STATE_BLOCKED, 'reason': 'waiting_signal'}  # ❌ STUCK
```

**After:**
```python
if is_draft:
    if copilot_review_requested or requested_reviewers:
        # Draft with reviews - ready for review
        return {'state': STATE_PENDING_REVIEW, 'reason': 'draft_ready_for_review'}
    else:
        # Draft in progress - Copilot working
        return {'state': STATE_CHANGES_REQUESTED, 'reason': 'draft_in_progress'}  # ✅ MONITORED

if mergeable is False and has_current_approval:
    # Merge conflict needs resolution
    return {'state': STATE_CHANGES_REQUESTED, 'reason': 'merge_conflict_needs_resolution'}  # ✅ ACTIONABLE

# Default to review instead of blocking
return {'state': STATE_PENDING_REVIEW, 'reason': 'unclear_state_defaulting_to_review'}  # ✅ SAFE DEFAULT
```

**Impact:**
- ✅ Draft PRs no longer stuck - treated as work in progress
- ✅ Merge conflicts no longer stuck - actively monitored for resolution
- ✅ Unknown states no longer abandoned - default to human review
- ✅ BLOCKED state now rare (only for truly exceptional cases)

### 2. ✅ Made _handle_blocked_state Proactive

**File**: `jedimaster.py` - `_handle_blocked_state()` method

**Before:**
```python
async def _handle_blocked_state(...):
    # Just logs and returns
    return [PRRunResult(..., action='remain_blocked')]  # ❌ NO ACTION
```

**After:**
```python
async def _handle_blocked_state(...):
    """Handler for blocked state - attempts to unstick PRs.
    
    Note: After classification changes, blocked state should be rare.
    This handler attempts recovery for truly blocked PRs.
    """
    # Add human escalation label
    if not self._has_label(pr, HUMAN_ESCALATION_LABEL):
        pr.add_to_labels(HUMAN_ESCALATION_LABEL)  # ✅ ESCALATE
    
    # Add explanatory comment
    message = f"This PR is in a blocked state (reason: {reason}). A human maintainer should review..."
    self._ensure_comment_with_tag(pr, f'copilot:blocked-{reason}', message)  # ✅ NOTIFY
    
    return [PRRunResult(..., status='human_escalated', action='escalate_blocked')]  # ✅ ACTION TAKEN
```

**Impact:**
- ✅ Truly blocked PRs escalated to humans automatically
- ✅ Clear explanation provided via comment
- ✅ Human escalation label added for visibility
- ✅ No PRs left silently stuck

### 3. ✅ Enhanced _handle_changes_requested_state

**File**: `jedimaster.py` - `_handle_changes_requested_state()` method

**Added Handling For:**

**Draft in Progress:**
```python
if reason == 'draft_in_progress':
    message = "Draft PR in progress. Copilot is working on this..."
    tag = 'copilot:draft-in-progress'
    details = 'Draft PR - Copilot working'
```

**Merge Conflicts:**
```python
elif reason == 'merge_conflict_needs_resolution':
    message = "This PR has merge conflicts that need to be resolved..."
    tag = 'copilot:merge-conflict'
    details = 'Merge conflict needs resolution'
    # Request Copilot to help resolve
```

**Impact:**
- ✅ Draft PRs actively monitored with clear status
- ✅ Merge conflicts flagged with actionable message
- ✅ All states have appropriate handling and messaging

## Before vs After

### Scenario 1: Draft PR Created by Copilot

**Before:**
1. Copilot creates draft PR
2. Classified as BLOCKED (reason: 'draft')
3. Handler does nothing
4. **PR stuck forever** ❌

**After:**
1. Copilot creates draft PR
2. Classified as CHANGES_REQUESTED (reason: 'draft_in_progress')
3. Handler monitors progress
4. **PR progresses when ready** ✅

### Scenario 2: Merge Conflict After Approval

**Before:**
1. PR approved
2. Another PR merges, causing conflict
3. Classified as BLOCKED (reason: 'merge_conflict')
4. Handler does nothing
5. **PR stuck until manual fix** ❌

**After:**
1. PR approved
2. Another PR merges, causing conflict
3. Classified as CHANGES_REQUESTED (reason: 'merge_conflict_needs_resolution')
4. Handler adds comment requesting resolution
5. **PR monitored for fixes** ✅

### Scenario 3: Unknown State

**Before:**
1. PR doesn't match any classification
2. Classified as BLOCKED (reason: 'waiting_signal')
3. Handler does nothing
4. **PR abandoned** ❌

**After:**
1. PR doesn't match any classification
2. Classified as PENDING_REVIEW (reason: 'unclear_state_defaulting_to_review')
3. Handler reviews PR with PRDecider
4. **Human decides next steps** ✅

## State Flow Changes

### Old Flow (Stuck States)
```
Draft PR → BLOCKED (draft) → [STUCK FOREVER]
Conflict → BLOCKED (merge_conflict) → [STUCK FOREVER]
Unknown → BLOCKED (waiting_signal) → [STUCK FOREVER]
```

### New Flow (Active Monitoring)
```
Draft PR → CHANGES_REQUESTED (draft_in_progress) → monitors → PENDING_REVIEW → ...
Conflict → CHANGES_REQUESTED (merge_conflict_needs_resolution) → monitors → PENDING_REVIEW → ...
Unknown → PENDING_REVIEW (unclear_state_defaulting_to_review) → human decides → ...
```

## BLOCKED State Now Rare

After these changes, the BLOCKED state should be **extremely rare** because:

1. **Draft PRs** → `changes_requested` (monitored)
2. **Merge conflicts** → `changes_requested` (actionable)
3. **Unknown states** → `pending_review` (safe default)

The only PRs that reach BLOCKED now are:
- Exceptional edge cases
- PRs that truly need human intervention
- All are automatically escalated with human-review label

## Testing

```bash
# Test imports
python -c "from jedimaster import JediMaster; print('✓ OK')"

# Test orchestrated run
python example.py lucabol/Hello-World --orchestrate
```

## Impact on Orchestrator

The orchestrator benefits from these changes:

**Before:**
- ❌ Many PRs stuck in BLOCKED state
- ❌ Orchestrator had no workflow for blocked PRs
- ❌ PRs abandoned silently

**After:**
- ✅ Very few PRs reach BLOCKED state
- ✅ Draft PRs and conflicts in `changes_requested` (monitored)
- ✅ Unknown states go to `pending_review` (safe)
- ✅ Truly blocked PRs escalated to humans automatically

**Orchestrator Workflows Still Apply:**
- `merge_ready_prs` - merges approved PRs
- `review_prs` - reviews pending PRs (now includes unclear states)
- `flag_blocked_prs` - flags PRs exceeding retry limit (rare now)

## Monitoring

To monitor effectiveness of fixes:

```bash
# Check for PRs in blocked state (should be very few)
gh pr list --label "copilot-state:blocked"

# Check for human escalations
gh pr list --label "copilot-human-review"

# Check draft PRs (should be in changes_requested, not blocked)
gh pr list --state open --json number,isDraft,labels

# Check for merge conflicts (should be in changes_requested)
gh pr list --state open --json number,mergeable,labels
```

## Summary

✅ **All stuck state issues resolved**
✅ **PRs no longer abandoned**
✅ **Clear progression paths for all states**
✅ **Automatic escalation for truly stuck PRs**
✅ **Safe defaults instead of giving up**

The state machine now actively works to progress PRs instead of abandoning them!
