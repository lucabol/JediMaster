# Orchestrator Implementation Examples

This document shows concrete code examples for the orchestrator refactoring.

## 1. Data Models (core/models.py)

```python
"""Data models for orchestrator and analytical agents."""
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Optional, Any

@dataclass
class RepoState:
    """Snapshot of repository state."""
    repo: str
    timestamp: datetime
    
    # Issues
    open_issues_total: int
    open_issues_unprocessed: int  # No copilot-candidate or no-github-copilot label
    open_issues_copilot_assigned: int
    open_issues_stale: int  # >7 days old
    
    # PRs
    open_prs_total: int
    open_prs_pending_review: int
    open_prs_changes_requested: int
    open_prs_ready_to_merge: int
    open_prs_blocked: int
    open_prs_stale: int  # >7 days old
    
    # Quick stats
    copilot_active_work: int  # Issues + PRs Copilot is working on
    quick_wins_available: int  # PRs ready to merge

@dataclass
class ResourceState:
    """Available resources and constraints."""
    github_api_remaining: int
    github_api_limit: int
    github_api_reset_at: datetime
    estimated_api_budget: int  # How many items we can safely process
    warnings: List[str] = field(default_factory=list)

@dataclass
class PrioritizedItem:
    """A single prioritized work item."""
    item_type: str  # "issue" or "pr"
    number: int
    title: str
    priority_score: float  # 0.0 - 1.0
    age_days: int
    state: str
    reason: str  # Why this priority

@dataclass
class PrioritizedWorkload:
    """Sorted work items by priority."""
    quick_wins: List[PrioritizedItem]  # PRs ready to merge
    high_priority: List[PrioritizedItem]  # Urgent items
    normal_priority: List[PrioritizedItem]  # Regular items
    low_priority: List[PrioritizedItem]  # Can defer

@dataclass
class WorkflowStep:
    """A single workflow execution step."""
    name: str  # merge_ready_prs, review_prs, process_issues, create_issues, cleanup_stale
    batch_size: int
    target_items: List[int] = field(default_factory=list)  # Specific issue/PR numbers
    reasoning: str = ""

@dataclass
class ExecutionPlan:
    """LLM-generated execution plan."""
    strategy: str  # High-level strategy explanation
    workflows: List[WorkflowStep]
    skip_workflows: List[str]  # Workflows to skip this run
    estimated_api_calls: int
    estimated_duration_seconds: int
    warnings: List[str] = field(default_factory=list)

@dataclass
class WorkflowResult:
    """Result of executing a single workflow."""
    workflow_name: str
    success: bool
    items_processed: int
    items_succeeded: int
    items_failed: int
    api_calls_used: int
    duration_seconds: float
    details: List[Any] = field(default_factory=list)
    error: Optional[str] = None

@dataclass
class OrchestrationReport:
    """Complete orchestration run report."""
    repo: str
    timestamp: datetime
    
    # Initial state
    initial_state: RepoState
    initial_resources: ResourceState
    prioritized_workload: PrioritizedWorkload
    
    # Plan
    execution_plan: ExecutionPlan
    
    # Results
    workflow_results: List[WorkflowResult]
    
    # Final state  
    final_state: RepoState
    final_resources: ResourceState
    
    # Metrics
    total_duration_seconds: float
    total_api_calls: int
    total_llm_calls: int
    backlog_reduction: int  # Items removed
    health_score_before: float  # 0.0 - 1.0
    health_score_after: float  # 0.0 - 1.0
```

## 2. Analytical Agent: RepoStateAnalyzer

