# Orchestrator Refactoring Plan

## Executive Summary

This refactoring transforms JediMaster from hardcoded decision-making to intelligent LLM-based orchestration. The orchestrator will analyze repository state, consider rate limits, and make strategic decisions about what actions to take to reduce open PRs and issues most effectively.

## Current State Analysis

### What Works Well (Keep)
1. **State Detection Logic**: All the PR/issue state classification logic in `jedimaster.py` is correct and should be maintained
2. **Agent Framework**: The DeciderAgent, PRDeciderAgent, and CreatorAgent are working well
3. **State Machine**: The PR state machine (`_process_pr_state_machine`) correctly handles PR lifecycle
4. **Metadata Collection**: `_collect_pr_metadata` and `_classify_pr_state` provide accurate state information

### What Needs To Change (Orchestrate)
1. **Hardcoded Execution**: Currently, the system blindly processes all issues/PRs in a fixed order
2. **No Prioritization**: No intelligence about which issues/PRs are most urgent
3. **No Resource Management**: Doesn't consider GitHub API or Copilot rate limits when planning work
4. **No Strategic Planning**: Can't decide "should I create issues, or focus on clearing the PR backlog?"

## Architecture Design

### Three-Tier Agent Hierarchy

```
┌─────────────────────────────────────────────────────────────┐
│                    Orchestrator Agent                        │
│  - Strategic planning (LLM-based)                           │
│  - Resource budgeting                                       │
│  - Workflow selection                                       │
│  - Priority ordering                                        │
└─────────────────────────────────────────────────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        │                   │                   │
        ▼                   ▼                   ▼
┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│ Analytical   │   │  Decision    │   │   Action     │
│   Agents     │   │   Agents     │   │   Agents     │
└──────────────┘   └──────────────┘   └──────────────┘
```

#### Tier 1: Analytical Agents (Read-Only, No LLM)
Fast, deterministic analysis of current state:

- **RepoStateAnalyzer**
  - Count open issues (total, unprocessed, assigned to Copilot, stale)
  - Count open PRs (by state: pending_review, changes_requested, ready_to_merge, blocked, done)
  - Count Copilot-involved items (issues assigned, PRs in progress)
  - Calculate staleness metrics (items > 7 days, > 30 days)
  - Identify blocked items needing human intervention
  
- **ResourceMonitor**
  - Check GitHub API rate limit (remaining calls, reset time)
  - Estimate Copilot usage (PRs in flight, review capacity)
  - Calculate available "budget" for this run
  - Warn if approaching limits

- **WorkloadPrioritizer**
  - Sort issues by urgency (age, labels, assignee status)
  - Sort PRs by urgency (state, review status, staleness)
  - Identify quick wins (ready-to-merge PRs)
  - Flag critical blockers

#### Tier 2: Decision Agents (LLM-Based, No Actions)
Use existing agents, keep current logic:

- **DeciderAgent**: Evaluate if issues are suitable for Copilot (KEEP AS-IS)
- **PRDeciderAgent**: Decide approve/request-changes on PRs (KEEP AS-IS)
- **CreatorAgent**: Suggest new issues (KEEP AS-IS)

#### Tier 3: Action Agents (No Decisions, Just Execute)
Execute the actual GitHub operations:

- **IssueProcessor**
  - Assign issues to Copilot
  - Add/remove labels
  - Close stale issues
  - Uses existing `process_issue()` logic

- **PRManager**
  - Review PRs (approve/request changes)
  - Merge PRs
  - Handle state transitions
  - Uses existing `_process_pr_state_machine()` logic

- **IssueCreator**
  - Open new issues
  - Uses existing `CreatorAgent.create_issues()` logic

### Orchestrator Agent (NEW)

**Core Responsibility**: Analyze repository state and decide the optimal workflow to reduce backlog.

