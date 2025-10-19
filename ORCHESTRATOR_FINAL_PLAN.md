# Orchestrator Refactoring - Final Plan

## Executive Summary

Transform JediMaster from hardcoded sequential processing to intelligent LLM-based orchestration. The orchestrator analyzes repository state, respects resource constraints (GitHub API and Copilot capacity), and strategically decides which workflows to execute to reduce the backlog most effectively.

## Core Principle: Copilot Is Always Working

**Key Insight**: If Copilot has been assigned an issue, it's working on it. The only "stuck" state is when a PR has exceeded `MERGE_MAX_RETRIES` and moved to blocked state. Otherwise, we trust Copilot is making progress.

### What This Means:
- ❌ No "stale" issue tracking (Copilot is working on it)
- ❌ No "stuck" issue detection (Copilot is working on it)
- ❌ No timeout-based cleanup (Copilot is working on it)
- ✅ Only track actual blocked state: PRs that exceeded merge retries
- ✅ Trust Copilot's capacity limits to prevent overload
- ✅ Focus orchestrator on new work assignment and PR pipeline

## Architecture: Analytical Agents Supporting Orchestrator

### Orchestrator Agent (LLM-Based)
**Role**: Strategic planner that decides optimal workflows based on current state and constraints

**Input**: Repository state, resource availability, prioritized workload
**Output**: Execution plan (which workflows, what batch sizes, what order)
**LLM Calls**: 1 per orchestration run

### Supporting Analytical Agents (No LLM, Fast)

These agents provide data to the orchestrator:

#### 1. **RepoStateAnalyzer**
**Purpose**: Snapshot of current repository state
**Provides**:
- Issue counts by state (open, unprocessed, assigned to Copilot)
- PR counts by state (pending_review, changes_requested, ready_to_merge, blocked, done)
- Blocked PRs (exceeded MERGE_MAX_RETRIES - the ONLY stuck state)
- Quick wins available (PRs ready to merge)

**Does NOT track**: Age/staleness (Copilot is working on everything)

```python
@dataclass
class RepoState:
    """Current repository state."""
    repo: str
    timestamp: datetime
    
    # Issues
    open_issues_total: int
    open_issues_unprocessed: int  # No copilot labels yet
    open_issues_assigned_to_copilot: int
    
    # PRs by state (from copilot-state labels)
    open_prs_total: int
    prs_pending_review: int
    prs_changes_requested: int
    prs_ready_to_merge: int
    prs_blocked: int  # Exceeded MERGE_MAX_RETRIES
    prs_done: int
    
    # Quick stats
    copilot_active_issues: int  # Issues assigned to Copilot
    copilot_active_prs: int     # PRs Copilot is working on
    quick_wins_available: int   # PRs ready to merge (immediate wins)
    truly_blocked_prs: int      # PRs that exceeded retry limit
```

#### 2. **ResourceMonitor**
**Purpose**: Track available resources and capacity constraints
**Provides**:
- GitHub API rate limits
- Copilot capacity (how many issues Copilot is handling)
- Available capacity (can we assign more work?)
- Warnings (low API quota, Copilot at capacity)

```python
@dataclass
class ResourceState:
    """Available resources and capacity."""
    # GitHub API
    github_api_remaining: int
    github_api_limit: int
    github_api_reset_at: datetime
    estimated_api_budget: int  # How many items we can safely process
    
    # Copilot Capacity
    copilot_assigned_issues: int      # Current workload
    copilot_max_concurrent: int       # Capacity limit (configurable)
    copilot_available_slots: int      # Can assign N more issues
    copilot_active_prs: int           # PRs in flight
    
    # Warnings
    warnings: List[str]  # e.g., "Low API quota", "Copilot at capacity"
```

#### 3. **WorkloadPrioritizer**
**Purpose**: Sort and prioritize work items for optimal processing
**Provides**:
- Quick wins (PRs ready to merge - highest ROI)
- Urgent items (blocked PRs needing human intervention)
- Normal items (regular issues/PRs to process)

**Prioritization Logic**:
1. **Quick Wins**: PRs in ready_to_merge state (merge immediately)
2. **Blocked PRs**: PRs that exceeded retry limit (need human help)
3. **Pending Reviews**: PRs awaiting Copilot review
4. **Changes Requested**: PRs waiting for Copilot updates
5. **Unprocessed Issues**: New issues needing evaluation

```python
@dataclass
class PrioritizedWorkload:
    """Work items sorted by priority."""
    quick_wins: List[int]        # PR numbers ready to merge (do first!)
    blocked_prs: List[int]        # PR numbers exceeded retry limit (flag for human)
    pending_review_prs: List[int] # PR numbers needing review
    changes_requested_prs: List[int]  # PR numbers Copilot updating
    unprocessed_issues: List[int] # Issue numbers needing evaluation
```

## Available Workflows

