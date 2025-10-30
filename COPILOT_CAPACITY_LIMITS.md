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
