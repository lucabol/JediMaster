# Agent Architecture Analysis for Orchestration

## Current Agents Review

### 1. DeciderAgent
**Current Purpose**: Evaluates individual issues to decide if suitable for Copilot
**How it works**: Takes one issue → LLM call → yes/no decision + reasoning

**Fit for Orchestration?** 
- ✅ **Correct level**: Makes tactical decisions (this issue suitable?)
- ✅ **Well-scoped**: Single responsibility
- ❌ **Inefficient**: One LLM call per issue (expensive at scale)
- ✅ **Good for orchestrated flow**: Orchestrator decides WHEN to use it

**Verdict**: **KEEP but consider batch optimization later**

### 2. PRDeciderAgent
**Current Purpose**: Reviews individual PRs to decide approve/request-changes
**How it works**: Takes one PR + diff → LLM call → approve or comment

**Fit for Orchestration?**
- ✅ **Correct level**: Makes tactical decisions (this PR good?)
- ✅ **Well-scoped**: Single responsibility
- ❌ **Inefficient**: One LLM call per PR
- ✅ **Good for orchestrated flow**: Orchestrator decides WHEN to use it

**Verdict**: **KEEP but consider batch optimization later**

### 3. CreatorAgent
**Current Purpose**: Suggests and creates new issues for repository
**How it works**: Analyzes repo → LLM call → N issue suggestions → opens them

**Fit for Orchestration?**
- ✅ **Correct level**: Generates new work
- ✅ **Well-scoped**: Single responsibility
- ✅ **Already batched**: Creates multiple issues in one call
- ✅ **Good for orchestrated flow**: Orchestrator decides WHEN to use it

**Verdict**: **KEEP - already good design**

## What's Missing for Orchestration?

### Current Architecture
```
DeciderAgent      → Decides: Is this issue suitable?
PRDeciderAgent    → Decides: Is this PR good?
CreatorAgent      → Decides: What issues should exist?
```

**Problem**: No agent decides **WHAT TO DO NEXT** strategically.

### Needed: Strategic vs. Tactical Separation

#### Strategic Level (NEW - Orchestrator)
**Question**: "Given the current state, what workflows should we run?"
- Looks at big picture (whole repository)
- Considers constraints (API limits, Copilot capacity)
- Makes strategic trade-offs (merge vs. review vs. create)
- Returns execution plan

#### Tactical Level (EXISTING - Decider/PRDecider/Creator)
**Question**: "For this specific item, what's the right decision?"
- Looks at individual items (one issue, one PR)
- Makes yes/no or approve/reject decisions
- Returns tactical action for that item

### The Orchestrator Agent (NEW)

```python
class OrchestratorAgent:
    """Strategic planner - decides what workflows to execute."""
    
    async def create_execution_plan(
        self,
        repo_state: RepoState,      # What's the current state?
        resources: ResourceState,    # What can we do?
        workload: PrioritizedWorkload  # What needs attention?
    ) -> ExecutionPlan:
        """
        Makes strategic decisions:
        - Should we merge PRs or review them?
        - Should we assign new issues or wait?
        - Should we create new issues or clear backlog first?
        
        Returns plan with workflows + batch sizes.
        """
```

**This is different from existing agents because**:
- Existing agents: Tactical (individual items)
- Orchestrator: Strategic (whole repository)

## Proposed Architecture: Two Layers

