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
            
            # Count PRs where Copilot is actively working (determines capacity)
            # A PR is considered "active" if:
            # 1. Copilot is the author (PR was created for an issue)
            # 2. A review comment was recently added (Copilot is addressing feedback)
            # 3. A merge conflict comment was added (Copilot is fixing conflicts)
            copilot_prs = 0
            
            for pr in repo.get_pulls(state='open'):
                author = pr.user.login if pr.user else ''
                if 'copilot' in author.lower():
                    # Check if Copilot is still working on this PR
                    if self._is_copilot_actively_working(pr):
                        copilot_prs += 1
            
            # Capacity is based on active PRs only
            # Assigning issues to Copilot just creates PRs which then consume capacity
            available_slots = max(0, self.copilot_max_concurrent - copilot_prs)
            
            # Add capacity warnings based on PRs
            if copilot_prs >= self.copilot_max_concurrent:
                warnings.append(
                    f"Copilot at capacity: {copilot_prs}/{self.copilot_max_concurrent} active PRs"
                )
            
            if copilot_prs > 5:
                warnings.append(
                    f"PR review backlog: {copilot_prs} PRs need attention"
                )
            
        except Exception as e:
            self.logger.error(f"Failed to check Copilot capacity: {e}")
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
            copilot_max_concurrent=self.copilot_max_concurrent,
            copilot_available_slots=available_slots,
            copilot_active_prs=copilot_prs,
            warnings=warnings
        )
    
    def _is_copilot_actively_working(self, pr) -> bool:
        """Check if Copilot is actively working on a PR using timeline events.
        
        Copilot is considered actively working if timeline shows:
        - "Copilot started work" without a corresponding "finished" or "stopped"
        
        Falls back to draft status if timeline unavailable.
        
        Returns:
            bool: True if Copilot is actively working, False otherwise
        """
        try:
            # Use timeline events to check for Copilot work status
            timeline = pr.as_issue().get_timeline()
            
            copilot_start = None
            copilot_finish = None
            copilot_stop = None
            
            for event in timeline:
                event_type = getattr(event, 'event', None)
                if event_type != 'commented':
                    continue
                
                body = getattr(event, 'body', '') or ''
                created_at = getattr(event, 'created_at', None)
                body_lower = body.lower()
                
                if 'copilot started work' in body_lower:
                    copilot_start = created_at
                elif 'copilot finished work' in body_lower:
                    copilot_finish = created_at
                elif 'copilot stopped work' in body_lower:
                    copilot_stop = created_at
            
            # If we found start/finish events, use them
            if copilot_start:
                # Check if there's a more recent finish/stop
                if copilot_finish and copilot_finish > copilot_start:
                    return False  # Finished after starting
                if copilot_stop and copilot_stop > copilot_start:
                    return False  # Stopped after starting
                return True  # Started but not finished/stopped
            
            # Fallback: If PR is draft, assume Copilot is working
            return pr.draft
            
        except Exception as e:
            self.logger.warning(f"Failed to check if Copilot is working on PR #{pr.number}: {e}")
            # Default to draft status as fallback
            try:
                return pr.draft
            except:
                # Conservative: assume working to avoid over-assignment
                return True