```python
"""Analyzes repository state without making decisions."""
import logging
from datetime import datetime, timedelta, timezone
from github import Github
from core.models import RepoState

class RepoStateAnalyzer:
    """Analyzes current repository state."""
    
    def __init__(self, github: Github):
        self.github = github
        self.logger = logging.getLogger('jedimaster.analyzer')
    
    def analyze(self, repo_name: str) -> RepoState:
        """Analyze repository and return current state."""
        repo = self.github.get_repo(repo_name)
        now = datetime.now(timezone.utc)
        stale_threshold = now - timedelta(days=7)
        
        # Analyze issues
        all_issues = list(repo.get_issues(state='open'))
        issues = [i for i in all_issues if not i.pull_request]
        
        unprocessed_issues = []
        copilot_assigned_issues = []
        stale_issues = []
        
        for issue in issues:
            labels = {label.name.lower() for label in issue.labels}
            
            # Unprocessed: no copilot labels
            if not labels.intersection({'copilot-candidate', 'no-github-copilot'}):
                unprocessed_issues.append(issue)
            
            # Copilot assigned
            if any('copilot' in (a.login or '').lower() for a in issue.assignees):
                copilot_assigned_issues.append(issue)
            
            # Stale
            if issue.updated_at < stale_threshold:
                stale_issues.append(issue)
        
        # Analyze PRs
        all_prs = list(repo.get_pulls(state='open'))
        
        pr_states = {
            'pending_review': [],
            'changes_requested': [],
            'ready_to_merge': [],
            'blocked': []
        }
        stale_prs = []
        
        for pr in all_prs:
            # Get PR state label
            state = self._get_pr_state_label(pr)
            if state in pr_states:
                pr_states[state].append(pr)
            
            # Stale PRs
            if pr.updated_at < stale_threshold:
                stale_prs.append(pr)
        
        # Calculate derived metrics
        copilot_active = len(copilot_assigned_issues) + sum(len(prs) for prs in pr_states.values())
        quick_wins = len(pr_states['ready_to_merge'])
        
        return RepoState(
            repo=repo_name,
            timestamp=now,
            open_issues_total=len(issues),
            open_issues_unprocessed=len(unprocessed_issues),
            open_issues_copilot_assigned=len(copilot_assigned_issues),
            open_issues_stale=len(stale_issues),
            open_prs_total=len(all_prs),
            open_prs_pending_review=len(pr_states['pending_review']),
            open_prs_changes_requested=len(pr_states['changes_requested']),
            open_prs_ready_to_merge=len(pr_states['ready_to_merge']),
            open_prs_blocked=len(pr_states['blocked']),
            open_prs_stale=len(stale_prs),
            copilot_active_work=copilot_active,
            quick_wins_available=quick_wins
        )
    
    def _get_pr_state_label(self, pr) -> str:
        """Extract copilot-state label from PR."""
        COPILOT_STATE_LABEL_PREFIX = "copilot-state:"
        try:
            for label in pr.labels:
                if label.name.startswith(COPILOT_STATE_LABEL_PREFIX):
                    return label.name[len(COPILOT_STATE_LABEL_PREFIX):]
        except Exception as e:
            self.logger.debug(f"Failed to get state label for PR #{pr.number}: {e}")
        return 'unknown'
```

## 3. Analytical Agent: ResourceMonitor

```python
"""Monitors API quotas and system resources."""
import logging
from datetime import datetime, timezone
from github import Github
from core.models import ResourceState

class ResourceMonitor:
    """Monitors available resources."""
    
    def __init__(self, github: Github):
        self.github = github
        self.logger = logging.getLogger('jedimaster.resources')
    
    def check_resources(self) -> ResourceState:
        """Check available resources and return state."""
        warnings = []
        
        try:
            rate_limit = self.github.get_rate_limit()
            
            # Handle both rate limit structures
            if hasattr(rate_limit, 'core'):
                remaining = rate_limit.core.remaining
                limit = rate_limit.core.limit
                reset_time = rate_limit.core.reset
            else:
                remaining = getattr(rate_limit, 'remaining', 5000)
                limit = getattr(rate_limit, 'limit', 5000)
                reset_time = getattr(rate_limit, 'reset', datetime.now(timezone.utc))
            
            # Calculate safe budget (use only 80% of remaining to leave buffer)
            safe_budget = int(remaining * 0.8)
            
            # Each item typically uses ~5 API calls
            estimated_items = safe_budget // 5
            
            # Add warnings
            if remaining < limit * 0.1:
                warnings.append(f"Low API quota: {remaining}/{limit} remaining")
            
            if remaining < 100:
                warnings.append("CRITICAL: Very low API quota, recommend deferring work")
            
            return ResourceState(
                github_api_remaining=remaining,
                github_api_limit=limit,
                github_api_reset_at=reset_time,
                estimated_api_budget=estimated_items,
                warnings=warnings
            )
            
        except Exception as e:
            self.logger.error(f"Failed to check resources: {e}")
            # Return conservative estimate on error
            return ResourceState(
                github_api_remaining=500,
                github_api_limit=5000,
                github_api_reset_at=datetime.now(timezone.utc),
                estimated_api_budget=10,  # Very conservative
                warnings=[f"Failed to check rate limit: {e}"]
            )
```

