"""Analytical agents for repository state analysis and resource monitoring."""
import logging
from typing import List
from datetime import datetime, timezone
from github import Github
from core.models import RepoState, ResourceState, PrioritizedWorkload


class RepoStateAnalyzer:
    """Analyzes repository state without making LLM calls."""
    
    def __init__(self, github: Github):
        self.github = github
        self.logger = logging.getLogger('jedimaster.orchestrator.state_analyzer')
    
    def analyze(self, repo_name: str) -> RepoState:
        """Analyze current repository state."""
        repo = self.github.get_repo(repo_name)
        
        # Count issues
        all_issues = list(repo.get_issues(state='open'))
        prs = [i for i in all_issues if i.pull_request]
        issues = [i for i in all_issues if not i.pull_request]
        
        # Unprocessed issues = those without copilot state labels
        unprocessed = len(issues)
        
        # Categorize PRs
        prs_ready = 0
        prs_pending_review = 0
        prs_changes_requested = 0
        prs_blocked = 0
        
        for pr in prs:
            labels = {label.name for label in pr.labels}
            
            # Blocked = exceeded merge retry limit
            if 'copilot-state:blocked' in labels:
                prs_blocked += 1
            # Ready to merge = approved and mergeable
            elif any(label.startswith('copilot-state:approved') for label in labels):
                prs_ready += 1
            # Changes requested (including draft in progress)
            elif any(label.startswith('copilot-state:changes_requested') for label in labels):
                prs_changes_requested += 1
            # Pending review
            elif any(label.startswith('copilot-state:pending_review') for label in labels):
                prs_pending_review += 1
            else:
                # Default to pending review if no state label
                prs_pending_review += 1
        
        return RepoState(
            repo_name=repo_name,
            open_issues_total=len(issues),
            open_issues_unprocessed=unprocessed,
            open_prs_total=len(prs),
            prs_ready_to_merge=prs_ready,
            prs_pending_review=prs_pending_review,
            prs_changes_requested=prs_changes_requested,
            prs_blocked=prs_blocked
        )