**LLM Prompt Structure**:
```
You are an orchestrator managing GitHub repository automation. Your goal is to 
reduce the number of open issues and PRs efficiently while respecting rate limits.

Current Repository State:
- Open Issues: {total_issues} ({unprocessed} unprocessed, {copilot_assigned} assigned to Copilot)
- Open PRs: {total_prs}
  - Pending Review: {pending_review}
  - Changes Requested: {changes_requested}  
  - Ready to Merge: {ready_to_merge}
  - Blocked: {blocked}
- Stale Items: {stale_count} (>7 days old)

Resource Limits:
- GitHub API: {api_remaining}/{api_limit} calls remaining (resets in {reset_time})
- Estimated Copilot Capacity: {copilot_capacity} reviews available
- Current Budget: Can process ~{estimated_items} items this run

Based on this state, decide which workflows to execute and in what order.
Available workflows:
1. merge_ready_prs - Quick wins: merge already-approved PRs (highest priority)
2. review_prs - Review PRs awaiting Copilot review
3. process_issues - Evaluate and assign unprocessed issues to Copilot
4. create_issues - Suggest and open new issues (lowest priority)
5. cleanup_stale - Close stale issues/PRs (if backlog is too high)

Return a JSON plan:
{
  "reasoning": "explanation of strategy",
  "workflows": [
    {"name": "merge_ready_prs", "priority": 1, "batch_size": 5},
    {"name": "review_prs", "priority": 2, "batch_size": 3}
  ],
  "skip": ["create_issues"],
  "warnings": ["approaching API rate limit"]
}
```

**Decision Logic Examples**:

1. **Healthy Backlog Scenario**:
   - 5 open issues, 2 PRs (both ready to merge), good API quota
   - Decision: Merge PRs first, then evaluate issues, then create new issues
   
2. **Overwhelmed Scenario**:
   - 50 open issues, 20 PRs (10 pending review, 5 ready to merge), low API quota
   - Decision: Merge ready PRs only, skip everything else to reduce backlog
   
3. **Stale Cleanup Scenario**:
   - 30 open issues (20 stale), 15 PRs (10 stale), normal API quota
   - Decision: Clean up stale items first, then process fresh ones

4. **Rate Limit Constraint**:
   - 100 API calls remaining, 40 unprocessed issues
   - Decision: Process only top 10 priority issues, defer rest to next run

## Implementation Plan

### Phase 1: Create Analytical Agents (No LLM, Fast)

**File**: `agents/analytical/repo_state_analyzer.py`
```python
class RepoStateAnalyzer:
    """Analyzes repository state without making decisions."""
    
    def analyze(self, repo_name: str) -> RepoState:
        # Uses existing GitHub API calls to gather:
        # - Issue counts by state
        # - PR counts by state  
        # - Staleness metrics
        # - Returns structured data model
```

**File**: `agents/analytical/resource_monitor.py`
```python
class ResourceMonitor:
    """Monitors API quotas and system constraints."""
    
    def check_resources(self) -> ResourceState:
        # Uses existing _check_rate_limit_status()
        # Estimates available budget for this run
        # Returns structured data model
```

**File**: `agents/analytical/workload_prioritizer.py`
```python
class WorkloadPrioritizer:
    """Prioritizes items without deciding actions."""
    
    def prioritize(self, repo_state: RepoState) -> PrioritizedWorkload:
        # Sorts issues/PRs by urgency
        # Identifies quick wins
        # Returns ordered lists
```

### Phase 2: Create Orchestrator Agent (LLM-Based)

**File**: `agents/orchestrator.py`
```python
class OrchestratorAgent:
    """LLM-based strategic planner."""
    
    async def create_execution_plan(
        self, 
        repo_state: RepoState,
        resource_state: ResourceState,
        prioritized_workload: PrioritizedWorkload
    ) -> ExecutionPlan:
        # Calls LLM with repository state
        # Gets back strategic workflow plan
        # Includes reasoning for decisions
```

### Phase 3: Refactor Execution Flow

