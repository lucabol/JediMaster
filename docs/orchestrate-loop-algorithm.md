# JediMaster Orchestrate Loop Algorithm

This document describes the algorithm used by `--orchestrate --loop` mode.

## High-Level Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           ORCHESTRATE LOOP START                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         ITERATION #N BEGINS                                  │
│                    (For each repository in list)                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                    ┌─────────────────┴─────────────────┐
                    ▼                                   │
    ┌───────────────────────────────┐                  │
    │  STEP 0: CREATE ISSUES        │                  │
    │  (if CREATE_ISSUES=1)         │                  │
    │                               │                  │
    │  • Check for fresh repo       │                  │
    │    (only README.md)           │                  │
    │  • If fresh: create           │                  │
    │    implementation issue       │                  │
    │  • Otherwise: use CreatorAgent│                  │
    │    to suggest new issues      │                  │
    │  • Wait 10s for GitHub index  │                  │
    └───────────────┬───────────────┘                  │
                    │                                   │
                    ▼                                   │
    ┌───────────────────────────────┐                  │
    │  STEP 1: PROCESS PULL REQUESTS│                  │
    │  (manage_pull_requests)       │                  │
    │                               │                  │
    │  For each open PR:            │                  │
    │  ┌─────────────────────────┐  │                  │
    │  │ Skip if human escalated │  │                  │
    │  │ Skip if Copilot working │──┼──► Count active  │
    │  │ Handle Copilot errors   │  │     Copilot PRs  │
    │  │ Skip if closed/merged   │  │                  │
    │  │ Review & act if open    │  │                  │
    │  └─────────────────────────┘  │                  │
    │                               │                  │
    │  Actions taken:               │                  │
    │  • Merge if approved          │                  │
    │  • Request changes & reassign │                  │
    │  • Escalate to human if stuck │                  │
    └───────────────┬───────────────┘                  │
                    │                                   │
                    ▼                                   │
        ┌───────────────────────┐                      │
        │ Calculate available   │                      │
        │ Copilot slots:        │                      │
        │ MAX_SLOTS - active    │                      │
        └───────────┬───────────┘                      │
                    │                                   │
        ┌───────────┴───────────┐                      │
        ▼                       ▼                      │
  ┌─────────────┐        ┌─────────────┐              │
  │ Slots > 0   │        │ Slots = 0   │              │
  │             │        │             │              │
  │ Continue to │        │ Skip issue  │              │
  │ Step 2      │        │ processing  │              │
  └──────┬──────┘        └──────┬──────┘              │
         │                      │                      │
         ▼                      │                      │
    ┌───────────────────────────┐                      │
    │  STEP 2: PROCESS ISSUES   │◄─────────────────────┘
    │  (if slots available)     │
    │                           │
    │  For each unprocessed     │
    │  issue (up to slot limit):│
    │  ┌───────────────────────┐│
    │  │ Evaluate with LLM     ││
    │  │ (DeciderAgent)        ││
    │  │                       ││
    │  │ If suitable:          ││
    │  │   • Assign to Copilot ││
    │  │   • Add label         ││
    │  │                       ││
    │  │ If not suitable:      ││
    │  │   • Add no-copilot    ││
    │  │     label             ││
    │  └───────────────────────┘│
    │                           │
    │  Stop when:               │
    │  • Slot limit reached     │
    │  • All issues processed   │
    └───────────────┬───────────┘
                    │
                    ▼
    ┌───────────────────────────────────────────────────────────────┐
    │                   DETERMINE WORK REMAINING                     │
    │                                                                │
    │  work_remaining = TRUE if ANY of:                             │
    │    • Processable PRs exist (not needing human review)         │
    │    • Unprocessed issues exist                                 │
    │    • Copilot is actively working on PRs                       │
    │    • Issue creation failed (should retry)                     │
    │    • New issues were just created                             │
    │    • README initialization mode active                        │
    └───────────────────────────────┬───────────────────────────────┘
                                    │
                    ┌───────────────┴───────────────┐
                    ▼                               ▼
    ┌───────────────────────┐       ┌───────────────────────┐
    │  work_remaining=TRUE  │       │ work_remaining=FALSE  │
    │                       │       │                       │
    │  If --loop mode:      │       │  EXIT LOOP            │
    │  • Sleep N minutes    │       │  "All work complete!" │
    │  • Repeat iteration   │       │                       │
    └───────────┬───────────┘       └───────────────────────┘
                │
                │ (loop back)
                ▼
    ┌───────────────────────────────────────────────────────────────┐
    │                     NEXT ITERATION                             │
    └───────────────────────────────────────────────────────────────┘
