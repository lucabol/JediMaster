# Copilot Work Detection via Timeline Events

## Summary

Implemented timeline event-based detection to accurately determine when GitHub Copilot is actively working on a PR. This replaces the previous heuristic-based approach with direct evidence from GitHub's timeline.

## Problem

Previously, we inferred Copilot's work status using heuristics:
- Draft PR = Copilot working
- Recent `@copilot` mentions = Copilot working
- Recent Copilot comments = Copilot working

**Issues:**
- Inaccurate: Drafts can be abandoned or finished
- Missed states: Copilot could finish but PR stays draft
- No error detection: Couldn't detect when Copilot stopped due to errors
- Poor capacity tracking: Overestimated active work

## Solution

Parse GitHub timeline events for explicit Copilot work markers:

### Timeline Events

GitHub adds comment events when Copilot starts/stops work:

1. **Start Event**
   ```
   Copilot started work on behalf of {user} {time}
   ```

2. **Finish Event**
   ```
   Copilot finished work on behalf of {user} {time}
   ```

3. **Error/Stop Event**
   ```
   Copilot stopped work on behalf of {user} due to an error {time}
   Sorry, you've hit a rate limit...
   ```

### Detection Algorithm

```python
def _get_copilot_work_status(pr):
    # Scan timeline for Copilot events
    for event in timeline:
        if 'copilot started work' in event.body:
            copilot_start = event.created_at
        elif 'copilot finished work' in event.body:
            copilot_finish = event.created_at
        elif 'copilot stopped work' in event.body:
            copilot_error = event.body
            copilot_error_time = event.created_at
    
    # Copilot is working if:
    # - Started AND (not finished OR finish before start)
    is_working = (copilot_start and 
                  (not copilot_finish or copilot_finish < copilot_start) and
                  (not copilot_error_time or copilot_error_time < copilot_start))
    
    return {
        'is_working': is_working,
        'last_start': copilot_start,
        'last_finish': copilot_finish,
        'last_error': copilot_error,
        'error_time': copilot_error_time
    }
```

## Implementation

### 1. Core Detection Function

**Location:** `jedimaster.py:_get_copilot_work_status()`

- Scans PR timeline events
- Identifies start/finish/error markers
- Returns structured work status dict
- Handles timezone normalization
- Logs detected events for debugging

### 2. Metadata Integration

**Location:** `jedimaster.py:_collect_pr_metadata()`

Added `copilot_work_status` to PR metadata:
```python
metadata['copilot_work_status'] = self._get_copilot_work_status(pr)
```

### 3. State Classification Updates

**Location:** `jedimaster.py:_classify_pr_state()`

Enhanced state classification with timeline data:

```python
copilot_work = metadata.get('copilot_work_status', {})
is_copilot_working = copilot_work.get('is_working', False)
copilot_error = copilot_work.get('last_error')

# If Copilot actively working, don't interrupt
if is_copilot_working:
    return {'state': STATE_CHANGES_REQUESTED, 'reason': 'copilot_working'}

# If Copilot hit rate limit, wait
if copilot_error and 'rate limit' in copilot_error.lower():
    return {'state': STATE_CHANGES_REQUESTED, 'reason': 'rate_limit_wait'}

# If Copilot hit other error, escalate
if copilot_error:
    return {'state': STATE_BLOCKED, 'reason': 'copilot_error'}

# If Copilot finished but still draft, needs human
if is_draft and copilot_work.get('last_finish'):
    return {'state': STATE_PENDING_REVIEW, 'reason': 'copilot_finished_needs_ready'}
```

### 4. Resource Monitor Update

**Location:** `agents/analytical/resource_monitor.py:_is_copilot_actively_working()`

Updated capacity tracking to use timeline:
```python
def _is_copilot_actively_working(pr):
    # Use timeline to check for Copilot work
    # Falls back to draft status if timeline unavailable
    ...
```

## Benefits

### 1. Accurate Capacity Tracking

**Before:**
```
Copilot capacity: 15/10 (overestimate based on drafts)
```

**After:**
```
Copilot capacity: 7/10 (accurate based on timeline)
Available slots: 3
```

### 2. Better State Classification

**Before:**
- Draft PR → always `changes_requested` (working)
- Couldn't tell if Copilot finished

**After:**
- Draft + Copilot working → `changes_requested`
- Draft + Copilot finished → `pending_review` (needs human)
- Draft + Copilot error → `blocked` (escalate)

### 3. Error Detection & Handling

**Rate Limit Errors:**
```
Copilot stopped work due to rate limit
→ State: changes_requested (wait, don't assign more)
→ Don't assign new issues until capacity frees up
```

