# Pull Request State Machine - Detailed Documentation

## Overview

JediMaster uses a state machine to manage pull requests automatically. PRs transition between states based on their current status (draft, reviews, commits, etc.) and the system takes appropriate actions at each state.

## State Labels

Each PR is labeled with exactly one `copilot-state:*` label indicating its current state:

| State | Label | Color | Description |
|-------|-------|-------|-------------|
| **intake** | (no label) | - | Initial state before classification |
| **pending_review** | `copilot-state:pending_review` | Blue (0366d6) | Awaiting Copilot review |
| **changes_requested** | `copilot-state:changes_requested` | Red (d73a49) | Awaiting author updates |
| **ready_to_merge** | `copilot-state:ready_to_merge` | Green (28a745) | Ready for merge |
| **blocked** | `copilot-state:blocked` | Gray (6a737d) | Blocked until manual action |
| **done** | `copilot-state:done` | Purple (5319e7) | Processing complete |

## State Transitions

```
┌──────────┐
│  intake  │ (PR opened/discovered)
└────┬─────┘
     │
     ├─── Draft PR? ──────────────► changes_requested (work in progress)
     │
     ├─── Has review requests? ───► pending_review
     │
     ├─── Changes requested? ─────► changes_requested
     │
     ├─── Approved + mergeable? ──► ready_to_merge
     │
     └─── Unclear state? ─────────► pending_review (default)


┌──────────────────┐
│ pending_review   │ ◄──┐
└────┬─────────────┘    │
     │                  │
     ├─── Review: Approved + mergeable? ────────► ready_to_merge
     │                  │
     ├─── Review: Changes requested? ───────────► changes_requested
     │                  │
     ├─── Draft created? ───────────────────────► changes_requested
     │                  │
     └─── Action: Submit to Copilot for review ──┘


┌───────────────────┐
│ changes_requested │ ◄──┐
└────┬──────────────┘    │
     │                   │
     ├─── New commits pushed? ──────────────────► pending_review
     │                   │
     ├─── Review re-requested? ─────────────────► pending_review
     │                   │
     ├─── Still draft? ─────────────────────────┤ (stay, Copilot working)
     │                   │
     └─── Action: Wait for author/Copilot ──────┘


┌─────────────────┐
│ ready_to_merge  │ ◄──┐
└────┬────────────┘    │
     │                 │
     ├─── Merge successful? ─────────────────► done
     │                 │
     ├─── Merge conflict? ───────────────────► changes_requested
     │                 │
     ├─── Max retries exceeded? ─────────────► blocked
     │                 │
     ├─── New commits? ──────────────────────► pending_review
     │                 │
     └─── Action: Attempt merge ─────────────┘


┌──────────┐
│ blocked  │ (requires manual intervention)
└────┬─────┘
     │
     └─── Label: copilot-human-review applied
          Action: Comment explaining blockage
          Resolution: Manual human action required


┌──────┐
│ done │ (PR closed/merged)
└──────┘
     Action: Clean up labels, close linked issues, delete branch
```

## How States Are Determined

### State Classification Logic (`_classify_pr_state`)

The system examines PR metadata to determine the appropriate state:

1. **Check if closed/merged** → `done`

2. **Check for explicit review requests** → `pending_review`
   - If there are requested reviewers AND not a draft
   - This handles cases where Copilot pushes changes and re-requests review

3. **Check for pending change requests** → `changes_requested`
   - If any reviewer requested changes
   - AND no new commits since that review
   - UNLESS it's a draft with merge conflicts (let Copilot fix)

4. **Check if ready to merge** → `ready_to_merge`
   - Has current approval
   - No new commits since approval
   - Mergeable (no conflicts)
   - Not a draft

5. **Check if needs review** → `pending_review`
   - Draft with human reviewers requested (Copilot finished)
   - Copilot review explicitly requested
   - Review required and not draft
   - New commits since last approval

6. **Draft PRs** → Usually `changes_requested`
   - Unless draft with review requests → `pending_review`
   - Considered "work in progress" where Copilot is working

7. **Default** → `pending_review`

## Metadata Collection (`_collect_pr_metadata`)

For each PR, the system collects:

### Basic Info
- `number`: PR number
- `title`: PR title
- `state`: open/closed
- `is_draft`: Draft status
- `merged`: Whether merged
- `mergeable`: Can it be merged (null/True/False)
- `mergeable_state`: GitHub's detailed merge status