```

## Detailed Algorithm Description

### Outer Loop (example.py)

The orchestrate loop runs continuously when `--loop N` is specified:

1. **Iteration Start**: Record iteration number and timestamp
2. **Process Repositories**: For each repository in the list, call `run_simplified_workflow()`
3. **Check Work Remaining**: If no repository has work remaining, exit the loop
4. **Sleep**: Wait for N minutes before next iteration
5. **Handle Interrupts**: Gracefully handle Ctrl+C with cumulative statistics

### Inner Workflow (run_simplified_workflow)

Each repository goes through this workflow:

#### Step 0: Issue Creation (Optional)
- **Trigger**: `CREATE_ISSUES=1` environment variable
- **Fresh Repo Detection**: If repo only has README.md, create "Implement project" issue
- **Normal Mode**: Use CreatorAgent to suggest and create new issues
- **Delay**: Wait 10 seconds for GitHub to index new issues

#### Step 1: Pull Request Processing
For each open PR, the state machine evaluates:

| Check | Action |
|-------|--------|
| Has human-escalation label? | Skip (count for stats) |
| Copilot actively working? | Skip (count toward slot usage) |
| Copilot hit an error? | Retry or escalate based on comment count |
| PR closed/merged? | Skip |
| Already approved by us? | Attempt merge |
| Otherwise | Review with PRDeciderAgent |

**PR Review Outcomes:**
- **Accept**: Mark ready (if draft), approve, attempt merge
- **Changes Requested**: Add comment with `@copilot`, request changes

**Merge Outcomes:**
- **Success**: Close linked issues, delete branch
- **Conflict**: Perform reverse merge, reassign to Copilot
- **Too Many Attempts**: Escalate to human

#### Step 2: Issue Processing
- Only runs if `available_slots > 0`
- **Available Slots** = `MAX_COPILOT_SLOTS` - active Copilot PRs
- For each unprocessed issue (up to slot limit):
  - Evaluate with DeciderAgent
  - If suitable: Assign to Copilot via GraphQL
  - If not suitable: Add `no-github-copilot` label

### Exit Conditions

The loop exits when **all** of the following are true:
- No processable PRs (all need human review or none exist)
- No unprocessed issues
- Copilot not actively working on any PRs
- No issues were just created
- Not in README initialization mode

## Key Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_COPILOT_SLOTS` | 10 | Max concurrent Copilot assignments |
| `MAX_COMMENTS` | 35 | Comments before human escalation |
| `CREATE_ISSUES` | 0 | Enable issue creation (0/1) |
| `CREATE_ISSUES_COUNT` | 3 | Issues to create per iteration |
| `SKIP_PR_REVIEWS` | 0 | Skip agent review, merge directly |

## State Labels

PRs are tracked with labels:
- `copilot-state-pending-review`
- `copilot-state-changes-requested`
- `copilot-state-ready-to-merge`
- `copilot-state-blocked`
- `copilot-human-review` - Escalated to human
- `copilot-merge-attempt-N` - Merge attempt counter

---

## PR State Detection Algorithm

The algorithm determines Copilot's status on a PR by analyzing the **GitHub Timeline API**. This is done in the `_get_copilot_work_status()` function.

### Data Sources

The timeline is fetched once per PR via `pr.as_issue().get_timeline()` and analyzed for these event types:

| Event Type | What It Indicates |
|------------|-------------------|
| `assigned` | Copilot was assigned to the PR |
| `copilot_work_started` | Copilot began working on the PR |
| `copilot_work_finished` | Copilot completed work successfully |
| `copilot_work_finished_failure` | Copilot stopped due to an error |
| `commented` | A comment was made (checked for Copilot patterns) |
| `committed` | A commit was pushed (checked for Copilot author) |
| `reviewed` | A review was submitted |

### Timeline Event Parsing

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         TIMELINE ANALYSIS                                    │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
    ┌───────────────────────────────────────────────────────────────┐
    │  For each event in timeline:                                   │
    │                                                                │
    │  1. 'assigned' event                                          │
    │     └─► If assignee contains 'copilot' → record assignment    │
    │                                                                │
    │  2. 'copilot_work_started' event                              │
    │     └─► Record start time                                     │
    │                                                                │
    │  3. 'copilot_work_finished' event                             │
    │     └─► Record finish time                                    │
    │                                                                │
    │  4. 'copilot_work_finished_failure' event                     │
    │     └─► Record error + error time                             │
    │     └─► Also counts as finish event                           │
    │                                                                │
    │  5. 'commented' event                                         │
    │     └─► If actor is Copilot → record last comment time        │
    │     └─► If body contains "copilot started work" → set start   │
    │     └─► If body contains "copilot finished work" → set finish │
    │     └─► If body contains "copilot stopped" + "error" → error  │
    │                                                                │
    │  6. 'committed' event                                         │
    │     └─► If author contains 'copilot' → record commit time     │
    │                                                                │
    │  7. 'reviewed' event                                          │
    │     └─► If reviewer is our bot → record review time           │
    └───────────────────────────────────────────────────────────────┘
```

### State Determination Logic

After parsing the timeline, the algorithm determines the current state:

#### "Copilot Working" Detection

```
is_working = FALSE