```
┌─────────────────────────────────────────────────────────┐
│                  STRATEGIC LAYER                         │
│                                                          │
│  OrchestratorAgent (NEW)                                │
│  ├─ Input: RepoState, ResourceState, PrioritizedWorkload│
│  ├─ Decides: Which workflows? What order? Batch sizes?  │
│  └─ Output: ExecutionPlan                               │
│                                                          │
└─────────────────────────────────────────────────────────┘
                          │
              ┌───────────┼───────────┐
              │           │           │
              ▼           ▼           ▼
┌──────────────────────────────────────────────────────────┐
│                   TACTICAL LAYER                         │
│                                                          │
│  DeciderAgent (EXISTING)                                │
│  ├─ Evaluates: Is this issue suitable for Copilot?      │
│  └─ Called by: process_issues workflow                   │
│                                                          │
│  PRDeciderAgent (EXISTING)                              │
│  ├─ Evaluates: Should we approve this PR?               │
│  └─ Called by: review_prs workflow                       │
│                                                          │
│  CreatorAgent (EXISTING)                                │
│  ├─ Generates: What new issues should we create?        │
│  └─ Called by: create_issues workflow                    │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

## Agent Roles in Orchestration

### Orchestrator's Role
**"What should we do next?"**

Example decisions:
- "We have 5 PRs ready to merge → run merge_ready_prs(5)"
- "Copilot at capacity → SKIP process_issues"
- "API quota low → only merge, skip everything else"
- "Backlog healthy → run create_issues(3)"

### DeciderAgent's Role (Unchanged)
**"Is this specific issue suitable for Copilot?"**

Example decisions:
- Issue #42: "Yes - implements new feature" → assign to Copilot
- Issue #43: "No - discussion about architecture" → label no-copilot

**When called**: When orchestrator runs `process_issues` workflow

### PRDeciderAgent's Role (Unchanged)
**"Should we approve this specific PR?"**

Example decisions:
- PR #100: "Approve - looks good" → approve
- PR #101: "Request changes - missing tests" → request changes

**When called**: When orchestrator runs `review_prs` workflow

### CreatorAgent's Role (Unchanged)
**"What new issues should we create?"**

Example decisions:
- Analyzes repo → suggests 5 issues
- Checks for duplicates → opens 3 unique issues

**When called**: When orchestrator runs `create_issues` workflow

## Why This Design Works

### 1. Clean Separation of Concerns
- **Strategic** (Orchestrator): Whole repository, resource management
- **Tactical** (Existing agents): Individual items, specific decisions

### 2. Reuses Existing Agents
- DeciderAgent, PRDeciderAgent, CreatorAgent work as-is
- No changes to their logic
- Orchestrator just decides when to invoke them

### 3. Single LLM Call for Planning
- Orchestrator: 1 LLM call per run (strategic planning)
- Then N tactical calls (based on plan)
- Still more efficient than blindly processing everything

### 4. Natural Workflow Integration
```python
# Orchestrated flow:
plan = orchestrator.create_execution_plan(state, resources, workload)

for workflow in plan.workflows:
    if workflow.name == "process_issues":
        # Use DeciderAgent for each issue
        for issue in get_unprocessed_issues(workflow.batch_size):
            decision = await decider.evaluate_issue(issue)
            if decision == "yes":
                assign_to_copilot(issue)
    
    elif workflow.name == "review_prs":
        # Use PRDeciderAgent for each PR
        for pr in get_pending_prs(workflow.batch_size):
            decision = await pr_decider.evaluate_pr(pr)
            handle_pr_decision(pr, decision)
    
    elif workflow.name == "create_issues":
        # Use CreatorAgent
        await creator.create_issues(workflow.batch_size)
```

## Answer to Your Question

**Are Decider, PRDecider and Creator the right agents for this architecture?**

**YES - They are perfect tactical agents!**

What they do well:
- ✅ Make specific, tactical decisions
- ✅ Single responsibility (evaluate one thing)
- ✅ Work independently
- ✅ Fit naturally into workflows

What they DON'T do (and shouldn't):
- ❌ Decide which workflows to run
- ❌ Manage resources or capacity
- ❌ Prioritize across different work types
- ❌ Make strategic trade-offs

**That's what the Orchestrator is for!**

## Complete Agent Set for Orchestration

### Strategic Layer (NEW)
1. **OrchestratorAgent** (LLM-based)
   - Decides what workflows to run
   - Respects resource constraints
   - Returns execution plan

### Analytical Layer (NEW)
2. **RepoStateAnalyzer** (No LLM)
   - Counts issues/PRs by state
   - Identifies quick wins
   
3. **ResourceMonitor** (No LLM)
   - Checks GitHub API quota
   - Tracks Copilot capacity
   
4. **WorkloadPrioritizer** (No LLM)
   - Sorts items by priority
   - Identifies urgent work

### Tactical Layer (EXISTING - No Changes)
5. **DeciderAgent** (LLM-based)
   - Evaluates issue suitability
   - Used by process_issues workflow
   
6. **PRDeciderAgent** (LLM-based)
   - Reviews PRs for approval
   - Used by review_prs workflow
   
7. **CreatorAgent** (LLM-based)
   - Generates new issues
   - Used by create_issues workflow

## Future Optimization: Batch Tactical Agents

Later, we could optimize the tactical agents to work in batches:

```python
class BatchDeciderAgent:
    """Evaluates multiple issues in one LLM call."""
    
    async def evaluate_issues_batch(self, issues: List[Issue]) -> List[Decision]:
        """
        Evaluate 10 issues in one LLM call instead of 10 separate calls.
        Potential 90% reduction in LLM costs for issue processing.
        """
```

But for now, **keep the existing agents as-is**. They work, they're simple, and the orchestrator makes the system smarter without changing them.

## Recommendation

**KEEP all three existing agents (Decider, PRDecider, Creator)**

They are:
1. ✅ Correctly scoped for tactical decisions
2. ✅ Compatible with orchestration architecture
3. ✅ Working well in current system
4. ✅ Easy to optimize later (batch processing)

**ADD orchestration layer on top**:
1. Orchestrator (strategic planning)
2. Analytical agents (state analysis)
3. Integration glue (orchestrated_run method)

**Result**: Clean two-layer architecture where strategic and tactical concerns are properly separated.