class ResourceMonitor:
    """Monitors resource constraints (API quota, Copilot capacity)."""
    
    def __init__(self, github: Github):
        self.github = github
        self.logger = logging.getLogger('jedimaster.orchestrator.resource_monitor')
    
    def check_resources(self, repo_name: str) -> ResourceState:
        """Check current resource availability."""
        repo = self.github.get_repo(repo_name)
        rate_limit = self.github.get_rate_limit()
        
        # GitHub API quota
        core_limit = rate_limit.core
        api_remaining = core_limit.remaining
        api_limit = core_limit.limit
        
        # Count Copilot-active PRs
        # Copilot is actively working on a PR if:
        # 1. PR is open and authored by Copilot, AND
        # 2. PR has one of these indicators:
        #    - Is a draft (Copilot still working on it)
        #    - Has a comment containing '@copilot' or '[copilot:' from a human (requesting Copilot action)
        #    - Has merge conflicts and hasn't exceeded retry limit (Copilot will fix)
        #    - Has 'copilot-state:changes_requested' label with reason 'draft_in_progress'
        
        copilot_active_prs = self._count_copilot_active_prs(repo)
        
        # Copilot capacity (max concurrent PRs it can work on)
        copilot_max = 5  # Conservative estimate
        copilot_available = max(0, copilot_max - copilot_active_prs)
        
        # Estimate API budget
        estimated_budget = min(api_remaining // 10, 100)  # Conservative
        
        # Warnings
        warnings = []
        if api_remaining < api_limit * 0.1:
            warnings.append(f"Low API quota: {api_remaining}/{api_limit} remaining")
        if copilot_available == 0:
            warnings.append(f"Copilot at full capacity: {copilot_active_prs}/{copilot_max} PRs")
        
        return ResourceState(
            github_api_remaining=api_remaining,
            github_api_limit=api_limit,
            copilot_active_prs=copilot_active_prs,
            copilot_max_concurrent=copilot_max,
            copilot_available_slots=copilot_available,
            estimated_api_budget=estimated_budget,
            warnings=warnings
        )
    
    def _count_copilot_active_prs(self, repo) -> int:
        """Count PRs where Copilot is actively working."""
        count = 0
        
        for pr in repo.get_pulls(state='open'):
            # Must be authored by Copilot
            if not pr.user or 'copilot' not in pr.user.login.lower():
                continue
            
            # Check if Copilot is actively working
            # 1. Draft PRs = Copilot still working
            if pr.draft:
                count += 1
                continue
            
            # 2. Has merge conflicts and not blocked = Copilot will fix
            labels = {label.name for label in pr.labels}
            if not pr.mergeable and 'copilot-state:blocked' not in labels:
                count += 1
                continue
            
            # 3. Has recent human comment requesting Copilot action
            try:
                comments = list(pr.get_issue_comments())
                if comments:
                    latest_comment = comments[-1]
                    comment_age_hours = (datetime.now(timezone.utc) - latest_comment.created_at).total_seconds() / 3600
                    
                    # Recent comment (<24h) from non-Copilot user mentioning @copilot or [copilot:
                    if (comment_age_hours < 24 and
                        latest_comment.user and 'copilot' not in latest_comment.user.login.lower() and
                        ('@copilot' in latest_comment.body.lower() or '[copilot:' in latest_comment.body.lower())):
                        count += 1
                        continue
            except Exception as e:
                self.logger.warning(f"Error checking comments for PR #{pr.number}: {e}")
            
            # 4. Has review from reviewer agent (copilot working on review feedback)
            try:
                reviews = list(pr.get_reviews())
                if reviews:
                    latest_review = reviews[-1]
                    review_age_hours = (datetime.now(timezone.utc) - latest_review.submitted_at).total_seconds() / 3600
                    
                    # Recent review (<48h) with changes requested = Copilot addressing feedback
                    if review_age_hours < 48 and latest_review.state == 'CHANGES_REQUESTED':
                        # But only if there's been no new commit since the review
                        if pr.commits > 0:
                            commits = list(pr.get_commits())
                            if commits:
                                last_commit = commits[-1]
                                if last_commit.commit.author.date < latest_review.submitted_at:
                                    count += 1
                                    continue
            except Exception as e:
                self.logger.warning(f"Error checking reviews for PR #{pr.number}: {e}")
        
        return count


class WorkloadPrioritizer:
    """Prioritizes work items based on impact and cost."""
    
    def __init__(self, github: Github):
        self.github = github
        self.logger = logging.getLogger('jedimaster.orchestrator.workload_prioritizer')
    
    def prioritize(self, repo_name: str, state: RepoState) -> PrioritizedWorkload:
        """Prioritize work items for execution."""
        repo = self.github.get_repo(repo_name)
        
        quick_wins = []
        blocked_prs = []
        pending_review_prs = []
        changes_requested_prs = []
        unprocessed_issues = []
        
        # Categorize all items
        all_items = list(repo.get_issues(state='open'))
        
        for item in all_items:
            if item.pull_request:
                # It's a PR - fetch full PR details to check draft status and review requests
                try:
                    pr = repo.get_pull(item.number)
                    labels = {label.name for label in pr.labels}
                    
                    self.logger.info(f"Categorizing PR #{item.number}: draft={pr.draft}, labels={labels}")
                    
                    if 'copilot-state:blocked' in labels:
                        blocked_prs.append(item.number)
                        self.logger.info(f"  -> blocked_prs")
                    elif any(label.startswith('copilot-state:approved') for label in labels):
                        quick_wins.append(item.number)
                        self.logger.info(f"  -> quick_wins")
                    elif any(label.startswith('copilot-state:changes_requested') for label in labels):
                        # Draft PRs should be processed to check their state and potentially reclassify
                        # (they may be ready for review now, or still in progress)
                        is_draft = getattr(pr, 'draft', False)
                        self.logger.info(f"  PR #{item.number} has changes_requested, checking draft status: {is_draft}")
                        if is_draft:
                            pending_review_prs.append(item.number)
                            self.logger.info(f"  -> pending_review_prs (draft with changes_requested)")
                        else:
                            changes_requested_prs.append(item.number)
                            self.logger.info(f"  -> changes_requested_prs (not draft)")
                    elif any(label.startswith('copilot-state:pending_review') for label in labels):
                        pending_review_prs.append(item.number)
                        self.logger.info(f"  -> pending_review_prs")
                    else:
                        pending_review_prs.append(item.number)  # Default
                        self.logger.info(f"  -> pending_review_prs (default)")
                except Exception as e:
                    self.logger.warning(f"Error categorizing PR #{item.number}: {e}")
                    pending_review_prs.append(item.number)  # Default on error
            else:
                # It's an issue
                is_assigned_to_copilot = any('copilot' in a.login.lower() for a in item.assignees)
                if not is_assigned_to_copilot:
                    unprocessed_issues.append(item.number)
        
        return PrioritizedWorkload(
            quick_wins=quick_wins,
            blocked_prs=blocked_prs,
            pending_review_prs=pending_review_prs,
            changes_requested_prs=changes_requested_prs,
            unprocessed_issues=unprocessed_issues
        )