**File**: `jedimaster.py` - New method
```python
async def orchestrated_run(self, repo_name: str) -> OrchestrationReport:
    """Execute an orchestrated run on a repository."""
    
    # Step 1: Analyze (No LLM, fast)
    analyzer = RepoStateAnalyzer(self.github)
    repo_state = analyzer.analyze(repo_name)
    
    monitor = ResourceMonitor(self.github)  
    resource_state = monitor.check_resources()
    
    prioritizer = WorkloadPrioritizer()
    workload = prioritizer.prioritize(repo_state)
    
    # Step 2: Plan (LLM-based, one call)
    orchestrator = OrchestratorAgent(self.azure_foundry_endpoint)
    plan = await orchestrator.create_execution_plan(
        repo_state, resource_state, workload
    )
    
    # Step 3: Execute plan (uses existing logic)
    results = []
    for workflow in plan.workflows:
        if workflow.name == "merge_ready_prs":
            results.extend(await self._execute_merge_workflow(
                repo_name, workflow.batch_size
            ))
        elif workflow.name == "review_prs":
            results.extend(await self._execute_review_workflow(
                repo_name, workflow.batch_size
            ))
        elif workflow.name == "process_issues":
            results.extend(await self._execute_issue_workflow(
                repo_name, workflow.batch_size
            ))
        elif workflow.name == "create_issues":
            results.extend(await self._execute_creation_workflow(
                repo_name, workflow.batch_size
            ))
    
    # Step 4: Report
    return OrchestrationReport(
        repo=repo_name,
        initial_state=repo_state,
        plan=plan,
        results=results,
        final_state=analyzer.analyze(repo_name)  # Re-analyze
    )
```

### Phase 4: Data Models

**File**: `core/models.py`
```python
@dataclass
class RepoState:
    """Current state of repository."""
    repo: str
    open_issues_total: int
    open_issues_unprocessed: int
    open_issues_copilot: int
    open_issues_stale: int  # >7 days
    open_prs_total: int
    open_prs_by_state: Dict[str, int]  # {pending_review: 5, ready_to_merge: 2, ...}
    stale_prs: int
    
@dataclass  
class ResourceState:
    """Available resources."""
    github_api_remaining: int
    github_api_limit: int
    github_api_reset_time: datetime
    estimated_budget: int  # How many items we can process
    warnings: List[str]

@dataclass
class PrioritizedWorkload:
    """Prioritized work items."""
    priority_issues: List[int]  # Issue numbers, sorted by priority
    priority_prs: List[int]     # PR numbers, sorted by priority
    quick_win_prs: List[int]    # Ready to merge
    
@dataclass
class WorkflowStep:
    """A single workflow to execute."""
    name: str  # merge_ready_prs, review_prs, etc.
    priority: int
    batch_size: int
    reasoning: str

@dataclass  
class ExecutionPlan:
    """LLM-generated execution plan."""
    reasoning: str
    workflows: List[WorkflowStep]
    skip: List[str]  # Workflows to skip
    warnings: List[str]
    estimated_duration: int  # seconds
    
@dataclass
class OrchestrationReport:
    """Complete report of orchestrated run."""
    repo: str
    initial_state: RepoState
    plan: ExecutionPlan
    results: List[Any]  # Workflow results
    final_state: RepoState
    metrics: Dict[str, Any]
```

## Migration Strategy

### Step 1: Add Orchestrator Alongside Existing Code
- Create new `agents/` directory structure
- Implement analytical agents
- Implement orchestrator agent  
- Add `orchestrated_run()` method to JediMaster
- Keep existing methods unchanged

### Step 2: Add CLI Flag
```bash
# Old behavior (unchanged)
python jedimaster.py owner/repo --manage-prs

# New orchestrated behavior
python jedimaster.py owner/repo --orchestrate
```

### Step 3: Update Azure Function
Add environment variable `ENABLE_ORCHESTRATION=true` to opt-in:
```python
enable_orchestration = os.getenv('ENABLE_ORCHESTRATION', '0') == '1'

if enable_orchestration:
    report = await jedi.orchestrated_run(repo_full)
else:
    # Existing logic
    report = await jedi.process_repositories([repo_full])
```

### Step 4: Testing & Validation
- Test on small repos first
- Compare orchestrated vs. non-orchestrated outcomes
- Monitor LLM decision quality
- Tune prompts based on results

### Step 5: Gradual Rollout
- Week 1: Manual testing with `--orchestrate` flag
- Week 2: Enable for 1-2 repos in Azure Functions
- Week 3: Enable for all repos if successful
- Week 4: Make orchestration default, deprecate old flow

