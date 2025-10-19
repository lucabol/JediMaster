# State Machine Analysis: Stuck States and Coverage Gaps

## State Machine Overview

### Defined States

```python
STATE_INTAKE = "intake"              # Virtual state (never actually labeled)
STATE_PENDING_REVIEW = "pending_review"
STATE_CHANGES_REQUESTED = "changes_requested"
STATE_READY_TO_MERGE = "ready_to_merge"
STATE_BLOCKED = "blocked"
STATE_DONE = "done"
```

### Handler Coverage

```python
handler_map = {
    STATE_PENDING_REVIEW: self._handle_pending_review_state,
    STATE_CHANGES_REQUESTED: self._handle_changes_requested_state,
    STATE_READY_TO_MERGE: self._handle_ready_to_merge_state,
    STATE_BLOCKED: self._handle_blocked_state,
}
```

**✅ Has Handler**: `pending_review`, `changes_requested`, `ready_to_merge`, `blocked`
**⚠️ Special Case**: `done` (conditional cleanup only)
**❌ No Handler**: `intake` (virtual state, never used)

## Critical Finding: BLOCKED State Can Cause Work Stoppage

### ❌ PROBLEM: STATE_BLOCKED Has NO Meaningful Handler

**Code Analysis:**

```python
async def _handle_blocked_state(self, pr, metadata, classification=None):
    """Handler for blocked state - just logs and returns."""
    repo_full = pr.base.repo.full_name
    results: List[PRRunResult] = []
    
    # Log blocked reason
    reason = classification.get('reason', 'unknown') if classification else 'unknown'
    self.logger.info(f"PR #{pr.number} in blocked state, reason: {reason}")
    
    results.append(
        PRRunResult(
            repo=repo_full,
            pr_number=pr.number,
            title=pr.title,
            status='blocked',
            details=f'PR blocked: {reason}',
            state_before=STATE_BLOCKED,
            state_after=STATE_BLOCKED,
            action='remain_blocked',
        )
    )
    return results
```

**What This Means:**
- ✅ Logs that PR is blocked
- ❌ **Does NOTHING to fix the problem**
- ❌ **No action to move PR out of blocked state**
- ❌ **PR sits in blocked forever**

## Scenarios Where PRs Get Stuck in BLOCKED State

### 1. ❌ Draft PRs Without Review Requests

**Classification Logic (line 1222-1227):**
```python
if is_draft:
    if copilot_review_requested:
        return {'state': STATE_PENDING_REVIEW, 'reason': 'copilot_review_on_draft'}
    else:
        return {'state': STATE_BLOCKED, 'reason': 'draft'}
```

**Scenario:**
1. Copilot creates a PR
2. PR is in draft mode
3. No reviewers requested yet
4. **Classified as BLOCKED (reason: 'draft')**
5. Handler does nothing
6. **PR STUCK FOREVER** ⚠️

**Why This is Bad:**
- Copilot often creates draft PRs while working
- System should either wait or mark ready when Copilot finishes
- Instead, it marks as BLOCKED and stops tracking

**Expected Behavior:**
- Should mark PR as ready for review when Copilot finishes
- Or at least monitor draft PRs for completion

### 2. ❌ Merge Conflicts on Approved PRs

**Classification Logic (line 1229-1230):**
```python
if mergeable is False and has_current_approval:
    return {'state': STATE_BLOCKED, 'reason': 'merge_conflict'}
```

**Scenario:**
1. PR gets approved
2. Another PR merges first, causing conflict
3. **Classified as BLOCKED (reason: 'merge_conflict')**
4. Handler does nothing
5. **PR STUCK** until someone manually resolves ⚠️

**Why This is Bad:**
- Merge conflicts are expected in active repos
- System should notify Copilot or author
- System should track conflict resolution
- Instead, marks blocked and abandons

**Expected Behavior:**
- Request Copilot to resolve conflict
- Or escalate to human with notification
- Monitor for resolution

### 3. ❌ PRs Waiting for Unknown Signal

**Classification Logic (line 1232):**
```python
return {'state': STATE_BLOCKED, 'reason': 'waiting_signal'}
```

