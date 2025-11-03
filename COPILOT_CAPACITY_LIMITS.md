# Copilot Capacity Limit Implementation

## Problem
The orchestrator was creating over 100 PRs by assigning too many issues to GitHub Copilot, causing:
1. Rate limit errors from Copilot ("you've hit a rate limit...")
2. Overwhelming Copilot with too many simultaneous PRs
3. Poor repository health due to too many in-flight PRs

## Root Cause
- No enforcement of maximum concurrent PRs that Copilot can handle
- Issue assignment didn't check Copilot capacity before assigning
- The hardcoded limit was too low (5) and not enforced

## Solution Implemented

### 1. Increased Copilot Max Concurrent PRs to 10
**File: `agents/analytical.py` line 94**
```python
copilot_max = 10  # Limit to prevent overloading Copilot and hitting rate limits
```

### 2. Added Capacity Tracking During Workflow Execution
**File: `jedimaster.py` lines 2193-2206**
- Track available Copilot slots dynamically as workflows execute
- Decrement available capacity when issues are assigned (creating PRs)
- Pass capacity tracker to each workflow execution

### 3. Enforce Capacity Limit in process_issues Workflow
**File: `jedimaster.py` lines 2283-2309**
- Check Copilot capacity before processing issues
- Skip issue assignment if no capacity available
- Limit batch size to available Copilot slots
- Log capacity-based limitations

### 4. Updated Orchestrator Strategic Rules
**File: `agents/orchestrator.py`**

#### System Prompt Changes:
- **Principle 1**: Explicit 10 PR hard limit
- **Principle 3**: Merge ready PRs FIRST to free Copilot capacity
- **Strategic Rule 1**: If Copilot ≥8 PRs, ONLY merge/review, DO NOT assign new issues
- **Strategic Rule 6**: Respect BOTH API quota AND Copilot capacity (use smaller limit)

#### Prompt Formatting:
- Show capacity warnings: "← AT CAPACITY, DO NOT ASSIGN MORE ISSUES"
- Highlight quick wins: "← MERGE THESE FIRST TO FREE COPILOT!"
- Show assignable count: "← CAN ASSIGN UP TO X"

### 5. Updated Fallback Plan
**File: `agents/orchestrator.py` lines 241-287**
- Only assign issues if Copilot has available slots
- Conservative approach: use half of available capacity in fallback mode
- Skip issue assignment if copilot_available_slots <= 0

## Expected Behavior

### Before Fix:
1. ❌ Assigns unlimited issues to Copilot
2. ❌ Creates 100+ PRs overwhelming Copilot
3. ❌ Hits Copilot rate limits frequently
4. ❌ Poor repo health

### After Fix:
1. ✅ Maximum 10 active PRs at any time
2. ✅ Merges ready PRs first to free capacity
3. ✅ Only assigns issues when capacity available
4. ✅ Respects both API quota and Copilot limits
5. ✅ Better repository health

## Monitoring

The orchestrator now logs:
- Initial Copilot capacity: `X/10 active PRs (Y slots available)`
- Capacity warnings when at limit
- Capacity reductions after assigning issues
- Skipped workflows due to capacity constraints

## Testing

Run orchestrator and verify:
```bash
python example.py --orchestrate
```

Expected log output:
```
[Orchestrator] Copilot Capacity: 3/10 active PRs (7 slots available)
[Orchestrator] Limiting process_issues batch from 15 to 7 based on Copilot capacity
[Orchestrator] Copilot capacity reduced by 5, now 2 slots available
```

## Future Improvements

1. Make copilot_max configurable via environment variable
2. Add dynamic capacity adjustment based on Copilot response times
3. Implement priority queuing when at capacity
4. Add metrics for capacity utilization over time

---

## Implementation Update - Full Slot Tracking System

### Overview
Implemented comprehensive slot tracking that counts BOTH "Copilot currently working" AND "our new requests to Copilot" toward the same limit of `MAX_COPILOT_SLOTS = 10`.

### Algorithm

#### During PR Review (`_execute_review_workflow`):
- Initialize: `copilot_slots_tracker = {'used': 0}`
- For each PR processed:
  - **If Copilot is actively working** → `tracker['used'] += 1`
  - **If we request changes** (triggers @copilot) → `tracker['used'] += 1`
  - **If we reassign after error** (triggers @copilot) → `tracker['used'] += 1`
  - **If `tracker['used'] >= MAX_COPILOT_SLOTS`** → Skip remaining PRs