The orchestrator can execute these workflows:

### 1. **merge_ready_prs**
- Merge PRs in ready_to_merge state
- **No LLM needed** (state machine handles it)
- **Highest priority** (quick wins, immediate backlog reduction)
- Uses: Existing `_handle_ready_to_merge_state()` logic

### 2. **review_prs**
- Review PRs in pending_review state
- **Uses LLM** (PRDeciderAgent per PR)
- Approves or requests changes
- Uses: Existing `_handle_pending_review_state()` logic

### 3. **process_issues**
- Evaluate unprocessed issues for Copilot assignment
- **Uses LLM** (DeciderAgent per issue)
- Assigns suitable issues to Copilot
- Uses: Existing `process_issue()` logic

### 4. **create_issues**
- Generate and open new issues
- **Uses LLM** (CreatorAgent)
- Only when backlog is healthy
- Uses: Existing `CreatorAgent.create_issues()` logic

### 5. **flag_blocked_prs** (NEW)
- Identify PRs that exceeded MERGE_MAX_RETRIES
- Add human escalation label
- Comment explaining what's blocked
- No LLM needed (just labeling)

## Orchestrator Decision Logic

The orchestrator's LLM prompt guides strategic planning:

```
You are an orchestrator managing GitHub repository automation.
Your goal: Reduce open issues and PRs efficiently while respecting constraints.

REPOSITORY STATE:
- Open Issues: {total} ({unprocessed} unprocessed, {copilot_assigned} assigned to Copilot)
- Open PRs: {total}
  * Ready to merge: {ready} ← QUICK WINS!
  * Pending review: {pending}
  * Changes requested: {changes_req}
  * Blocked (merge retries exceeded): {blocked} ← Need human help
- Copilot active work: {copilot_issues} issues, {copilot_prs} PRs

RESOURCE CONSTRAINTS:
- GitHub API: {api_remaining}/{api_limit} calls available
- Copilot Capacity: {copilot_assigned}/{copilot_max} issues assigned
  * Available slots: {copilot_available}
  * If at capacity, don't assign more issues

IMPORTANT RULES:
1. ALWAYS merge ready PRs first (instant wins, no LLM cost)
2. If Copilot at capacity, focus on clearing PRs (review/merge)
3. Don't assign more than {copilot_available} new issues
4. If blocked PRs exist, flag them for humans
5. Only create new issues if backlog is healthy (< 20 items)
6. Respect API budget (don't plan more work than we can handle)

Available workflows:
- merge_ready_prs: Merge approved PRs (no LLM, fast)
- review_prs: Review PRs needing Copilot evaluation (uses LLM)
- process_issues: Evaluate and assign issues to Copilot (uses LLM)
- create_issues: Generate new issues (uses LLM)
- flag_blocked_prs: Mark PRs that need human intervention (no LLM)

Return JSON execution plan:
{
  "strategy": "Brief explanation of approach",
  "workflows": [
    {"name": "workflow_name", "batch_size": N, "reasoning": "why"}
  ],
  "skip_workflows": ["workflow_name"],
  "estimated_api_calls": N,
  "warnings": ["any concerns"]
}
```

## Example Scenarios

### Scenario 1: Healthy Repository
```
State:
- 5 issues (3 unprocessed, 2 assigned to Copilot)
- 3 PRs (1 ready to merge, 2 pending review)
- API: 4800/5000
- Copilot: 2/10 issues (8 slots available)

Orchestrator Plan:
1. merge_ready_prs (1 PR) - Quick win
2. review_prs (2 PRs) - Clear review queue
3. process_issues (3 issues) - Assign to Copilot
4. create_issues (3 new) - Backlog healthy, can add work

Result: Processed everything, added new work
```

### Scenario 2: Copilot at Capacity
```
State:
- 15 issues (5 unprocessed, 10 assigned to Copilot)
- 8 PRs (2 ready to merge, 5 pending review, 1 blocked)
- API: 4500/5000
- Copilot: 10/10 issues (0 slots available!)

Orchestrator Plan:
1. merge_ready_prs (2 PRs) - Quick wins, free Copilot
2. flag_blocked_prs (1 PR) - Alert humans
3. review_prs (5 PRs) - Help clear Copilot's backlog
4. SKIP process_issues - Copilot at capacity
5. SKIP create_issues - Copilot overwhelmed

Result: Cleared PRs to free Copilot capacity, no new assignments
```

### Scenario 3: Low API Quota
```
State:
- 10 issues (8 unprocessed)
- 5 PRs (3 ready to merge, 2 pending)
- API: 100/5000 (very low!)
- Copilot: 2/10 issues

Orchestrator Plan:
1. merge_ready_prs (3 PRs) - Highest value, low API cost
2. SKIP everything else - Conserve API quota

Result: 3 PRs merged with minimal API usage
```

