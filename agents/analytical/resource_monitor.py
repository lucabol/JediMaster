"""Monitors API quotas and system resources."""
import logging
import os
from datetime import datetime, timezone
from github import Github
from core.models import ResourceState


class ResourceMonitor:
    """Monitors available resources including Copilot capacity."""
    
    def __init__(self, github: Github, copilot_max_concurrent: int = None):
        self.github = github
        self.copilot_max_concurrent = copilot_max_concurrent or int(os.getenv('COPILOT_MAX_CONCURRENT_ISSUES', '10'))
        self.logger = logging.getLogger('jedimaster.resources')
    
    def check_resources(self, repo_name: str) -> ResourceState:
        """Check all available resources including Copilot capacity."""
        warnings = []
        
        # Check GitHub API rate limits
        try:
            rate_limit = self.github.get_rate_limit()
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
        except Exception as e:
            self.logger.error(f"Failed to check GitHub rate limit: {e}")
            remaining, limit, reset_time = 500, 5000, datetime.now(timezone.utc)
            estimated_items = 10
            warnings.append(f"Failed to check rate limit: {e}")
        
        # Check Copilot capacity
        try:
            repo = self.github.get_repo(repo_name)
            
            # Count Copilot's active work
            copilot_issues = 0
            copilot_prs = 0
            
            for issue in repo.get_issues(state='open'):
                if issue.pull_request:
                    continue
                # Check if assigned to Copilot
                if any('copilot' in (a.login or '').lower() for a in issue.assignees):
                    copilot_issues += 1
            
            # Count PRs (approximation of Copilot PRs)
            for pr in repo.get_pulls(state='open'):
                author = pr.user.login if pr.user else ''
                if 'copilot' in author.lower():
                    copilot_prs += 1
            
            available_slots = max(0, self.copilot_max_concurrent - copilot_issues)
            
            # Add capacity warnings
            if copilot_issues >= self.copilot_max_concurrent:
                warnings.append(
                    f"Copilot at capacity: {copilot_issues}/{self.copilot_max_concurrent} issues"
                )
            
            if copilot_prs > 5:
                warnings.append(
                    f"PR review backlog: {copilot_prs} PRs need attention"
                )
            
        except Exception as e:
            self.logger.error(f"Failed to check Copilot capacity: {e}")
            copilot_issues = 0
            copilot_prs = 0
            available_slots = self.copilot_max_concurrent
            warnings.append(f"Failed to check Copilot capacity: {e}")
        
        return ResourceState(
            # GitHub API
            github_api_remaining=remaining,
            github_api_limit=limit,
            github_api_reset_at=reset_time,
            estimated_api_budget=estimated_items,
            # Copilot capacity
            copilot_assigned_issues=copilot_issues,
            copilot_max_concurrent=self.copilot_max_concurrent,
            copilot_available_slots=available_slots,
            copilot_active_prs=copilot_prs,
            warnings=warnings
        )
