# Fix: WorkloadPrioritizer Not Including Unlabeled PRs

## Issue

Orchestrator log shows:
```
STRATEGY:
  ...prioritize clearing PRs via reviews...
  
WORKFLOWS EXECUTED:
  • review_prs (batch=5)
    Reasoning: There are 10 PRs needing attention...
    
RESULTS:
  ✓ review_prs:
     Processed: 0, Succeeded: 0, Failed: 0  ❌
```

**Problem**: Orchestrator says "10 PRs need attention" but then processes 0 PRs.

## Root Cause Analysis

### The Chicken-and-Egg Problem

1. **RepoStateAnalyzer** counts open PRs and reports "10 PRs open"
2. **WorkloadPrioritizer** filters PRs by state label to build `pending_review_prs` list
3. **New PRs don't have state labels** (haven't been processed yet)
4. **WorkloadPrioritizer ignores unlabeled PRs** ← THE BUG
5. **Result**: `pending_review_prs = []` (empty list)
6. **Workflow processes 0 PRs** even though there are 10 PRs!

### Code Analysis

**WorkloadPrioritizer._get_pr_state_label()** (line 69-77):

```python
def _get_pr_state_label(self, pr) -> str:
    """Extract copilot-state label from PR."""
    try:
        for label in pr.labels:
            if label.name.startswith(COPILOT_STATE_LABEL_PREFIX):
                return label.name[len(COPILOT_STATE_LABEL_PREFIX):]
    except Exception:
        pass
    return 'unknown'  # PRs without state labels
```

**WorkloadPrioritizer.prioritize()** (line 28-45) - BEFORE FIX:

```python
for pr in repo.get_pulls(state='open'):
    state = self._get_pr_state_label(pr)
    
    if state == 'ready_to_merge':
        quick_wins.append(pr.number)
    elif state == 'pending_review':
        pending_review_prs.append(pr.number)
    elif state == 'changes_requested':
        changes_requested_prs.append(pr.number)
    # ❌ NO HANDLING FOR state == 'unknown'
    # PRs without labels are silently ignored!
```

**The Problem**:
- New PRs have no `copilot-state:*` labels
- `_get_pr_state_label()` returns `'unknown'`
- Code has no `elif state == 'unknown'` case
- **PRs are silently ignored** and not added to any category

### Why This Happens

PR Lifecycle:
1. **Created** → No labels, state = 'unknown'
2. **First processing** → State machine classifies → Adds `copilot-state:pending_review` label
3. **Subsequent runs** → WorkloadPrioritizer sees label → Adds to `pending_review_prs`

But WorkloadPrioritizer only sees PRs at step 3! It misses PRs at step 1.

## The Fix

### Add Handler for Unlabeled PRs

```python
for pr in repo.get_pulls(state='open'):
    state = self._get_pr_state_label(pr)
    
    if state == 'ready_to_merge':
        quick_wins.append(pr.number)
    elif state == 'pending_review':
        pending_review_prs.append(pr.number)
    elif state == 'changes_requested':
        changes_requested_prs.append(pr.number)
    elif state == 'unknown':  # ✅ NEW
        # PRs without state labels need to be classified
        # Add them to pending_review to be processed by state machine
        pending_review_prs.append(pr.number)
    # Note: 'blocked' and 'done' states are intentionally not processed
```

### Why This Works

**New Flow**:
1. **Created** → No labels, state = 'unknown'
2. **WorkloadPrioritizer** → Sees state = 'unknown' → Adds to `pending_review_prs`
3. **Orchestrator** → Executes review_prs workflow → Processes the PR
4. **State machine** → Classifies PR → Adds appropriate label
5. **Next run** → PR has label → Handled normally

**Result**: All PRs get processed, not just previously-labeled ones!

## Before vs After

### Before Fix

```
Open PRs: PR #1686 (no labels)

RepoStateAnalyzer:
  "10 PRs open" ✅

WorkloadPrioritizer:
  state = 'unknown'
  No handler for 'unknown'
  pending_review_prs = []  ❌

Orchestrator:
  "10 PRs need attention, batch=5"
  pr_numbers = [][:5] = []
  Processes: 0 PRs  ❌
```

### After Fix

```
Open PRs: PR #1686 (no labels)

RepoStateAnalyzer:
  "10 PRs open" ✅

WorkloadPrioritizer:
  state = 'unknown'
  ✅ Handler: Add to pending_review_prs
  pending_review_prs = [1686]  ✅

Orchestrator:
  "10 PRs need attention, batch=5"
  pr_numbers = [1686][:5] = [1686]
  Processes: 1 PR  ✅
  State machine classifies PR
  Adds copilot-state label
```

## Impact

This fix ensures:
- ✅ **New PRs are immediately processed** (don't need to wait for initial labeling)
- ✅ **WorkloadPrioritizer accurately reflects work to be done**
- ✅ **Orchestrator processes the correct number of PRs**
- ✅ **No silent failures** (PRs aren't ignored)

## States Intentionally Not Processed

The code intentionally does **not** add these states to any workload:

- **`blocked`** - PRs blocked and escalated to humans (shouldn't be auto-processed)
- **`done`** - Closed/merged PRs (no work needed)

These states are correctly left out of the workload.

## Testing

```bash
# Test imports
python -c "from agents.analytical.workload_prioritizer import WorkloadPrioritizer; print('✓ OK')"

# Test orchestrator (should now process unlabeled PRs)
python example.py lucabol/Hello-World --orchestrate
```

Expected result: PRs without labels will now be included in `pending_review_prs` and processed.

## Summary

**Problem**: WorkloadPrioritizer only counted PRs with existing state labels, ignoring new unlabeled PRs.

**Solution**: Add handler for `state == 'unknown'` that adds unlabeled PRs to `pending_review_prs` for classification.

**Result**: All open PRs are now processed, not just previously-labeled ones. The orchestrator will actually process PRs as intended!
