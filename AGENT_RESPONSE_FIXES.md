# Agent Response Format Fixes

## Problem
The PR reviewer agent was returning responses in the correct format (`{'decision': 'accept'}`), but the code was incorrectly validating and handling them, causing PRs to be skipped with the error:
```
Skipping PR #1407: agent result has neither 'accept' nor 'comment'. 
Full agent_result: {'decision': 'accept'}
```

## Root Causes

### 1. Ambiguous Prompt
The system prompt in `decider.py` wasn't clear enough about the exact JSON format expected, leading to potential confusion.

### 2. Incorrect Validation Logic
In `jedimaster.py`, the code path for approved PRs didn't account for the case where `decision: 'accept'` is present but there's no `comment` field. The code tried to extract `comment` and failed when it wasn't there.

### 3. Duplicate Code
There was redundant validation and error handling for the comment field that created unnecessary complexity.

## Fixes Applied

### 1. Improved Prompt Clarity (decider.py)
- **Before**: Vague description of expected response
- **After**: Provides exact JSON examples:
  - `{"decision": "accept"}` for accepting PRs
  - `{"comment": "..."}` for requesting changes
- Added explicit instruction to NOT wrap JSON in markdown
- Made escape sequence requirements clearer

### 2. Fixed Validation Logic (decider.py)
```python
# Before: Unclear validation
if not (("decision" in parsed_result and parsed_result["decision"] == "accept") or "comment" in parsed_result):
    raise ValueError("Agent response missing required fields")

# After: Clear validation with helpful error message
has_accept = parsed_result.get("decision") == "accept"
has_comment = "comment" in parsed_result and parsed_result["comment"]

if not (has_accept or has_comment):
    raise ValueError(
        f"Agent response must have either 'decision: accept' OR 'comment'. "
        f"Got: {parsed_result}"
    )
```

### 3. Simplified Flow (jedimaster.py)
Restructured the PR review flow to handle all cases cleanly:

1. **If approved and mergeable** → Merge immediately
2. **If approved but not mergeable** → Check comment count and either escalate or skip
3. **If changes needed** → Extract comment (or use fallback)
4. **Check comment limit** → Escalate if exceeded
5. **Check Copilot slots** → Skip if full
6. **Request changes** → Submit review with agent's comment

Removed duplicate comment extraction and validation logic.

## Testing
- Syntax validated with `python -m py_compile`
- The fix should handle both agent response formats correctly:
  - `{'decision': 'accept'}` → Will merge or handle appropriately
  - `{'comment': 'feedback'}` → Will request changes
- Exponential backoff retry logic remains in place in `decider.py` for service errors

## Impact
- PRs with `decision: 'accept'` will no longer be incorrectly skipped
- Better error messages when agent response is truly invalid
- Cleaner, more maintainable code with less duplication
- Agent should produce more consistent responses with clearer examples