## 4. Orchestrator Agent

```python
"""Main orchestrator agent with LLM-based planning."""
import json
import logging
from typing import Optional
from agent_framework.azure import AzureAIAgentClient
from agent_framework import ChatAgent
from azure.identity.aio import DefaultAzureCredential
from core.models import RepoState, ResourceState, PrioritizedWorkload, ExecutionPlan, WorkflowStep

class OrchestratorAgent:
    """LLM-based strategic planner for repository automation."""
    
    def __init__(self, azure_foundry_endpoint: str, model: str = None):
        self.azure_foundry_endpoint = azure_foundry_endpoint
        self.model = model or os.getenv('AZURE_AI_MODEL', 'model-router')
        self.logger = logging.getLogger('jedimaster.orchestrator')
        self._credential: Optional[DefaultAzureCredential] = None
        
        self.system_prompt = """You are an orchestrator managing GitHub repository automation.
Your goal is to reduce the number of open issues and PRs efficiently while respecting rate limits.

You will receive:
1. Current repository state (issue counts, PR counts, staleness)
2. Resource availability (API quotas, estimated budget)
3. Prioritized workload (sorted by urgency)

You must return a JSON execution plan with these workflows:
- merge_ready_prs: Merge already-approved PRs (no LLM needed, fast)
- review_prs: Review PRs that need Copilot evaluation (uses LLM)
- process_issues: Evaluate unprocessed issues for Copilot (uses LLM)
- create_issues: Generate new issues (uses LLM)
- cleanup_stale: Close stale issues/PRs

Strategy guidelines:
1. ALWAYS merge ready PRs first (quick wins, no LLM cost)
2. Focus on clearing backlogs before creating new work
3. Respect API budget (don't plan more work than we can handle)
4. Prioritize stale cleanup if >50% of items are stale
5. Skip issue creation if backlog is >20 items
6. Use batch sizes based on urgency and budget

Return ONLY valid JSON matching this schema:
{
  "strategy": "Brief explanation of approach",
  "workflows": [
    {
      "name": "workflow_name",
      "batch_size": number,
      "reasoning": "why this workflow and batch size"
    }
  ],
  "skip_workflows": ["workflow_name"],
  "estimated_api_calls": number,
  "estimated_duration_seconds": number,
  "warnings": ["any concerns"]
}
"""
    
    async def __aenter__(self):
        """Async context manager entry."""
        self._credential = DefaultAzureCredential()
        await self._credential.__aenter__()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self._credential:
            await self._credential.__aexit__(exc_type, exc_val, exc_tb)
    
    async def create_execution_plan(
        self,
        repo_state: RepoState,
        resource_state: ResourceState,
        prioritized_workload: PrioritizedWorkload
    ) -> ExecutionPlan:
        """Create an execution plan using LLM."""
        
        # Format state for LLM
        prompt = self._format_planning_prompt(repo_state, resource_state, prioritized_workload)
        
        try:
            # Call LLM
            async with ChatAgent(
                chat_client=AzureAIAgentClient(async_credential=self._credential),
                instructions=self.system_prompt,
                model=self.model
            ) as agent:
                result = await agent.run(prompt)
                plan_json = result.text
                
                # Parse response
                plan_data = json.loads(plan_json)
                
                # Validate and construct ExecutionPlan
                workflows = [
                    WorkflowStep(
                        name=w['name'],
                        batch_size=w['batch_size'],
                        reasoning=w.get('reasoning', '')
                    )
                    for w in plan_data.get('workflows', [])
                ]
                
                return ExecutionPlan(
                    strategy=plan_data.get('strategy', ''),
                    workflows=workflows,
                    skip_workflows=plan_data.get('skip_workflows', []),
                    estimated_api_calls=plan_data.get('estimated_api_calls', 0),
                    estimated_duration_seconds=plan_data.get('estimated_duration_seconds', 0),
                    warnings=plan_data.get('warnings', [])
                )
                
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse orchestrator response: {e}")
            # Fallback to safe default plan
            return self._create_fallback_plan(repo_state, resource_state)
        except Exception as e:
            self.logger.error(f"Orchestrator planning failed: {e}")
            return self._create_fallback_plan(repo_state, resource_state)
    
    def _format_planning_prompt(
        self,
        repo_state: RepoState,
        resource_state: ResourceState,
        workload: PrioritizedWorkload
    ) -> str:
        """Format repository state into LLM prompt."""
        
        # Calculate derived metrics
        total_backlog = repo_state.open_issues_total + repo_state.open_prs_total
        stale_percentage = ((repo_state.open_issues_stale + repo_state.open_prs_stale) / max(total_backlog, 1)) * 100
        
        return f"""Analyze this repository and create an execution plan:

REPOSITORY STATE:
- Total backlog: {total_backlog} items ({repo_state.open_issues_total} issues, {repo_state.open_prs_total} PRs)
- Unprocessed issues: {repo_state.open_issues_unprocessed}
- PRs by state:
  * Ready to merge: {repo_state.open_prs_ready_to_merge} (QUICK WINS!)
  * Pending review: {repo_state.open_prs_pending_review}
  * Changes requested: {repo_state.open_prs_changes_requested}
  * Blocked: {repo_state.open_prs_blocked}
- Stale items: {repo_state.open_issues_stale + repo_state.open_prs_stale} ({stale_percentage:.1f}% of backlog)
- Copilot active work: {repo_state.copilot_active_work} items

RESOURCE AVAILABILITY:
- GitHub API: {resource_state.github_api_remaining}/{resource_state.github_api_limit} calls remaining
- Estimated budget: Can safely process ~{resource_state.estimated_api_budget} items
- Warnings: {', '.join(resource_state.warnings) if resource_state.warnings else 'None'}

PRIORITIZED WORKLOAD:
- Quick wins available: {len(workload.quick_wins)} PRs ready to merge
- High priority items: {len(workload.high_priority)}
- Normal priority items: {len(workload.normal_priority)}
- Low priority items: {len(workload.low_priority)}

Create an optimal execution plan. Remember:
1. Merge ready PRs first (no LLM cost, immediate backlog reduction)
2. Don't overwhelm - if backlog >20 items, skip issue creation
3. Clean up stale items if >50% are stale
4. Respect API budget constraint
"""
    
    def _create_fallback_plan(self, repo_state: RepoState, resource_state: ResourceState) -> ExecutionPlan:
        """Create a safe fallback plan if LLM fails."""
        workflows = []
        
        # Always try to merge ready PRs
        if repo_state.open_prs_ready_to_merge > 0:
            workflows.append(WorkflowStep(
                name='merge_ready_prs',
                batch_size=min(repo_state.open_prs_ready_to_merge, 5),
                reasoning='Fallback: merge available quick wins'
            ))
        
        # Only do more if we have budget
        if resource_state.estimated_api_budget > 10:
            # Review a few PRs
            if repo_state.open_prs_pending_review > 0:
                workflows.append(WorkflowStep(
                    name='review_prs',
                    batch_size=min(repo_state.open_prs_pending_review, 3),
                    reasoning='Fallback: review small batch of PRs'
                ))
        
        return ExecutionPlan(
            strategy="Fallback plan: LLM unavailable, using conservative defaults",
            workflows=workflows,
            skip_workflows=['process_issues', 'create_issues', 'cleanup_stale'],
            estimated_api_calls=25,
            estimated_duration_seconds=60,
            warnings=["Using fallback plan due to orchestrator failure"]
        )
```

