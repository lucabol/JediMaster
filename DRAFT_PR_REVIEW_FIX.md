# Fix: Draft PRs with Human Reviewers Not Being Reviewed

## Issue

PR #1686 was not being reviewed despite:
- ✅ Being a draft PR created by Copilot
- ✅ Having human reviewer requested (`lucabol`)
- ✅ Copilot finished working and requested review

## Root Cause Analysis

The classification logic had a gap in handling **draft PRs with human reviewers**.

### The Problem

The `needs_review` logic at line 1230 had this check:

```python
elif requested_reviewers and not is_draft:
    needs_review = True
```

This condition **excludes draft PRs** from the needs_review check.

Then later at lines 1259-1264, there was logic to catch draft PRs with human reviewers:

```python
if is_draft and requested_reviewers:
    human_reviewers = [r for r in requested_reviewers if 'copilot' not in r.lower()]
    if human_reviewers:
        needs_review = True
```

**But this came AFTER other draft handling logic**, so the execution order was problematic.

### The Scenario

When Copilot finishes work on an issue:
1. Creates a draft PR
2. Commits changes
3. Requests human reviewer (to signal work is done)
4. **PR should be reviewed at this point**

The classification logic should detect:
- Draft = True ✅
- Requested reviewers = ['lucabol'] ✅
- Human reviewers present (not just Copilot) ✅
- **Conclusion: Copilot done, needs review** ✅

But the check for this was placed too late in the logic flow.

## The Fix

### Moved Draft + Human Reviewers Check Earlier

Placed the check **right at the start** of the `needs_review` determination:

```python
needs_review = False
requested_reviewers = metadata.get('requested_reviewers', [])

# Check for draft PRs with human reviewers (Copilot finished and wants review)
if is_draft and requested_reviewers:
    human_reviewers = [r for r in requested_reviewers if 'copilot' not in r.lower()]
    if human_reviewers:
        # Draft with human reviewers requested = Copilot done, needs review
        self.logger.info(f"PR #{pr.number}: Draft with human reviewers {human_reviewers}, treating as needs_review")
        needs_review = True

if copilot_review_requested:
    needs_review = True
elif requested_reviewers and not is_draft:
    needs_review = True
# ... rest of logic
```

### Removed Duplicate Logic

Removed the duplicate check that was placed later (lines 1259-1264).

### Why This Works

Now the logic flow is:

1. **First check**: Is this a draft with human reviewers? → needs_review = True
2. **Second check**: Is Copilot review requested? → needs_review = True  
3. **Third check**: Are reviewers requested on non-draft? → needs_review = True
4. Continue with other checks...

This ensures draft PRs with human reviewers are **immediately** flagged for review.

## Expected Behavior

### Before Fix

Draft PR created by Copilot with human reviewer:
1. `is_draft = True, requested_reviewers = ['lucabol']`
2. Skipped by line 1230 check (`not is_draft` = False)
3. Eventually caught by line 1259-1264 check
4. But execution order could cause it to be missed
5. **PR not reviewed** ❌

### After Fix

Draft PR created by Copilot with human reviewer:
1. `is_draft = True, requested_reviewers = ['lucabol']`
2. **Immediately caught at line 1223-1233**
3. `needs_review = True` set
4. Returns `STATE_PENDING_REVIEW`
5. **PR reviewed by PRDeciderAgent** ✅

## Testing

```bash
# Test imports
python -c "from jedimaster import JediMaster; print('✓ OK')"

# Test on PR #1686 (should now be reviewed)
python example.py lucabol/Hello-World --orchestrate
```

## Impact

This fix ensures that **draft PRs with human reviewers are immediately recognized as ready for review**, which is the signal that Copilot has finished working and wants human approval.

This is a critical workflow for Copilot:
1. Copilot assigned to issue
2. Copilot creates draft PR
3. Copilot works on the PR
4. **Copilot requests human review** (signals completion)
5. System should review the PR
6. System approves/requests changes
7. PR moves through state machine

Without this fix, step 5 was being skipped!