### Scenario 4: Blocked PRs
```
State:
- 8 issues (all assigned to Copilot)
- 10 PRs (0 ready, 5 pending, 3 blocked after retry limit)
- API: 4000/5000
- Copilot: 8/10 issues

Orchestrator Plan:
1. flag_blocked_prs (3 PRs) - Alert humans
2. review_prs (5 PRs) - Move pipeline forward
3. SKIP process_issues - Wait for PRs to clear
4. SKIP create_issues - PR backlog too high

Result: Flagged blockers, cleared review queue
```

## Implementation Structure

```
agents/
├── orchestrator.py                    # LLM-based strategic planner
└── analytical/
    ├── __init__.py
    ├── repo_state_analyzer.py         # Analyze current state
    ├── resource_monitor.py            # Check API + Copilot capacity
    └── workload_prioritizer.py        # Sort by priority

core/
├── __init__.py
└── models.py                          # Data structures

jedimaster.py
└── orchestrated_run()                 # NEW: Orchestrated execution

example.py
└── --orchestrate flag                 # NEW: Opt-in CLI flag

function_app.py
└── ENABLE_ORCHESTRATION env var       # NEW: Opt-in for Azure Functions
```

## Configuration

```bash
# Copilot Capacity
COPILOT_MAX_CONCURRENT_ISSUES=10       # Max issues Copilot handles at once

# Merge Retries (existing)
MERGE_MAX_RETRIES=3                    # After this, PR is blocked

# Orchestration (new)
ENABLE_ORCHESTRATION=1                 # Enable in Azure Functions
```

## What Gets Preserved (UNCHANGED)

✅ All PR state detection (`_classify_pr_state`)
✅ All metadata collection (`_collect_pr_metadata`)
✅ All state machine handlers (`_handle_*_state`)
✅ PR state labels (copilot-state:pending_review, etc.)
✅ Merge retry tracking (MERGE_ATTEMPT_LABEL_PREFIX)
✅ DeciderAgent, PRDeciderAgent, CreatorAgent
✅ All GitHub API operations
✅ All existing workflows

**Nothing breaks - orchestration is purely additive!**

## Success Metrics

1. **API Efficiency**: API calls per item processed (expect 30-40% reduction)
2. **Quick Wins**: PRs merged per run (expect 2-3x more)
3. **Copilot Utilization**: Keep Copilot near capacity without overwhelming
4. **Backlog Reduction**: Time to clear backlog (expect 50% faster)
5. **Blocked Detection**: PRs flagged before they rot

## Migration Timeline

**Week 1**: Build analytical agents (RepoStateAnalyzer, ResourceMonitor, WorkloadPrioritizer)
**Week 2**: Build orchestrator agent with LLM
**Week 3**: Add `orchestrated_run()` method and test with `--orchestrate`
**Week 4**: Deploy to Azure Functions as opt-in (`ENABLE_ORCHESTRATION=1`)
**Week 5-6**: Monitor, tune, validate outcomes

## Key Design Decisions

### 1. No Stale/Stuck Tracking
**Decision**: Trust Copilot is working on assigned issues
**Reason**: Avoids false positives and unnecessary complexity
**Exception**: PRs that exceeded MERGE_MAX_RETRIES are genuinely blocked

### 2. Copilot Capacity Awareness
**Decision**: Track how many issues Copilot has, respect max limit
**Reason**: Prevent overload, focus on clearing work when at capacity
**Implementation**: Count issues assigned to copilot-swe-agent

### 3. Quick Wins First
**Decision**: Always merge ready PRs before anything else
**Reason**: Immediate backlog reduction, no LLM cost, high ROI
**Implementation**: Orchestrator always prioritizes merge_ready_prs

### 4. Opt-In Initially
**Decision**: New `--orchestrate` flag, old behavior unchanged
**Reason**: Safe migration, easy rollback, side-by-side validation
**Future**: Make default once proven

### 5. Minimal Agent Set
**Decision**: Only 3 analytical agents + 1 orchestrator
**Reason**: Simple, focused, easy to understand and maintain
**Agents**:
- RepoStateAnalyzer (what's the state?)
- ResourceMonitor (what can we do?)
- WorkloadPrioritizer (what should we prioritize?)
- OrchestratorAgent (what's the plan?)

## Open Questions

1. **Copilot Max Concurrent**: Is 10 issues a reasonable default?
2. **Blocked PR Handling**: Should we auto-add human-review label when retry limit hit?
3. **Backlog Threshold**: What's "healthy" - less than 20 items?
4. **API Buffer**: Using 80% of remaining quota safe enough?

## Next Steps

Ready to implement? The plan is:
1. ✅ Simplified (no stale tracking)
2. ✅ Focused (minimal agent set)
3. ✅ Practical (respects real constraints)
4. ✅ Safe (preserves all existing logic)

Please review and approve before implementation begins!