IF assigned but no start event (or assignment > start):
   AND no finish/error after assignment
   AND no review by us after assignment
   AND assigned within last 2 hours
   THEN is_working = TRUE   (waiting to start)

IF copilot_start exists:
   IF copilot_finish > copilot_start → is_working = FALSE
   ELSE IF copilot_error > copilot_start → is_working = FALSE
   ELSE IF copilot_comment > copilot_start → is_working = FALSE (posted result)
   ELSE IF time_since_start < 2 hours → is_working = TRUE

IF copilot committed within last 30 minutes:
   is_working = TRUE
```

#### "Copilot Error" Detection

```
IF copilot_error_time exists:
   Check for activity AFTER the error:
   - copilot_start > error_time?
   - copilot_finish > error_time?
   - copilot_assigned > error_time?
   - our_review > error_time?
   
   IF any activity after error:
      Clear the error (it's stale)
   ELSE:
      Error is still relevant → trigger retry or escalation
```

### State Detection Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    PR STATE DETECTION FLOW                                   │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
                        ┌─────────────────────────┐
                        │ Fetch PR Timeline       │
                        │ (GitHub Timeline API)   │
                        └───────────┬─────────────┘
                                    │
                                    ▼
                        ┌─────────────────────────┐
                        │ Parse all timeline      │
                        │ events, extract:        │
                        │ • last_assigned         │
                        │ • copilot_start         │
                        │ • copilot_finish        │
                        │ • copilot_error         │
                        │ • last_commit           │
                        │ • last_comment          │
                        └───────────┬─────────────┘
                                    │
              ┌─────────────────────┼─────────────────────┐
              ▼                     ▼                     ▼
    ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
    │ Human Escalated?│   │ Copilot Working?│   │ Copilot Error?  │
    │                 │   │                 │   │                 │
    │ Check for label:│   │ Evaluate:       │   │ Check if:       │
    │ copilot-human-  │   │ • assigned but  │   │ • error event   │
    │ review          │   │   not started   │   │   exists        │
    │                 │   │ • started but   │   │ • no activity   │
    │                 │   │   not finished  │   │   after error   │
    │                 │   │ • recent commit │   │                 │
    └────────┬────────┘   └────────┬────────┘   └────────┬────────┘
             │                     │                     │
             ▼                     ▼                     ▼
    ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
    │ Skip PR         │   │ Skip PR         │   │ Retry or        │
    │ (needs human)   │   │ (count as slot) │   │ Escalate        │
    └─────────────────┘   └─────────────────┘   └─────────────────┘
```

### Returned Status Object

The `_get_copilot_work_status()` function returns:

```python
{
    'is_working': bool,          # True if Copilot is actively working
    'last_start': datetime,      # When Copilot started work
    'last_finish': datetime,     # When Copilot finished work
    'last_error': str,           # Error message (if any)
    'error_time': datetime,      # When error occurred
    'last_commit': datetime,     # Last commit by Copilot
    'last_assigned': datetime,   # When Copilot was assigned
    'last_review_by_us': datetime  # When we last reviewed
}
```

### Timeout Thresholds

| Threshold | Duration | Purpose |
|-----------|----------|---------|
| Assignment timeout | 2 hours | If assigned but no start within 2 hours, assume abandoned |
| Work timeout | 2 hours | If started but no finish within 2 hours, assume abandoned |
| Recent commit | 30 minutes | If Copilot committed recently, assume still working |

### Error Handling Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      COPILOT ERROR HANDLING                                  │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
                        ┌─────────────────────────┐
                        │ Error detected in       │
                        │ copilot_work_status     │
                        └───────────┬─────────────┘
                                    │
                                    ▼
                        ┌─────────────────────────┐
                        │ Count total comments    │
                        │ on the PR               │
                        └───────────┬─────────────┘
                                    │
              ┌─────────────────────┴─────────────────────┐
              ▼                                           ▼
    ┌─────────────────────┐                   ┌─────────────────────┐
    │ comments > MAX      │                   │ comments ≤ MAX      │
    │ (default: 35)       │                   │                     │
    │                     │                   │                     │
    │ Add human-escalation│                   │ Check available     │
    │ label               │                   │ Copilot slots       │
    │ Add explanatory     │                   │                     │
    │ comment             │                   │                     │
    └─────────────────────┘                   └───────────┬─────────┘
                                                          │
                                    ┌─────────────────────┴─────────────────────┐
                                    ▼                                           ▼
                        ┌─────────────────────┐                   ┌─────────────────────┐
                        │ Slots available     │                   │ No slots available  │
                        │                     │                   │                     │
                        │ Post retry comment: │                   │ Skip (will retry    │
                        │ "@copilot Please    │                   │ next iteration)     │
                        │ retry this PR..."   │                   │                     │
                        │                     │                   │                     │
                        │ Increment slot      │                   │                     │
                        │ usage counter       │                   │                     │
                        └─────────────────────┘                   └─────────────────────┘
```
