# Copilot Error Recovery & Rate Limit Management

## Current Issues

### Problem 1: Copilot Rate Limit Errors in PRs
Many PRs show this error:
```
Copilot stopped work on behalf of lucabol_microsoft due to an error 44 minutes ago
Sorry, you've hit a rate limit that restricts the number of Copilot model requests you can make within a specific time period. 
Please try again in 1 minute.
```

**Impact**: These PRs are stuck and not being retried automatically.

### Problem 2: Too Many Concurrent PRs
The system is creating/managing ~100 active PRs simultaneously, which:
1. Increases likelihood of rate limits
2. Makes it hard to track progress
3. Wastes resources on PRs that will fail

## Root Causes

### 1. No Copilot-Specific Rate Limit Detection
The current code only checks GitHub API rate limits (`_check_rate_limit_status()`), not Copilot's model rate limits.
Copilot has separate rate limits that are hit when it's making too many requests to the LLM.

### 2. No Recovery Mechanism for Rate-Limited PRs
When Copilot hits a rate limit and stops work, the PR stays in that state. The system needs to:
- Detect when Copilot has stopped due to rate limit
- Wait appropriate time
- Re-request review to retry

### 3. No Workload Throttling
The system processes all issues/PRs without considering:
- How many PRs Copilot is currently working on
- Whether we're hitting rate limits
- Priority of work items

## Recommended Solutions

### Solution 1: Detect Copilot Rate Limit Errors

Add detection for Copilot rate limit errors in PR comments/timeline:

```python
def _has_copilot_rate_limit_error(self, pr) -> tuple[bool, Optional[datetime]]:
    """Check if Copilot stopped work due to rate limit.
    
    Returns:
        tuple: (has_error, error_timestamp)
    """
    try:
        comments = pr.get_issue_comments()
        for comment in reversed(list(comments)):  # Check recent first
            if not comment.user or 'copilot' not in comment.user.login.lower():
                continue
            
            body = (comment.body or '').lower()
            if 'rate limit' in body and 'stopped work' in body:
                return True, comment.created_at
                
    except Exception as e:
        self.logger.error(f"Failed to check for Copilot rate limit in PR #{pr.number}: {e}")
    
    return False, None
```

### Solution 2: Implement Retry Logic

When a rate limit error is detected:

```python
def _handle_copilot_rate_limit_recovery(self, pr, error_time: datetime) -> List[PRRunResult]:
    """Recover from Copilot rate limit by waiting and re-requesting review."""
    
    # Check if enough time has passed (e.g., 5 minutes)
    wait_period = timedelta(minutes=5)
    if datetime.now(timezone.utc) - error_time < wait_period:
        return []  # Too soon to retry
    
    # Check retry count to avoid infinite loops
    retry_count = self._get_retry_count_from_labels(pr)
    if retry_count >= 3:  # Max 3 retries
        self._escalate_pr_to_human(pr, 0, 0)  # Different escalation reason
        return [PRRunResult(..., status='max_retries_exceeded')]
    
    # Re-request review from Copilot
    self._increment_retry_label(pr, retry_count)
    self._request_copilot_review(pr, "Retrying after rate limit recovery")
    
    return [PRRunResult(..., status='rate_limit_recovery_initiated')]
```

### Solution 3: Implement Workload Throttling

Limit concurrent work to prevent overwhelming Copilot:

```python
def _count_active_copilot_work(self, repo) -> int:
    """Count how many PRs Copilot is actively working on."""
    active_count = 0
    
    for pr in repo.get_pulls(state='open'):
        # Check if assigned to Copilot and not done
        is_copilot_assigned = any(
            'copilot' in a.login.lower() 
            for a in (pr.assignees or [])
        )
        
        state = self._get_state_label(pr)
        if is_copilot_assigned and state not in [STATE_DONE, STATE_BLOCKED]:
            active_count += 1
    
    return active_count

def should_assign_more_work(self, repo) -> tuple[bool, str]:
    """Determine if we should assign more issues to Copilot."""
    
    # Check GitHub API rate limits
    is_rate_limited, rate_msg = self._check_rate_limit_status()
    if is_rate_limited:
        return False, f"GitHub API rate limited: {rate_msg}"
    
    # Check active workload
    active_work = self._count_active_copilot_work(repo)
    max_concurrent = 10  # Configurable threshold
    
    if active_work >= max_concurrent:
        return False, f"Too many active PRs ({active_work}/{max_concurrent})"
    
    return True, f"OK to assign (active: {active_work}/{max_concurrent})"
```

