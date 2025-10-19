# Orchestrator Design - Visual Overview

## Current vs. Proposed Architecture

### CURRENT: Hardcoded Sequential Processing

```
┌─────────────────────────────────────────────────────────┐
│                     JediMaster                          │
│                                                         │
│  for issue in fetch_issues(repo):                      │
│      decision = DeciderAgent.evaluate(issue)  ← LLM 1  │
│      if decision == "yes":                              │
│          assign_to_copilot(issue)                       │
│                                                         │
│  for pr in fetch_prs(repo):                             │
│      decision = PRDeciderAgent.evaluate(pr)   ← LLM 2  │
│      if decision == "approve":                          │
│          approve_pr(pr)                                 │
│          merge_pr(pr)                                   │
│                                                         │
│  if create_issues_enabled:                              │
│      suggestions = CreatorAgent.suggest()     ← LLM 3  │
│      open_issues(suggestions)                           │
└─────────────────────────────────────────────────────────┘

Problems:
❌ Always processes all items in fixed order
❌ No consideration of API rate limits
❌ No prioritization (stale PRs wait behind new issues)
❌ Creates new issues even when backlog is overwhelming
❌ Many LLM calls (one per issue/PR)
```

### PROPOSED: Intelligent Orchestration

```
┌──────────────────────────────────────────────────────────────────┐
│                      Orchestrator Agent                          │
│                                                                  │
│  1. ANALYZE (Fast, No LLM)                                      │
│     ┌─────────────────────────────────────────────────┐         │
│     │ RepoStateAnalyzer                               │         │
│     │ • 5 open issues (3 unprocessed)                 │         │
│     │ • 8 PRs: 2 ready-to-merge, 3 pending-review     │         │
│     │ • 4 items are stale (>7 days)                   │         │
│     └─────────────────────────────────────────────────┘         │
│                                                                  │
│     ┌─────────────────────────────────────────────────┐         │
│     │ ResourceMonitor                                 │         │
│     │ • GitHub API: 4500/5000 calls remaining         │         │
│     │ • Can process ~20 items this run                │         │
│     └─────────────────────────────────────────────────┘         │
│                                                                  │
│     ┌─────────────────────────────────────────────────┐         │
│     │ WorkloadPrioritizer                             │         │
│     │ • Quick wins: PRs #42, #38 (ready to merge)     │         │
│     │ • Urgent: Issue #15 (30 days old)               │         │
│     │ • Normal: 3 new issues, 3 PRs pending review    │         │
│     └─────────────────────────────────────────────────┘         │
│                                                                  │
│  2. PLAN (One LLM call)                                         │
│     ┌─────────────────────────────────────────────────┐         │
│     │ LLM Decision                               ← LLM │         │
│     │                                                   │         │
│     │ Strategy: Quick wins first, then clear review    │         │
│     │ backlog. Skip issue creation - backlog healthy.  │         │
│     │                                                   │         │
│     │ Plan:                                             │         │
│     │  1. merge_ready_prs (batch=2) - PR#42, #38       │         │
│     │  2. review_prs (batch=3) - Top 3 by priority     │         │
│     │  3. process_issues (batch=3) - Unprocessed only  │         │
│     │  SKIP: create_issues (not needed)                │         │
│     └─────────────────────────────────────────────────┘         │
│                                                                  │
│  3. EXECUTE (Reuse existing logic)                              │
│     ┌─────────────────────────────────────────────────┐         │
│     │ Step 1: Merge PRs #42, #38 (no LLM needed)      │         │
│     │   → 2 PRs merged, backlog reduced                │         │
│     └─────────────────────────────────────────────────┘         │
│                                                                  │
│     ┌─────────────────────────────────────────────────┐         │
│     │ Step 2: Review top 3 PRs                         │         │
│     │   PR #45: PRDeciderAgent.evaluate()   ← LLM 2    │         │
│     │   PR #47: PRDeciderAgent.evaluate()   ← LLM 3    │         │
│     │   PR #50: PRDeciderAgent.evaluate()   ← LLM 4    │         │
│     │   → 2 approved, 1 requested changes              │         │
│     └─────────────────────────────────────────────────┘         │
│                                                                  │
│     ┌─────────────────────────────────────────────────┐         │
│     │ Step 3: Process top 3 issues                     │         │
│     │   Issue #52: DeciderAgent.evaluate()  ← LLM 5    │         │
│     │   Issue #53: DeciderAgent.evaluate()  ← LLM 6    │         │
│     │   Issue #54: DeciderAgent.evaluate()  ← LLM 7    │         │
│     │   → 2 assigned to Copilot, 1 labeled no-copilot  │         │
│     └─────────────────────────────────────────────────┘         │
│                                                                  │
│  4. REPORT                                                      │
│     Merged: 2 PRs ✓ | Reviewed: 3 PRs ✓ | Processed: 3 issues ✓ │
│     Backlog reduced from 13 → 6 items (-54%)                    │
│     API calls used: ~45 of budget 100                            │
│     LLM calls: 7 (vs 13 in old approach)                        │
└──────────────────────────────────────────────────────────────────┘

Benefits:
✓ Smart prioritization (quick wins first)
✓ Resource-aware (API budget respected)
✓ Strategic (skip unnecessary work)
✓ Fewer LLM calls (7 vs 13)
✓ Better outcomes (backlog reduced 54%)
```

## Decision Flow Comparison

### OLD: Blind Processing
```
START
  ├─> Process ALL issues (even if backlog huge)
  ├─> Process ALL PRs (even if rate limited)
  └─> Create new issues (even if overwhelmed)
END
```

