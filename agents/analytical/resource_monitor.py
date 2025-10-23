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
        """Check if Copilot is actively working on a PR.
        
        Copilot is considered actively working if:
        1. The PR is in draft state (Copilot hasn't requested review yet)
        2. There was a recent comment mentioning @copilot (review feedback or merge conflict)
        3. The most recent comment is from Copilot (responding to feedback)
        
        Returns:
            bool: True if Copilot is actively working, False otherwise
        """
        try:
            # If PR is draft, Copilot is still working on it
            if pr.draft:
                return True
            
            # Check recent comments to see if Copilot is being asked to work or responding
            comments = list(pr.get_issue_comments())
            if not comments:
                # No comments yet, if it's draft=False and ready for review, not actively working
                return False
            
            # Get the most recent comment
            last_comment = comments[-1]
            last_comment_author = last_comment.user.login if last_comment.user else ''
            last_comment_body = last_comment.body or ''
            
            # Check if last comment is from Copilot (responding to feedback)
            if 'copilot' in last_comment_author.lower():
                return True
            
            # Check if last comment mentions @copilot (asking for work)
            if '@copilot' in last_comment_body.lower():
                return True
            
            # Check if there's a recent review comment (last few comments)
            # Look at last 3 comments to see if there's ongoing review discussion
            recent_comments = comments[-3:]
            for comment in recent_comments:
                body = comment.body or ''
                # Check for review-related markers
                if '@copilot' in body.lower():
                    return True
                # Check if it's a review comment from our system
                if '[copilot:' in body.lower() or 'review feedback' in body.lower():
                    return True
            
            # If none of the above, Copilot is not actively working
            return False
            
        except Exception as e:
            self.logger.warning(f"Failed to check if Copilot is working on PR #{pr.number}: {e}")
            # Default to True (assume working) to be conservative with capacity
            return True