### Reviews
- `latest_reviews`: Most recent review from each reviewer
- `latest_copilot_review`: Most recent Copilot review
- `latest_copilot_state`: APPROVED/CHANGES_REQUESTED/etc.
- `approved_by`: List of approvers
- `review_decision`: APPROVED/CHANGES_REQUESTED/REVIEW_REQUIRED

### Reviewers
- `requested_reviewers`: Who is asked to review
- `copilot_review_requested`: Is Copilot specifically requested

### Commits
- `last_commit_sha`: SHA of latest commit
- `last_commit_time`: When last commit was made
- `has_new_commits_since_copilot_review`: Commits after last Copilot review

### Status
- `has_current_approval`: Approval valid for current commit
- `has_copilot_approval`: Copilot approved current version
- `copilot_changes_requested_pending`: Copilot requested changes, not addressed
- `any_changes_requested_pending`: Any reviewer requested changes, not addressed

## State Handlers

Each state has a handler that executes actions:

### `_handle_pending_review_state`
**Actions:**
1. Mark draft PRs as ready for review (if needed)
2. Submit PR to Copilot agent for review
3. Post review comments
4. Transition based on review outcome

### `_handle_changes_requested_state`
**Actions:**
1. Check if changes were addressed (new commits)
2. If addressed, re-request review → `pending_review`
3. Otherwise, wait (do nothing, Copilot is working)

### `_handle_ready_to_merge_state`
**Actions:**
1. Attempt to merge the PR
2. Track merge attempts with labels (`merge-attempt-1`, `merge-attempt-2`, etc.)
3. On success → `done`
4. On conflict → `changes_requested`
5. On max retries → `blocked`

### `_handle_blocked_state`
**Actions:**
1. Apply `copilot-human-review` label
2. Post escalation comment explaining why blocked
3. Wait for manual intervention

### `_handle_done_state`
**Actions:**
1. Remove merge attempt labels
2. Close linked issues
3. Delete PR branch (if from same repo, not protected)

## Detecting Copilot Work Status

### Current Method
The system infers Copilot is working by examining:
- **Draft status**: Draft PRs are considered "Copilot working"
- **Review status**: Change requests without new commits = "waiting for work"
- **Commits**: New commits after change requests = "addressed, needs review"

### Proposed Improvement: Timeline Event Detection

**Problem:** The current method doesn't distinguish between:
- Copilot actively working on a PR
- PR sitting idle waiting for action
- Copilot finished but PR still in draft

**Solution:** Examine timeline events for Copilot work markers.

#### Timeline Events to Check

GitHub PR timeline includes comment events with specific text patterns:

1. **Copilot Start Event**
   ```
   Copilot started work on behalf of {user} {time}
   ```

2. **Copilot Finish Event**  
   ```
   Copilot finished work on behalf of {user} {time}
   ```

3. **Copilot Stop Event (Error)**
   ```
   Copilot stopped work on behalf of {user} due to an error {time}
   {error message}
   ```

#### Detection Algorithm

```python
def _get_copilot_work_status(pr) -> Dict[str, Any]:
    """
    Analyze timeline to determine if Copilot is actively working.
    
    Returns:
        {
            'is_working': bool,  # Copilot currently working
            'last_start': datetime or None,
            'last_finish': datetime or None,
            'last_error': str or None,
            'error_time': datetime or None
        }
    """
    timeline = pr.as_issue().get_timeline()
    
    copilot_start = None
    copilot_finish = None
    copilot_error = None
    copilot_error_time = None
    
    for event in timeline:
        if event.event != 'commented':
            continue
            
        body = getattr(event, 'body', '') or ''
        created_at = getattr(event, 'created_at', None)
        
        # Check for start event
        if 'Copilot started work' in body:
            copilot_start = created_at
            
        # Check for finish event  
        elif 'Copilot finished work' in body:
            copilot_finish = created_at
            
        # Check for error/stop event
        elif 'Copilot stopped work' in body and 'error' in body:
            copilot_error = body
            copilot_error_time = created_at
    
    # Copilot is working if:
    # 1. There's a start event
    # 2. No finish event after the start OR finish is before start
    # 3. No error after the start (or error is before start)
    
    is_working = False
    if copilot_start:
        # Check if there's a more recent finish/error
        if copilot_finish and copilot_finish > copilot_start:
            is_working = False  # Finished after starting
        elif copilot_error_time and copilot_error_time > copilot_start:
            is_working = False  # Stopped with error after starting
        else:
            is_working = True  # Started but not finished/errored
    
    return {
        'is_working': is_working,
        'last_start': copilot_start,
        'last_finish': copilot_finish,
        'last_error': copilot_error,
        'error_time': copilot_error_time
    }
```