### NEW: Smart Orchestration
```
START
  ├─> Analyze: What's the state?
  ├─> Plan: What should we focus on?
  │    ├─ Overwhelmed? → Clean up stale + merge ready
  │    ├─ Healthy? → Full workflow
  │    └─ Rate limited? → Quick wins only
  ├─> Execute: Run planned workflows
  └─> Report: What changed?
END
```

## Example Scenarios

### Scenario 1: Healthy Repository
```
State:
- 5 issues (3 new, 2 assigned to Copilot)
- 3 PRs (1 ready to merge, 2 pending review)
- API quota: 4800/5000
- No stale items

Orchestrator Decision:
✓ merge_ready_prs (1 PR)     - Quick win
✓ review_prs (2 PRs)         - Clear review queue
✓ process_issues (3 issues)  - Handle new issues
✓ create_issues (3 new)      - Backlog healthy, can add more

Outcome: Cleared existing backlog + created new work
```

### Scenario 2: Overwhelmed Repository
```
State:
- 50 issues (40 unprocessed, 10 stale)
- 20 PRs (5 ready to merge, 10 pending, 5 stale)
- API quota: 500/5000 (low!)
- High staleness

Orchestrator Decision:
✓ merge_ready_prs (5 PRs)      - Quick wins, reduce backlog
✓ cleanup_stale (10 items)     - Remove clutter
✗ SKIP review_prs              - Not enough quota
✗ SKIP process_issues          - Backlog too high
✗ SKIP create_issues           - Already overwhelmed

Outcome: Reduced backlog 15 items without adding more
```

### Scenario 3: Rate Limited
```
State:
- 10 issues (8 new)
- 5 PRs (3 ready to merge, 2 pending)
- API quota: 100/5000 (very low!)
- Rate limit resets in 45 minutes

Orchestrator Decision:
✓ merge_ready_prs (3 PRs)      - Highest value, low cost
✗ SKIP everything else         - Save quota for next run

Outcome: 3 PRs merged, minimal API usage
```

### Scenario 4: PR-Heavy Load
```
State:
- 3 issues (all assigned to Copilot already)
- 15 PRs (10 pending review, 3 ready to merge, 2 blocked)
- API quota: 4000/5000
- PRs are backing up!

Orchestrator Decision:
✓ merge_ready_prs (3 PRs)        - Quick wins
✓ review_prs (10 PRs)            - Major focus
✗ SKIP process_issues            - Already assigned
✗ SKIP create_issues             - PR backlog needs attention

Outcome: 13 PRs handled, cleared review queue
```

## Key Design Principles

### 1. Separation of Concerns
```
Analytical Agents → Data gathering (no decisions)
Orchestrator      → Strategy (LLM decides what/when)
Action Agents     → Execution (no intelligence)
```

### 2. Resource Awareness
```
Before: Process until rate limited (bad)
After:  Check limits → Budget → Plan accordingly (good)
```

### 3. Priority-Based Execution
```
Before: FIFO (first issue processed first)
After:  Urgency-based (stale items + quick wins first)
```

### 4. Adaptive Behavior
```
Before: Same workflow every time
After:  Adapts to repository state
```

### 5. Efficiency Through Intelligence
```
Before: Many small LLM calls (one per item)
After:  One strategic LLM call + targeted execution
```

## Integration Points

### Existing Code (Keep)
- All `_collect_pr_metadata()` logic
- All `_classify_pr_state()` logic  
- All state machine handlers (`_handle_*_state`)
- DeciderAgent, PRDeciderAgent, CreatorAgent
- All GitHub API operations

### New Code (Add)
- `agents/orchestrator.py` - Strategic planning
- `agents/analytical/` - State analysis
- `core/models.py` - Data structures
- `jedimaster.orchestrated_run()` - New entry point

### Modified Code (Minimal)
- `jedimaster.py` - Add `orchestrated_run()` method
- `example.py` - Add `--orchestrate` flag
- `function_app.py` - Add `ENABLE_ORCHESTRATION` env var

## Success Criteria

The orchestrator is successful if:

1. **Reduces API Waste**: Fewer API calls per item processed
2. **Smarter Prioritization**: Stale items handled faster
3. **Better Resource Management**: Never hits rate limits
4. **Improved Outcomes**: Faster backlog reduction
5. **Maintainable**: Clear separation of concerns
6. **Observable**: Detailed logging and reporting

## Migration Path

```
Phase 1: Build (Weeks 1-2)
├─ Create analytical agents
├─ Create orchestrator agent
└─ Add orchestrated_run() method

Phase 2: Test (Week 3)
├─ Manual testing with --orchestrate
├─ Compare outcomes vs. old approach
└─ Tune LLM prompts

Phase 3: Opt-In (Week 4-5)
├─ Deploy to Azure Functions
├─ Enable for 1-2 test repos
└─ Monitor and validate

Phase 4: Default (Week 6+)
├─ Make orchestration default
├─ Deprecate old flow
└─ Remove legacy code
```

## Questions for Review

1. **Architecture**: Does the three-tier design make sense?
2. **Workflows**: Are merge/review/process/create/cleanup the right granularity?
3. **Migration**: Should orchestration be opt-in forever, or eventually replace old flow?
4. **State Logic**: Confirm we're keeping ALL existing state detection logic unchanged?
5. **Rate Limits**: Should we also consider Copilot's rate limits (how many concurrent reviews)?

Please review and provide feedback before implementation begins.
