# Pull Request State Machine Documentation

## Overview

The JediMaster PR state machine automatically manages pull requests through their lifecycle, from initial creation to merge or escalation to humans. It uses labels to track state and applies appropriate actions based on PR conditions.

## State Definitions

The system defines 6 states, tracked via labels with the prefix `copilot-state:`:

| State | Label | Color | Description |
|-------|-------|-------|-------------|
| **INTAKE** | (implicit) | N/A | Initial state before classification |
| **PENDING_REVIEW** | `copilot-state:pending_review` | 🔵 Blue (#0366d6) | Awaiting Copilot review |
| **CHANGES_REQUESTED** | `copilot-state:changes_requested` | 🔴 Red (#d73a49) | Awaiting author updates |
| **READY_TO_MERGE** | `copilot-state:ready_to_merge` | 🟢 Green (#28a745) | Ready for merge |
| **BLOCKED** | `copilot-state:blocked` | ⚫ Gray (#6a737d) | Blocked until manual action |
| **DONE** | `copilot-state:done` | 🟣 Purple (#5319e7) | Processing complete |

Additional labels:
- **Human Escalation**: `copilot-human-review` - PR escalated to human review
- **Merge Attempts**: `merge-attempt-1`, `merge-attempt-2`, etc. - Tracks merge retry count

## State Detection and Classification

### How States Are Determined

States are **not manually set** but **automatically classified** based on PR metadata. The system:

1. **Collects metadata** via `_collect_pr_metadata()` including:
   - Draft status
   - Mergeable state (clean, conflict, unknown)
   - Review decisions (approved, changes_requested, review_required)
   - Requested reviewers
   - Review history with timestamps
   - Commit timestamps
   - Current state label

2. **Classifies the PR** via `_classify_pr_state()` using this logic flow:

```
┌─────────────────────────────────────────────────────────┐
│                    Classification Logic                 │
└─────────────────────────────────────────────────────────┘

Is PR closed or merged?
  └─> YES → STATE_DONE (reason: pr_closed)
  └─> NO ↓

Are there explicit review requests AND not draft?
  └─> YES → STATE_PENDING_REVIEW (reason: review_requested)
  └─> NO ↓

Are there pending change requests (from any reviewer)?
  └─> YES ↓
      Are there new commits since the change request?
        └─> YES → STATE_PENDING_REVIEW (reason: changes_addressed)
        └─> NO → STATE_CHANGES_REQUESTED (reason: awaiting_author)
  └─> NO ↓

Is PR in draft?
  └─> YES → STATE_CHANGES_REQUESTED (reason: draft_in_progress)
  └─> NO ↓

Does PR have current approval AND no new commits since approval?
  └─> YES → STATE_READY_TO_MERGE (reason: approved_ready)
  └─> NO ↓

Is PR not mergeable (conflict)?
  └─> YES → STATE_BLOCKED (reason: merge_conflict)
  └─> NO ↓

DEFAULT → STATE_PENDING_REVIEW (reason: awaiting_initial_review)
```

### Key Metadata Fields

The classification depends on these computed metadata fields:

- `mergeable`: `'MERGEABLE'`, `'CONFLICTING'`, or `None`
- `is_draft`: Boolean
- `has_current_approval`: Boolean - has approval AND no commits since
- `has_new_commits_since_copilot_review`: Boolean
- `has_new_commits_since_any_review`: Boolean
- `copilot_changes_requested_pending`: Boolean - Copilot requested changes, not addressed
- `any_changes_requested_pending`: Boolean - Any reviewer requested changes, not addressed
- `copilot_review_requested`: Boolean
- `requested_reviewers`: List of usernames

## State Persistence

### How States Are Stored

States are persisted as **GitHub labels** on the PR:

1. **Reading state**: `_get_state_label(pr)` - finds labels starting with `copilot-state:` prefix
2. **Writing state**: `_set_state_label(pr, state)` - removes old state labels and adds new one
3. **Label creation**: Labels are auto-created on the repo if they don't exist

Example:
```python
# PR initially has no state label → state is None (INTAKE)
current_state = self._get_state_label(pr)  # Returns None

# Classification determines it should be PENDING_REVIEW
desired_state = classification['state']  # 'pending_review'

# Label is applied to PR
self._set_state_label(pr, desired_state)
# → Adds label: copilot-state:pending_review
```

### State Transitions

When processing a PR in `_process_pr_state_machine()`:

1. Collect current metadata
2. Classify what state it **should** be in
3. Read what state label it **currently** has
4. If states differ, transition:
   - Remove old state label
   - Add new state label
   - Log transition as `PRRunResult` with `state_before` and `state_after`
5. Call the appropriate state handler

## State Handlers

Each state has a handler function that implements the behavior for PRs in that state:

### 1. PENDING_REVIEW Handler
**Function**: `_handle_pending_review_state()`

**What it does**:
- If PR is already approved → transition to READY_TO_MERGE and call that handler
- Otherwise, **request Copilot to review**:
  - Fetches PR diff
  - Calls `PRDeciderAgent` with PR content
  - Posts review comment based on agent decision
  - May request changes or approve

**Transitions**:
- Has approval → READY_TO_MERGE
- Agent requests changes → stays PENDING_REVIEW (classification will move to CHANGES_REQUESTED on next iteration)

### 2. CHANGES_REQUESTED Handler
**Function**: `_handle_changes_requested_state()`

**What it does**:
- **If new commits since review** → transition to PENDING_REVIEW
- **If draft PR** → posts "draft in progress" comment
- **Otherwise** → posts "awaiting author" comment

**Special cases**:
- `reason='draft_in_progress'`: Copilot is working on draft
- `reason='awaiting_author'`: Waiting for human to push changes

**Transitions**:
- New commits detected → PENDING_REVIEW

### 3. READY_TO_MERGE Handler
**Function**: `_handle_ready_to_merge_state()`

**What it does**:
- **If `manage_prs=False`** → just log as ready (external merge expected)
- **If `manage_prs=True`**:
  - Checks merge attempt count
  - If under retry limit:
    - Attempts to merge PR
    - On success → transitions to DONE
    - On failure → increments attempt counter, may escalate
  - If over retry limit → escalates to human

**Transitions**:
- Merge successful → DONE
- Merge conflict/failure → may escalate to human or retry

### 4. BLOCKED Handler
**Function**: `_handle_blocked_state()`

**What it does**:
- Logs the blocking reason
- Adds `copilot-human-review` label
- Removes all other labels
- Posts explanatory comment

This state is rare after recent classification improvements.

**Transitions**:
- None (manual intervention required)

### 5. DONE Handler
**Function**: `_handle_done_state()`

**What it does**:
- Cleanup: removes merge-attempt labels
- No further action

## Entry Point and Processing Flow

### Main Entry Point
**Function**: `manage_pull_requests(repo_name, batch_size)`

```python
async def manage_pull_requests(self, repo_name: str, batch_size: int = 15):
    # 1. Get open PRs (limited by batch_size)
    pulls = list(repo.get_pulls(state='open'))[:batch_size]
    
    # 2. Process each PR through state machine
    for pr in pulls:
        pr_results = await self._process_pr_state_machine(pr)
        results.extend(pr_results)
    
    return results
```

### State Machine Processing
**Function**: `_process_pr_state_machine(pr)`

```
┌──────────────────────────────────────────────────────────┐
│              _process_pr_state_machine(pr)               │
└──────────────────────────────────────────────────────────┘
                           │
                           ▼
         ┌─────────────────────────────────┐
         │ Skip if human escalation label  │
         └─────────────────────────────────┘
                           │
                           ▼
         ┌─────────────────────────────────┐
         │ Skip if assigned to human only  │
         │ (no Copilot assignment)         │
         └─────────────────────────────────┘
                           │
                           ▼
         ┌─────────────────────────────────┐
         │ Check if should escalate based  │
         │ on comment count/type           │
         └─────────────────────────────────┘
                           │ Not escalated
                           ▼
         ┌─────────────────────────────────┐
         │ Collect PR metadata             │
         │ (_collect_pr_metadata)          │
         └─────────────────────────────────┘
                           │
                           ▼
         ┌─────────────────────────────────┐
         │ Classify desired state          │
         │ (_classify_pr_state)            │
         └─────────────────────────────────┘
                           │
                           ▼
         ┌─────────────────────────────────┐
         │ Get current state label         │
         │ (_get_state_label)              │
         └─────────────────────────────────┘
                           │
                           ▼
              Is current_state None?
                    │           │
               YES  │           │ NO
                    ▼           ▼
         ┌──────────────┐  ┌─────────────────────┐
         │ Initial      │  │ Does current match  │
         │ state        │  │ desired?            │
         │ assignment   │  └─────────────────────┘
         └──────────────┘       │            │
                    │      YES  │            │ NO
                    │           ▼            ▼
                    │    ┌──────────┐  ┌────────────┐
                    │    │ No-op    │  │ Transition │
                    │    └──────────┘  │ state      │
                    │                  └────────────┘
                    │                        │
                    └────────────────────────┘
                                │
                                ▼
                ┌───────────────────────────────┐
                │ Call handler for current      │
                │ state (handler_map[state])    │
                └───────────────────────────────┘
                                │
                                ▼
         ┌─────────────────────────────────────────┐
         │ Handler may:                            │
         │ - Request review (PENDING_REVIEW)       │
         │ - Post comment (CHANGES_REQUESTED)      │
         │ - Attempt merge (READY_TO_MERGE)        │
         │ - Escalate (BLOCKED)                    │
         │ - Cleanup (DONE)                        │
         │                                         │
         │ Handler may also transition to another  │
         │ state and recursively call that handler │
         └─────────────────────────────────────────┘
                                │
                                ▼
                ┌───────────────────────────────┐
                │ Return List[PRRunResult]      │
                │ with all actions taken        │
                └───────────────────────────────┘
```

## State Transition Diagram

```
                    ┌─────────────────┐
                    │     INTAKE      │
                    │   (no label)    │
                    └────────┬────────┘
                             │ initial classification
                             ▼
            ┌────────────────────────────────┐
            │                                │
    ┌───────▼─────────┐            ┌────────▼─────────┐
    │ PENDING_REVIEW  │            │ CHANGES_REQUESTED│
    │   (blue label)  │            │   (red label)    │
    └───────┬─────────┘            └────────┬─────────┘
            │                                │
            │ approved                       │ new commits
            │                                │
            │         ┌──────────────────────┘
            │         │
            │         │ new commits/
            │         │ re-review
            └─────┐   │
                  ▼   ▼
         ┌─────────────────────┐
         │  READY_TO_MERGE     │
         │   (green label)     │
         └──────────┬──────────┘
                    │
                    │ merge success
                    ▼
         ┌─────────────────────┐
         │       DONE          │
         │   (purple label)    │
         └─────────────────────┘

         ┌─────────────────────┐
         │      BLOCKED        │ (rare - escalated to human)
         │   (gray label)      │
         └─────────────────────┘
```

## How State Changes Are Detected

The system does **not** listen to webhooks or events. Instead:

1. **Polling Model**: The orchestrator or `manage_pull_requests()` periodically processes PRs
2. **Fresh Metadata**: Each iteration collects fresh PR metadata from GitHub API
3. **Reclassification**: Every PR is reclassified based on current conditions
4. **Change Detection**: 
   - New commits detected by comparing commit timestamps to review timestamps
   - Review state changes detected by querying review decision
   - Mergeable state queried from GitHub

### Examples of State Change Detection

**Example 1: Author pushes changes**
```
Iteration N:
  - PR has review requesting changes (timestamp: T1)
  - Last commit: T0 (before T1)
  - Classification: CHANGES_REQUESTED
  
Iteration N+1:
  - PR has review requesting changes (timestamp: T1)  
  - Last commit: T2 (after T1)  ← NEW COMMIT DETECTED
  - metadata['has_new_commits_since_any_review'] = True
  - Classification: PENDING_REVIEW  ← STATE CHANGE
```

**Example 2: Review approval**
```
Iteration N:
  - No reviews yet
  - Classification: PENDING_REVIEW
  - Handler: Copilot reviews PR, approves
  
Iteration N+1:
  - Has approval, no commits since approval
  - metadata['has_current_approval'] = True
  - Classification: READY_TO_MERGE  ← STATE CHANGE
```

**Example 3: Review requested**
```
Iteration N:
  - No review requests
  - Classification: PENDING_REVIEW
  
Iteration N+1:
  - metadata['requested_reviewers'] = ['copilot-swe-agent']  ← NEW REQUEST
  - Classification: PENDING_REVIEW (takes priority over other states)
```

## Escalation to Humans

PRs are escalated to human review when:

1. **Too many comments** (checked in `_should_escalate_for_human()`):
   - ≥5 merge conflict comments, OR
   - ≥10 total review comments

2. **Merge retry limit exceeded**:
   - Attempted to merge `merge_max_retries` times (default: 3)
   - Each attempt tracked with `merge-attempt-N` label

3. **Truly blocked state**:
   - Rare after classification improvements
   - Used for unexpected edge cases

**Escalation action**:
- Adds `copilot-human-review` label
- Removes all other labels
- Posts explanatory comment
- PR skipped in future iterations (until label removed)

## Agent Integration

The state machine integrates with LLM agents:

### PRDeciderAgent
**Used in**: PENDING_REVIEW handler

**Purpose**: Reviews PR and decides whether to approve or request changes

**Input**:
- PR title and description
- PR diff (unified diff format)

**Output**:
- `decision`: "APPROVE" or "REQUEST_CHANGES"
- `comment`: Review comment text

**Implementation**:
```python
# In _handle_pending_review_state()
result = await self.pr_decider.evaluate(
    pr_title=pr.title,
    pr_description=pr.body or '',
    pr_diff=diff_content
)

if result['decision'] == 'APPROVE':
    # Post approval review
elif result['decision'] == 'REQUEST_CHANGES':
    # Post changes requested review
```

### DeciderAgent
**Used in**: Issue assignment (not in PR state machine)

**Purpose**: Evaluates whether issue is suitable for GitHub Copilot

## Configuration

Key configuration options:

```python
JediMaster(
    github_token="ghp_...",
    azure_foundry_endpoint="https://...",
    manage_prs=True,      # Enable auto-merge attempts
    just_label=False,     # If True, only labels issues (no assignment)
    use_topic_filter=True # Filter repos by topic
)
```

Environment variables:
- `GITHUB_TOKEN`: GitHub API token
- `AZURE_FOUNDRY_ENDPOINT`: Azure AI endpoint for agents
- `MERGE_MAX_RETRIES`: Max merge attempts (default: 3)

## Batch Processing

The system processes PRs in batches:

```python
await jm.manage_pull_requests(
    repo_name="owner/repo",
    batch_size=15  # Process up to 15 PRs per run
)
```

This prevents overwhelming GitHub API rate limits and LLM services.

## Return Values

All processing returns `List[PRRunResult]` with detailed information:

```python
@dataclass
class PRRunResult:
    repo: str                      # "owner/repo"
    pr_number: int                 # 42
    title: str                     # "Fix bug in parser"
    status: str                    # 'state_transition', 'merged', 'error', etc.
    details: Optional[str]         # Human-readable description
    attempts: Optional[int]        # Merge attempt count
    state_before: Optional[str]    # Previous state
    state_after: Optional[str]     # New state  
    action: Optional[str]          # 'classify', 'merge_success', etc.
```

## Orchestrator Integration

The orchestrator uses this state machine by calling:

```python
# Review workflow - processes PRs needing review
await jm._execute_review_workflow(repo_name, pr_numbers)

# Merge workflow - processes PRs ready to merge  
await jm._execute_merge_workflow(repo_name, pr_numbers)

# Or full state machine for all PRs
await jm.manage_pull_requests(repo_name, batch_size)
```

The orchestrator queries PR states to decide which workflows to run based on repository health metrics.

## Summary

The PR state machine is a **classification-based, label-driven** system that:

1. **Collects** fresh metadata from GitHub on each iteration
2. **Classifies** PRs into appropriate states based on metadata rules
3. **Persists** state as GitHub labels
4. **Transitions** states by updating labels when classification changes
5. **Handles** each state with specific actions (review, merge, comment, escalate)
6. **Detects** changes through metadata comparison (new commits, reviews, etc.)
7. **Escalates** to humans when stuck or exceeding retry limits

No webhooks, no events - just periodic polling and intelligent reclassification based on current PR conditions.