#### Benefits

1. **Accurate Capacity Tracking**: Know exactly how many PRs Copilot is working on
2. **Better State Classification**: Don't review PRs Copilot is still working on
3. **Error Detection**: Identify PRs where Copilot hit errors and stopped
4. **Rate Limit Handling**: Detect rate limit errors and wait before assigning more work

#### Integration with State Machine

```python
def _classify_pr_state(pr, metadata):
    # Add Copilot work status to metadata
    copilot_work = self._get_copilot_work_status(pr)
    metadata['copilot_work_status'] = copilot_work
    
    # If Copilot is actively working, don't interrupt
    if copilot_work['is_working']:
        return {
            'state': STATE_CHANGES_REQUESTED,
            'reason': 'copilot_working'
        }
    
    # If Copilot stopped with error, escalate
    if copilot_work['last_error']:
        if 'rate limit' in copilot_work['last_error'].lower():
            return {
                'state': STATE_CHANGES_REQUESTED,
                'reason': 'rate_limit_wait'
            }
        else:
            return {
                'state': STATE_BLOCKED,
                'reason': f"copilot_error: {copilot_work['last_error']}"
            }
    
    # If Copilot finished, check if ready for review
    if copilot_work['last_finish']:
        if metadata['is_draft']:
            # Copilot finished but still draft - needs human to mark ready
            return {
                'state': STATE_PENDING_REVIEW,
                'reason': 'copilot_finished_needs_ready'
            }
    
    # Continue with normal classification...
```

## Special Cases

### Human Escalation

PRs are escalated to human review (`blocked` state) when:

1. **Max merge retries exceeded** (default: 3 attempts)
   - Label: `copilot-human-review`
   - Comment: "This PR has exceeded the maximum merge retry limit..."

2. **Copilot errors** (with timeline detection)
   - Label: `copilot-human-review`
   - Comment: "Copilot encountered an error: {error message}"

### Rate Limiting

When Copilot hits rate limits:
- Don't assign new issues to Copilot
- Don't request reviews from Copilot
- Wait for existing work to complete
- Monitor timeline for "finished" events

### Draft PR Handling

Draft PRs are nuanced:
- **Draft + no reviewers** = Copilot working → `changes_requested`
- **Draft + human reviewers** = Copilot done, needs approval → `pending_review`
- **Draft + merge conflicts** = Let Copilot fix → `pending_review`

## Capacity Management

The orchestrator monitors:
- **Active Copilot work**: Count PRs where `is_working = True`
- **Max capacity**: Default 10 concurrent PRs
- **Available slots**: Max - Active

Before assigning new issues:
```python
copilot_capacity = self._count_copilot_active_work(repo)
if copilot_capacity >= MAX_COPILOT_CAPACITY:
    # Don't assign new issues, let existing work complete
    skip_issue_assignment = True
```

## Error Recovery

### Detecting Stuck States

Timeline-based detection identifies:
1. PRs where Copilot started but never finished (>24 hours)
2. PRs where Copilot stopped with errors
3. PRs sitting in draft after Copilot finished

### Recovery Actions

1. **Stale starts**: Comment asking human to check progress
2. **Error stops**: Escalate to human review
3. **Finished drafts**: Mark as ready for review automatically

## Implementation Files

- **jedimaster.py**: Main state machine logic
  - `_process_pr_state_machine()`: Entry point
  - `_classify_pr_state()`: State determination
  - `_collect_pr_metadata()`: Metadata gathering
  - `_handle_*_state()`: State-specific actions

- **agents/orchestrator.py**: High-level workflow orchestration
  - Decides when to run PR management
  - Enforces capacity limits
  - Prioritizes work

- **agents/analytical/resource_monitor.py**: Resource tracking
  - GitHub API quota
  - Copilot capacity
  - Rate limit detection

## Metrics & Monitoring

The state machine tracks:
- **Transitions**: State changes per PR
- **Actions**: Reviews, merges, escalations
- **Errors**: Failed operations
- **Timing**: Time in each state

This data feeds the orchestrator's decision-making.