#### During Issue Assignment (`_execute_workflow` → `process_issues`):
- Calculate: `available_slots = MAX_COPILOT_SLOTS - copilot_slots_used`
- **If `available_slots <= 0`** → Skip issue assignment entirely
- **Otherwise** → Assign `min(workflow.batch_size, available_slots)` issues

### Code Changes

#### 1. Added Constant (Line ~77)
```python
MAX_COPILOT_SLOTS = 10  # Maximum concurrent Copilot assignments
```

#### 2. Updated `_process_pr_state_machine` Signature
```python
async def _process_pr_state_machine(self, pr, copilot_slots_tracker: Optional[Dict[str, int]] = None)
```
- Tracks slots when Copilot is working
- Tracks slots when we reassign after error
- Skips PR if slots full

#### 3. Updated `_review_and_act_on_pr` Signature
```python
async def _review_and_act_on_pr(self, pr, copilot_slots_tracker: Optional[Dict[str, int]] = None)
```
- Tracks slots when we request changes
- Skips PR if slots full

#### 4. Enhanced `_execute_review_workflow`
- Initializes slot tracker
- Passes tracker to each PR processing
- Stops processing when slots fill up
- Logs final slot usage

#### 5. Enhanced `orchestrated_run`
- Tracks `copilot_slots_used` across all workflows
- Counts slots from PR reviews
- Counts slots from issue assignments
- Passes current usage to each workflow

#### 6. Enhanced `_execute_workflow`
- Uses `copilot_slots_used` instead of `copilot_available_slots`
- Calculates available slots: `MAX_COPILOT_SLOTS - copilot_slots_used`
- Limits issue batch size accordingly

### Example Scenario

With `MAX_COPILOT_SLOTS = 10` and 20 PRs + 15 issues:

**Step 1: Review PRs**
1. PR #1: Copilot working → slots: 1
2. PR #2: Copilot working → slots: 2  
3. PR #3: Request changes → slots: 3
4. PR #4: Copilot working → slots: 4
5. PR #5: Request changes → slots: 5
6. PR #6: Approve & merge → slots: 5 (no Copilot)
7. PR #7: Copilot error, reassign → slots: 6
8. PR #8: Request changes → slots: 7
9. PR #9: Copilot working → slots: 8
10. PR #10: Request changes → slots: 9
11. PR #11: Request changes → slots: 10
12. **PR #12-20: SKIPPED** (slots full: 10/10)

**Step 2: Assign Issues**
- Used slots: 10
- Available: 10 - 10 = 0
- Planned batch: 15
- **Result: SKIP all issue assignments** (no capacity)

**Alternative: If only 6 slots used:**
- Used slots: 6
- Available: 10 - 6 = 4
- Planned batch: 15
- **Result: Assign 4 issues** (respects capacity)

### Benefits

1. **Prevents Copilot Overload**: Hard limit of 10 concurrent work items
2. **Prioritizes PRs**: Reviews existing PRs before assigning new issues
3. **Accurate Tracking**: Counts both active work AND new requests
4. **Transparent**: Detailed logging at each step
5. **Configurable**: Single constant to adjust limit

### Monitoring

Enhanced log messages:
```
Review workflow received PR numbers: [123, 124, 125, ...]
Starting with MAX_COPILOT_SLOTS=10
Reviewing PR #123: draft=False, state=open, slots_used=0/10
  PR #123: Changes requested
Reviewing PR #124: draft=False, state=open, slots_used=1/10
  PR #124: Copilot working
Copilot slots full (10/10), skipping remaining 8 PRs
Review workflow complete. Final Copilot slots used: 10/10

[Orchestrator] After review_prs: 10 Copilot slots used
[Orchestrator] Skipping process_issues: No Copilot slots available (10/10 used)
```

### Configuration

To adjust the limit, modify `MAX_COPILOT_SLOTS` constant in `jedimaster.py`:
- **5-8**: Conservative, for smaller repos or limited Copilot capacity
- **10**: Balanced (current default)
- **15-20**: Aggressive, for large repos with high Copilot throughput