**Scenario:**
1. PR doesn't match any classification criteria
2. Not draft, not approved, not pending, not closed
3. **Classified as BLOCKED (reason: 'waiting_signal')**
4. Handler does nothing
5. **PR STUCK** ⚠️

**Why This is Bad:**
- This is a catch-all for "don't know what to do"
- System gives up on the PR
- No attempt to understand or fix

**Expected Behavior:**
- Should investigate what signal is missing
- Should default to pending_review if unclear
- Should never just abandon a PR

### 4. ✅ Draft PRs WITH Merge Conflicts (Not Stuck)

**Note:** Draft PRs with merge conflicts would hit the draft check first (line 1222) before the merge conflict check (line 1229), so they become BLOCKED with reason 'draft', not 'merge_conflict'. Still stuck though.

## Scenarios That Work Correctly

### ✅ 1. Pending Review → Approved → Ready to Merge → Merged

**Happy Path:**
1. PR created: `pending_review`
2. PRDecider approves: transitions to `ready_to_merge`
3. Handler merges PR: transitions to `done`
4. ✅ Works perfectly

### ✅ 2. Approved But New Commits → Needs Re-review

**Path:**
1. PR approved: `ready_to_merge`
2. Author pushes new commits
3. Reclassified: `pending_review` (needs re-review)
4. PRDecider reviews again
5. ✅ Works correctly

### ✅ 3. Changes Requested → Author Updates → Review Again

**Path:**
1. PRDecider requests changes: `changes_requested`
2. Handler waits for author
3. Author pushes update
4. Reclassified: `pending_review`
5. PRDecider reviews again
6. ✅ Works correctly

### ✅ 4. Merge Exceeds Retry Limit → Blocked with Human Escalation

**Path:**
1. PR ready to merge: `ready_to_merge`
2. Merge fails 3 times
3. Moves to `blocked` BUT adds `copilot-human-review` label
4. State machine sees escalation label and skips (lines 520-531)
5. ✅ Human notified, system stops trying

## Summary: Where PRs Get Stuck

### ❌ CRITICAL: Stuck in BLOCKED State

| Reason | When It Happens | Impact | Fix Needed |
|--------|----------------|--------|------------|
| `draft` | Copilot creates draft PR, no reviewers | PR sits forever | Auto-request review when draft marked ready |
| `merge_conflict` | Approved PR gets conflict from other merge | Sits until manual fix | Request Copilot to resolve or escalate |
| `waiting_signal` | PR doesn't match classification rules | Abandoned | Default to pending_review or investigate |

### ✅ States That Work

- `pending_review` → Handler reviews with PRDecider
- `changes_requested` → Handler waits for author, then re-reviews
- `ready_to_merge` → Handler merges (or escalates after max retries)
- `done` → Handler cleans up labels

## Classification Logic Issues

### Issue 1: Draft PRs Are Too Aggressively Blocked

**Current Logic:**
```python
if is_draft:
    if copilot_review_requested:
        return {'state': STATE_PENDING_REVIEW, ...}
    else:
        return {'state': STATE_BLOCKED, 'reason': 'draft'}  # ❌ STUCK
```

**Problem:** 
- Copilot creates draft PRs
- Draft status doesn't mean "blocked"
- It means "work in progress"
- System should wait, not block

**Better Logic:**
```python
if is_draft:
    if copilot_review_requested or requested_reviewers:
        # Copilot likely done, request review
        return {'state': STATE_PENDING_REVIEW, 'reason': 'draft_ready_for_review'}
    else:
        # Still in progress - check age or wait
        return {'state': STATE_CHANGES_REQUESTED, 'reason': 'draft_in_progress'}
        # OR: Don't classify yet, skip this PR
```

### Issue 2: Merge Conflicts Have No Recovery Path

**Current Logic:**
```python
if mergeable is False and has_current_approval:
    return {'state': STATE_BLOCKED, 'reason': 'merge_conflict'}  # ❌ STUCK
```

**Problem:**
- System detects conflict but does nothing
- No notification to Copilot
- No escalation to human
- Just abandons the PR

