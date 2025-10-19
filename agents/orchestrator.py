"""Main orchestrator agent with LLM-based strategic planning."""
import json
import logging
import os
from typing import Optional
from datetime import datetime
from agent_framework.azure import AzureAIAgentClient
from agent_framework import ChatAgent
from azure.identity.aio import DefaultAzureCredential
from github import Github

from core.models import (
    RepoState,
    ResourceState,
    PrioritizedWorkload,
    ExecutionPlan,
    WorkflowStep,
    OrchestrationReport,
    WorkflowResult
)
from agents.analytical import RepoStateAnalyzer, ResourceMonitor, WorkloadPrioritizer


class OrchestratorAgent:
    """LLM-based strategic planner for repository automation."""
    
    def __init__(self, github: Github, azure_foundry_endpoint: str, model: str = None):
        self.github = github
        self.azure_foundry_endpoint = azure_foundry_endpoint
        self.model = model or os.getenv('AZURE_AI_MODEL', 'model-router')
        self.logger = logging.getLogger('jedimaster.orchestrator')
        self._credential: Optional[DefaultAzureCredential] = None
        
        # Initialize analytical agents
        self.state_analyzer = RepoStateAnalyzer(github)
        self.resource_monitor = ResourceMonitor(github)
        self.workload_prioritizer = WorkloadPrioritizer(github)
        
        self.system_prompt = """You are a strategic orchestrator managing GitHub repository automation.
Your goal is to reduce the number of open issues and PRs efficiently while respecting constraints.

IMPORTANT PRINCIPLES:
1. Copilot is always working on assigned issues - trust it's making progress
2. The ONLY stuck state is when a PR exceeded MERGE_MAX_RETRIES
3. Quick wins first: ALWAYS merge ready PRs before anything else
4. Respect Copilot capacity: Don't overwhelm it
5. Conserve API quota: Prioritize high-value, low-cost work
6. Clear backlogs before creating new issues

You will receive:
- Repository state (issue counts, PR counts by state)
- Resource constraints (GitHub API quota, Copilot capacity)
- Prioritized workload (which items need attention)

Available workflows:
- merge_ready_prs: Merge approved PRs (no LLM, fast, highest priority)
- review_prs: Review PRs needing evaluation (uses PRDeciderAgent LLM)
- process_issues: Evaluate and assign issues (uses DeciderAgent LLM)
- create_issues: Generate new issues (uses CreatorAgent LLM)
- flag_blocked_prs: Mark PRs that exceeded retry limit (no LLM)

STRATEGIC RULES:
1. If Copilot at capacity → focus on clearing PRs, skip issue assignments
2. If API quota low (<10% remaining) → only merge ready PRs, skip everything else
3. If backlog >20 items → skip issue creation
4. If blocked PRs exist → flag them for humans
5. Adapt batch sizes to available resources

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
        """Create an execution plan using LLM strategic reasoning."""
        
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
                
                self.logger.info(f"Orchestrator LLM response: {plan_json[:500]}...")
                
                # Clean up response (remove markdown code blocks if present)
                cleaned = plan_json.strip()
                if cleaned.startswith('```json'):
                    cleaned = cleaned[7:]
                if cleaned.startswith('```'):
                    cleaned = cleaned[3:]
                if cleaned.endswith('```'):
                    cleaned = cleaned[:-3]
                cleaned = cleaned.strip()
                
                # Parse response
                plan_data = json.loads(cleaned)
                
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
                    warnings=plan_data.get('warnings', [])
                )
                
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse orchestrator response: {e}")
            self.logger.error(f"Raw response: {plan_json if 'plan_json' in locals() else 'N/A'}")
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
        
        return f"""Analyze this repository and create an execution plan:

REPOSITORY STATE:
- Repository: {repo_state.repo}
- Total backlog: {total_backlog} items ({repo_state.open_issues_total} issues, {repo_state.open_prs_total} PRs)
- Unprocessed issues: {repo_state.open_issues_unprocessed}
- Issues assigned to Copilot: {repo_state.copilot_active_issues}