## Expected Benefits

### Efficiency Gains
- **Reduced Wasted Work**: Don't create issues when backlog is overwhelming
- **Quick Wins First**: Merge ready PRs immediately to show progress
- **Smart Batching**: Process only what fits in API budget
- **Stale Cleanup**: Automatically detect and clean up abandoned work

### Better Resource Management  
- **Rate Limit Aware**: Never hit GitHub API limits
- **Copilot Capacity**: Don't overwhelm Copilot with too many concurrent reviews
- **Cost Control**: One orchestration LLM call vs. many decision calls

### Improved Outcomes
- **Faster PR Merges**: Prioritize ready-to-merge PRs
- **Less Clutter**: Clean up stale items proactively
- **Balanced Load**: Create new issues only when current work is under control
- **Strategic**: Focus on highest-impact work first

## Risks & Mitigations

### Risk: LLM Makes Poor Decisions
**Mitigation**: 
- Include guardrails in prompt (e.g., "always merge ready PRs first")
- Log all LLM decisions for review
- Add override flags for manual control
- Fall back to simple heuristics if LLM call fails

### Risk: Added Complexity
**Mitigation**:
- Keep existing code paths functional
- Make orchestration opt-in initially
- Document decision-making clearly
- Provide detailed logs and reports

### Risk: Slower Execution
**Mitigation**:
- Analytical agents are fast (no LLM)
- Only one LLM call for orchestration
- Can still be faster overall by avoiding wasted work

## Success Metrics

Track these metrics to validate the refactoring:

1. **Backlog Reduction**: Time to reduce open PRs/issues by 50%
2. **API Efficiency**: API calls per item processed (should decrease)
3. **LLM Cost**: Cost per repo run (one orchestration call vs. many decision calls)
4. **Time to Merge**: Average time from PR creation to merge (should decrease)
5. **Stale Rate**: Percentage of items that go stale (should decrease)

## File Structure

```
JediMaster/
├── core/
│   ├── __init__.py
│   └── models.py                    # NEW: Data models
├── agents/
│   ├── __init__.py
│   ├── orchestrator.py              # NEW: Main orchestrator
│   ├── analytical/
│   │   ├── __init__.py
│   │   ├── repo_state_analyzer.py   # NEW: State analysis
│   │   ├── resource_monitor.py      # NEW: Rate limit tracking
│   │   └── workload_prioritizer.py  # NEW: Priority sorting
│   ├── decision/                    # EXISTING agents
│   │   ├── __init__.py
│   │   └── (DeciderAgent, PRDeciderAgent stay in decider.py for now)
│   └── action/                      # Future: extract from jedimaster.py
│       └── __init__.py
├── jedimaster.py                    # MODIFIED: Add orchestrated_run()
├── decider.py                       # UNCHANGED
├── creator.py                       # UNCHANGED  
├── function_app.py                  # MODIFIED: Add ENABLE_ORCHESTRATION
└── example.py                       # MODIFIED: Add --orchestrate flag
```

## Implementation Timeline

- **Week 1**: Create data models and analytical agents
- **Week 2**: Implement orchestrator agent with LLM
- **Week 3**: Add `orchestrated_run()` to JediMaster
- **Week 4**: Add CLI flags and test on sample repos
- **Week 5**: Update Azure Functions, gradual rollout
- **Week 6**: Monitoring, tuning, documentation

## Open Questions

1. **LLM Model Selection**: Should orchestrator use a different (faster) model than DeciderAgent?
2. **Caching**: Should we cache repository state between runs to detect trends?
3. **Multi-Repo**: Should orchestrator handle multiple repos in one plan?
4. **Learning**: Should we track outcomes and feed back to improve future plans?
5. **Human Override**: What's the best UX for humans to override orchestrator decisions?

## Next Steps

Before starting implementation, please review and provide feedback on:

1. Is the three-tier architecture appropriate?
2. Should any existing logic be changed, or only orchestration added?
3. Are the proposed workflows (merge, review, process, create, cleanup) the right granularity?
4. Should orchestration be opt-in or eventually become the default?
5. Any other concerns or suggestions?