### Solution 4: Priority-Based Processing

Process high-priority work first:

```python
def _prioritize_issues(self, issues: List) -> List:
    """Sort issues by priority for assignment."""
    
    def priority_key(issue):
        # Higher priority = lower number
        labels = [l.name.lower() for l in issue.labels]
        
        if 'critical' in labels or 'urgent' in labels:
            return 0
        elif 'bug' in labels:
            return 1
        elif 'enhancement' in labels:
            return 2
        else:
            return 3
    
    return sorted(issues, key=priority_key)
```

## Implementation Plan

### Phase 1: Detection & Monitoring (Immediate)
1. Add `_has_copilot_rate_limit_error()` function
2. Log when rate limit errors are detected
3. Add metrics collection for rate limit occurrences

### Phase 2: Recovery Mechanism (High Priority)
1. Implement retry logic with exponential backoff
2. Add retry count tracking via labels
3. Implement auto-recovery for rate-limited PRs

### Phase 3: Workload Management (Medium Priority)
1. Implement active work counting
2. Add throttling before assigning new issues
3. Configure reasonable concurrent work limits

### Phase 4: Optimization (Lower Priority)
1. Add priority-based issue processing
2. Implement smart scheduling based on time-of-day patterns
3. Add dashboard/reporting for workload visibility

## Configuration Variables

Recommended environment variables:

```bash
# Workload management
MAX_CONCURRENT_COPILOT_WORK=10  # Max PRs Copilot works on simultaneously
RATE_LIMIT_RETRY_DELAY_MINUTES=5  # Wait time before retrying rate-limited PRs
MAX_RATE_LIMIT_RETRIES=3  # Max retry attempts before escalation

# Batch processing
ISSUE_BATCH_SIZE=15  # Existing
PR_BATCH_SIZE=15  # Existing
```

## Testing Plan

1. **Test Rate Limit Detection**: Manually create test PR with rate limit error comment
2. **Test Recovery**: Verify retry logic triggers after wait period
3. **Test Throttling**: Verify system stops assigning when threshold reached
4. **Test Escalation**: Verify max retries leads to human escalation

## Metrics to Track

- Number of rate limit errors per hour/day
- Average recovery time for rate-limited PRs
- Number of concurrent active Copilot PRs
- Success rate after recovery attempts
- Number of PRs escalated due to max retries

---

## Update: copilot_work_finished_failure Detection (2025-11-03)

### Issue Fixed
PR #830 in gim-home/JediTestRepoV3 was closed with unmerged commits because the system didn't detect the `copilot_work_finished_failure` timeline event.

### Root Cause
The `_get_copilot_work_status()` method only checked for comment-based error indicators but missed the actual GitHub timeline event type `copilot_work_finished_failure`.

### Solution
Enhanced the method to detect the timeline event:

```python
elif event_type == 'copilot_work_finished_failure':
    copilot_error = f"Copilot work finished with failure at {created_at}"
    copilot_error_time = created_at
    copilot_finish = created_at
```

### Behavior
When `copilot_work_finished_failure` is detected:
1. Check comment count against MAX_COMMENTS (default: 10)
2. If under limit: Post `@copilot` retry comment and increment slot counter
3. If over limit: Add `copilot-human-review` label and escalate

### Enhanced Logging
Added detailed logging:
- "Detected Copilot error at {time}: {error}"
- "Retrying after Copilot error (comments: X/Y, slots: A/B)"
- "Escalating to human - Copilot error with too many comments"

### Impact
- No more stuck PRs due to undetected Copilot errors
- Automatic retry for transient failures
- Clear escalation path for persistent errors
- Better debugging via enhanced logs