PRs by state:
- Ready to merge: {repo_state.prs_ready_to_merge} ← QUICK WINS!
- Pending review: {repo_state.prs_pending_review}
- Changes requested: {repo_state.prs_changes_requested}
- Blocked (merge retries exceeded): {repo_state.prs_blocked} ← Need human help
- Done: {repo_state.prs_done}

Copilot active work: {repo_state.copilot_active_issues} issues, {repo_state.copilot_active_prs} PRs

RESOURCE CONSTRAINTS:
- GitHub API: {resource_state.github_api_remaining}/{resource_state.github_api_limit} calls available
- Estimated budget: Can safely process ~{resource_state.estimated_api_budget} items
- Copilot Capacity: {resource_state.copilot_assigned_issues}/{resource_state.copilot_max_concurrent} issues assigned
- Copilot Available slots: {resource_state.copilot_available_slots}
- Warnings: {', '.join(resource_state.warnings) if resource_state.warnings else 'None'}

PRIORITIZED WORKLOAD:
- Quick wins available: {len(workload.quick_wins)} PRs ready to merge
- Blocked PRs needing attention: {len(workload.blocked_prs)}
- PRs pending review: {len(workload.pending_review_prs)}
- PRs with changes requested: {len(workload.changes_requested_prs)}
- Unprocessed issues: {len(workload.unprocessed_issues)}

Create an optimal execution plan. Remember:
1. ALWAYS merge ready PRs first (instant wins, no LLM cost, frees Copilot)
2. If Copilot at capacity ({resource_state.copilot_available_slots} slots) → focus on clearing PRs
3. If API quota low → prioritize high-value, low-cost work
4. If backlog >{20} items → skip issue creation
5. If blocked PRs exist → flag them for humans
6. Respect API budget: don't plan more work than we can handle

Return your plan as JSON."""
    
    def _create_fallback_plan(self, repo_state: RepoState, resource_state: ResourceState) -> ExecutionPlan:
        """Create a safe fallback plan if LLM fails."""
        workflows = []
        
        # Always try to merge ready PRs
        if repo_state.prs_ready_to_merge > 0:
            workflows.append(WorkflowStep(
                name='merge_ready_prs',
                batch_size=min(repo_state.prs_ready_to_merge, 5),
                reasoning='Fallback: merge available quick wins'
            ))
        
        # Only do more if we have budget
        if resource_state.estimated_api_budget > 10:
            # Flag blocked PRs
            if repo_state.prs_blocked > 0:
                workflows.append(WorkflowStep(
                    name='flag_blocked_prs',
                    batch_size=repo_state.prs_blocked,
                    reasoning='Fallback: alert humans to blocked PRs'
                ))
            
            # Review a few PRs if Copilot not maxed
            if repo_state.prs_pending_review > 0 and resource_state.copilot_available_slots > 0:
                workflows.append(WorkflowStep(
                    name='review_prs',
                    batch_size=min(repo_state.prs_pending_review, 3),
                    reasoning='Fallback: review small batch of PRs'
                ))
        
        return ExecutionPlan(
            strategy="Fallback plan: LLM unavailable, using conservative defaults",
            workflows=workflows,
            skip_workflows=['process_issues', 'create_issues'],
            estimated_api_calls=25,
            warnings=["Using fallback plan due to orchestrator failure"]
        )
    
    def calculate_health_score(self, state: RepoState) -> float:
        """Calculate repository health score (0.0 = poor, 1.0 = excellent)."""
        total_items = state.open_issues_total + state.open_prs_total
        
        if total_items == 0:
            return 1.0  # Perfect health
        
        # Penalties
        backlog_penalty = min(total_items / 50.0, 1.0)  # >50 items = max penalty
        blocked_penalty = state.prs_blocked / max(state.open_prs_total, 1)
        
        # Calculate score (inverse of penalties)
        score = 1.0 - ((backlog_penalty + blocked_penalty) / 2.0)
        return max(0.0, min(1.0, score))