**Other Errors:**
```
Copilot stopped work due to error
→ State: blocked (human review needed)
→ Label: copilot-human-review
```

### 4. Intelligent Workflow Decisions

Orchestrator can now:
- Skip PR review if Copilot actively working
- Detect when Copilot is stuck and needs help
- Respect true capacity limits
- Identify rate limit situations

## Usage

### Check Single PR

```python
jm = JediMaster(...)
repo = github.get_repo("owner/repo")
pr = repo.get_pull(123)

# Get work status
work_status = jm._get_copilot_work_status(pr)
print(f"Copilot working: {work_status['is_working']}")
print(f"Last start: {work_status['last_start']}")
print(f"Last finish: {work_status['last_finish']}")
print(f"Error: {work_status['last_error']}")
```

### Count Active Work

```python
from agents.analytical.resource_monitor import ResourceMonitor

monitor = ResourceMonitor(github)
resource_state = monitor.analyze_repository(repo_name)

print(f"Copilot active PRs: {resource_state.copilot_active_prs}")
print(f"Available slots: {resource_state.copilot_available_slots}")
```

### Debug Timeline

```bash
# Use sample script to see timeline events
python pr_timeline_sample.py --pr 123 --repo owner/repo
```

Look for:
```
- [2025-10-30 10:15:00] commented by github-actions Copilot started work...
- [2025-10-30 10:20:00] commented by github-actions Copilot finished work...
```

## Edge Cases

### 1. Multiple Start/Finish Cycles

If Copilot is asked to work multiple times:
```
Start #1 → Finish #1 → Start #2 → (still working)
```

Detection uses most recent timestamps:
- Most recent start: Start #2
- Most recent finish: Finish #1
- Working: True (Start #2 > Finish #1)

### 2. Missing Timeline Events

If timeline API fails or events are missing:
- Falls back to draft status
- Logs warning
- Conservative: assumes working to avoid over-assignment

### 3. Timeline Performance

Timeline API is expensive (requires API call per PR).

**Optimization:**
- Only called when collecting metadata (once per PR per iteration)
- Results cached in metadata dict
- Timeline events are paginated but typically small (<100 events)

### 4. Copilot Rate Limits

When Copilot hits rate limits:
```
Error: "you've hit a rate limit...try again in 1 minute"
```

System response:
1. Classifies PR as `changes_requested` (wait)
2. Don't assign new issues
3. Don't request new reviews
4. Wait for existing work to complete
5. Check again next iteration

## Testing

### Manual Test

1. Create an issue
2. Assign to Copilot
3. Watch for "Copilot started work" comment
4. Run: `python example.py --orchestrate --verbose`
5. Verify log shows: `PR #X: Copilot is actively working`
6. Wait for "Copilot finished work" comment
7. Run again
8. Verify log shows: `PR #X: Copilot finished but still draft`

### Check Capacity

```bash
python example.py --orchestrate --verbose | grep "Copilot.*capacity"
```

Should show accurate counts based on timeline.

### Simulate Rate Limit

Not easily testable without hitting actual limits, but logic is:
1. Timeline shows "Copilot stopped...rate limit"
2. PR classified as `changes_requested`
3. Orchestrator sees no available capacity
4. Skips issue assignment workflow

## Future Enhancements

### 1. Stale Work Detection

Track PRs where Copilot started but hasn't finished in >24 hours:
```python
if copilot_work['is_working'] and copilot_work['last_start']:
    time_working = now - copilot_work['last_start']
    if time_working > timedelta(hours=24):
        # Potentially stuck, escalate
```

### 2. Work Duration Metrics

Track how long Copilot takes on average:
```python
if copilot_work['last_start'] and copilot_work['last_finish']:
    duration = copilot_work['last_finish'] - copilot_work['last_start']
    # Record metric for analytics
```

### 3. Error Pattern Analysis

Categorize errors for better handling:
```python
if 'rate limit' in error:
    action = 'wait'
elif 'merge conflict' in error:
    action = 'escalate_for_conflicts'
elif 'timeout' in error:
    action = 'retry'
```

### 4. Proactive Ready-for-Review

When Copilot finishes on a draft:
```python
if is_draft and copilot_work['last_finish'] and not is_working:
    # Auto-mark as ready for review
    pr.mark_as_ready_for_review()
```

## Documentation

- **Architecture:** `PR_STATE_MACHINE_DETAILED.md`
- **This doc:** `COPILOT_WORK_DETECTION.md`
- **Code:**
  - `jedimaster.py:_get_copilot_work_status()`
  - `jedimaster.py:_classify_pr_state()`
  - `agents/analytical/resource_monitor.py:_is_copilot_actively_working()`
