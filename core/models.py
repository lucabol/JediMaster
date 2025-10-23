"""Core data models for the orchestrator architecture."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any


@dataclass
class RepoState:
    """Snapshot of current repository state."""
    repo: str
    timestamp: datetime
    
    # Issues
    open_issues_total: int
    open_issues_unprocessed: int  # No copilot labels yet
    
    # PRs by state (from copilot-state labels)
    open_prs_total: int
    prs_pending_review: int
    prs_changes_requested: int
    prs_ready_to_merge: int
    prs_blocked: int  # Exceeded MERGE_MAX_RETRIES
    prs_done: int
    
    # Quick stats
    copilot_active_prs: int     # PRs Copilot is working on
    quick_wins_available: int   # PRs ready to merge (immediate wins)
    truly_blocked_prs: int      # PRs that exceeded retry limit


@dataclass
class ResourceState:
    """Available resources and capacity constraints."""
    # GitHub API
    github_api_remaining: int
    github_api_limit: int
    github_api_reset_at: datetime
    estimated_api_budget: int  # How many items we can safely process
    
    # Copilot Capacity (based on active PRs only)
    copilot_max_concurrent: int       # Capacity limit (configurable)
    copilot_available_slots: int      # Can work on N more PRs
    copilot_active_prs: int           # PRs in flight
    
    # Warnings
    warnings: List[str] = field(default_factory=list)


@dataclass
class PrioritizedWorkload:
    """Work items sorted by priority."""
    quick_wins: List[int]              # PR numbers ready to merge (do first!)
    blocked_prs: List[int]             # PR numbers exceeded retry limit (flag for human)
    pending_review_prs: List[int]      # PR numbers needing review
    changes_requested_prs: List[int]   # PR numbers Copilot updating
    unprocessed_issues: List[int]      # Issue numbers needing evaluation


@dataclass
class WorkflowStep:
    """A single workflow execution step."""
    name: str  # merge_ready_prs, review_prs, process_issues, create_issues, flag_blocked_prs
    batch_size: int
    reasoning: str = ""


@dataclass
class ExecutionPlan:
    """LLM-generated execution plan."""
    strategy: str  # High-level strategy explanation
    workflows: List[WorkflowStep]
    skip_workflows: List[str]  # Workflows to skip this run
    estimated_api_calls: int
    warnings: List[str] = field(default_factory=list)


@dataclass
class WorkflowResult:
    """Result of executing a single workflow."""
    workflow_name: str
    success: bool
    items_processed: int
    items_succeeded: int
    items_failed: int
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
    backlog_reduction: int  # Items removed
    health_score_before: float  # 0.0 - 1.0
    health_score_after: float  # 0.0 - 1.0