**Better Logic:**
```python
if mergeable is False and has_current_approval:
    # Request Copilot to resolve conflict
    # Or escalate to human after some time
    return {'state': STATE_CHANGES_REQUESTED, 'reason': 'merge_conflict_needs_resolution'}
    # Handler should request Copilot to fix conflict
```

### Issue 3: Catch-All "waiting_signal" Gives Up Too Early

**Current Logic:**
```python
return {'state': STATE_BLOCKED, 'reason': 'waiting_signal'}  # ❌ STUCK
```

**Problem:**
- This is reached when nothing else matches
- System has no idea what to do
- Gives up and abandons

**Better Logic:**
```python
# Default to pending review if unclear
return {'state': STATE_PENDING_REVIEW, 'reason': 'unclear_state_defaulting_to_review'}
# Let human reviewer decide what's needed
```

## Recommendations

### 1. Fix BLOCKED State Handler

**Current:**
```python
async def _handle_blocked_state(self, pr, metadata, classification):
    # Just logs and returns
    return [PRRunResult(..., action='remain_blocked')]
```

**Recommended:**
```python
async def _handle_blocked_state(self, pr, metadata, classification):
    reason = classification.get('reason', 'unknown')
    
    if reason == 'draft':
        # Check if draft is old or has reviewers
        # If Copilot done, mark ready for review
        # If still in progress, wait
        
    elif reason == 'merge_conflict':
        # Request Copilot to resolve conflict
        # Or escalate to human after timeout
        
    elif reason == 'waiting_signal':
        # Default to pending_review
        # Let human decide
    
    # Add escalation after X days in blocked
```

### 2. Reduce Aggressive BLOCKED Classification

**Change draft handling:**
- Draft = work in progress, NOT blocked
- Only block if draft is stale (>7 days)
- Otherwise treat as `changes_requested` (Copilot working)

**Change conflict handling:**
- Merge conflict = needs work, NOT permanently blocked
- Classify as `changes_requested` with conflict flag
- Request Copilot to resolve

**Change catch-all:**
- Unknown state = default to `pending_review`
- Don't abandon PRs

### 3. Add Escape Hatches

**Time-based escalation:**
```python
# If PR stuck in any state >7 days
if stuck_duration > timedelta(days=7):
    # Escalate to human regardless of state
    self._escalate_pr_to_human(pr, reason='stuck_timeout')
```

**Periodic re-evaluation:**
```python
# If PR in blocked state, re-classify periodically
if current_state == STATE_BLOCKED:
    # Re-run classification (maybe conditions changed)
    new_classification = self._classify_pr_state(pr, metadata)
    # Might move out of blocked
```

## Orchestrator Impact

The orchestrator respects these states:
- ✅ Merges `ready_to_merge` PRs (quick wins)
- ✅ Reviews `pending_review` PRs
- ❌ **Ignores `blocked` PRs** (leaves them stuck)
- ❌ **Ignores `changes_requested` PRs** (assumes Copilot working)

**Orchestrator should:**
1. Monitor `blocked` PRs and escalate old ones
2. Check `changes_requested` PRs for stale work
3. Re-evaluate stuck PRs periodically

## Conclusion

### 🚨 CRITICAL ISSUES

1. **BLOCKED state is a dead-end** - PRs go in, never come out
2. **Draft PRs get blocked** - Should be "in progress", not "blocked"
3. **Merge conflicts abandoned** - Should request resolution
4. **No time-based escape hatches** - Stuck PRs stay stuck forever

### 🎯 IMMEDIATE FIXES NEEDED

1. **Make _handle_blocked_state proactive** - Try to unstuck PRs
2. **Reduce aggressive blocking** - Draft = WIP, not blocked
3. **Add escalation timeouts** - Stuck >7 days → human review
4. **Default to review, not blocked** - When uncertain, ask human

### ⚠️ IMPACT ON ORCHESTRATOR

The orchestrator currently:
- ✅ Handles working states well
- ❌ **Ignores blocked states** (no workflow for them)
- ❌ **No escape mechanism for stuck PRs**

**Recommendation:** Add `unstuck_blocked_prs` workflow to orchestrator.