## 5. Integration in JediMaster

```python
# In jedimaster.py

async def orchestrated_run(self, repo_name: str) -> OrchestrationReport:
    """Execute an orchestrated run on a repository."""
    from agents.analytical.repo_state_analyzer import RepoStateAnalyzer
    from agents.analytical.resource_monitor import ResourceMonitor
    from agents.analytical.workload_prioritizer import WorkloadPrioritizer
    from agents.orchestrator import OrchestratorAgent
    from datetime import datetime
    
    start_time = datetime.now()
    self.logger.info(f"[Orchestrator] Starting orchestrated run for {repo_name}")
    
    # Step 1: Analyze (Fast, no LLM)
    self.logger.info("[Orchestrator] Step 1: Analyzing repository state...")
    analyzer = RepoStateAnalyzer(self.github)
    initial_state = analyzer.analyze(repo_name)
    
    self.logger.info("[Orchestrator] Step 2: Checking resources...")
    monitor = ResourceMonitor(self.github)
    initial_resources = monitor.check_resources()
    
    self.logger.info("[Orchestrator] Step 3: Prioritizing workload...")
    prioritizer = WorkloadPrioritizer(self.github)
    workload = prioritizer.prioritize(repo_name, initial_state)
    
    # Step 2: Plan (One LLM call)
    self.logger.info("[Orchestrator] Step 4: Creating execution plan...")
    async with OrchestratorAgent(self.azure_foundry_endpoint, self.model) as orchestrator:
        plan = await orchestrator.create_execution_plan(
            initial_state, initial_resources, workload
        )
    
    self.logger.info(f"[Orchestrator] Plan created: {plan.strategy}")
    for workflow in plan.workflows:
        self.logger.info(f"  - {workflow.name} (batch={workflow.batch_size})")
    
    # Step 3: Execute workflows
    self.logger.info("[Orchestrator] Step 5: Executing workflows...")
    workflow_results = []
    total_llm_calls = 1  # One for orchestration
    
    for workflow in plan.workflows:
        result = await self._execute_workflow(repo_name, workflow)
        workflow_results.append(result)
        total_llm_calls += result.items_processed  # Estimate
    
    # Step 4: Final analysis
    self.logger.info("[Orchestrator] Step 6: Collecting final metrics...")
    final_state = analyzer.analyze(repo_name)
    final_resources = monitor.check_resources()
    
    # Calculate metrics
    duration = (datetime.now() - start_time).total_seconds()
    api_calls_used = initial_resources.github_api_remaining - final_resources.github_api_remaining
    backlog_reduction = (initial_state.open_issues_total + initial_state.open_prs_total) - \
                       (final_state.open_issues_total + final_state.open_prs_total)
    
    # Calculate health scores
    health_before = self._calculate_health_score(initial_state)
    health_after = self._calculate_health_score(final_state)
    
    return OrchestrationReport(
        repo=repo_name,
        timestamp=datetime.now(),
        initial_state=initial_state,
        initial_resources=initial_resources,
        prioritized_workload=workload,
        execution_plan=plan,
        workflow_results=workflow_results,
        final_state=final_state,
        final_resources=final_resources,
        total_duration_seconds=duration,
        total_api_calls=api_calls_used,
        total_llm_calls=total_llm_calls,
        backlog_reduction=backlog_reduction,
        health_score_before=health_before,
        health_score_after=health_after
    )

def _calculate_health_score(self, state: RepoState) -> float:
    """Calculate repository health score (0.0 = poor, 1.0 = excellent)."""
    total_items = state.open_issues_total + state.open_prs_total
    
    if total_items == 0:
        return 1.0  # Perfect health
    
    # Penalties
    stale_penalty = (state.open_issues_stale + state.open_prs_stale) / total_items
    backlog_penalty = min(total_items / 50.0, 1.0)  # >50 items = max penalty
    blocked_penalty = state.open_prs_blocked / max(state.open_prs_total, 1)
    
    # Calculate score (inverse of penalties)
    score = 1.0 - ((stale_penalty + backlog_penalty + blocked_penalty) / 3.0)
    return max(0.0, min(1.0, score))
```

## 6. CLI Integration

```python
# In example.py

parser.add_argument('--orchestrate', action='store_true',
                   help='Use intelligent orchestration (LLM-based planning)')

# In main():
if args.orchestrate:
    print("Running orchestrated workflow...")
    async with JediMaster(...) as jedi:
        report = await jedi.orchestrated_run(repo_name)
        jedi.print_orchestration_report(report)
else:
    print("Running legacy workflow...")
    # Existing logic
```

This shows the complete implementation structure with real code examples!
