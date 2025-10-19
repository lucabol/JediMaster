"""Repository analyzer agent - analyzes repository metrics and health."""

import logging
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional
from github import Github
from github.Repository import Repository
from functools import partial

from core.models import RepoMetrics, HealthStatus

logger = logging.getLogger(__name__)


class RepoAnalyzerAgent:
    """Analyzes repository metrics without making any LLM calls."""
    
    def __init__(self, github_client: Github):
        """Initialize the repo analyzer.
        
        Args:
            github_client: GitHub API client
        """
        self.github = github_client
        self.logger = logging.getLogger(f"{__name__}.RepoAnalyzerAgent")
    
    async def analyze(self, repo_name: str) -> RepoMetrics:
        """Analyze repository health and activity metrics.
        
        Args:
            repo_name: Full repository name (owner/repo)
            
        Returns:
            RepoMetrics object with comprehensive metrics
        """
        self.logger.info(f"Analyzing repository: {repo_name}")
        
        try:
            # Run the sync analysis in a thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            metrics = await loop.run_in_executor(None, self._analyze_sync, repo_name)
            return metrics
            
        except Exception as e:
            self.logger.error(f"Error analyzing repository {repo_name}: {e}")
            # Return empty metrics on error
            return RepoMetrics()
    
    def _analyze_sync(self, repo_name: str) -> RepoMetrics:
        """Synchronous analysis method that does all the GitHub API calls."""
        repo = self.github.get_repo(repo_name)
        
        # Get time windows for analysis
        now = datetime.now(timezone.utc)
        seven_days_ago = now - timedelta(days=7)
        thirty_days_ago = now - timedelta(days=30)
        
        # Collect metrics
        metrics = RepoMetrics()
        
        # Issue metrics
        self.logger.info("Collecting basic issue count...")
        metrics.open_issues = self._count_open_issues(repo)
        
        # Skip slow operations for now - these iterate through many items
        self.logger.info("Skipping closed issues count (slow operation)")
        metrics.closed_issues_7d = 0
        
        self.logger.info("Counting stale issues (limited to 50)...")
        metrics.stale_issues_7d = self._count_stale_issues(repo, seven_days_ago)
        metrics.stale_issues_30d = self._count_stale_issues(repo, thirty_days_ago)
        
        # PR metrics
        self.logger.info("Collecting PR metrics...")
        pr_metrics = self._analyze_prs(repo)
        metrics.open_prs = pr_metrics['open']
        metrics.draft_prs = pr_metrics['draft']
        metrics.ready_prs = pr_metrics['ready']
        
        # Skip merged PRs count (slow operation)
        self.logger.info("Skipping merged PRs count (slow operation)")
        metrics.merged_prs_7d = 0
        
        # Label metrics
        self.logger.info("Analyzing labels (limited to 100 issues)...")
        label_metrics = self._analyze_labels(repo)
        metrics.copilot_labeled = label_metrics['copilot']
        metrics.pending_review = label_metrics['pending_review']
        metrics.ready_to_merge = label_metrics['ready_to_merge']
        
        # Activity metrics
        self.logger.info("Calculating average issue age (limited to 50)...")
        metrics.avg_issue_age_days = self._calculate_avg_issue_age(repo)
        
        self.logger.info("Calculating average PR age...")
        metrics.avg_pr_age_days = self._calculate_avg_pr_age(repo)
        
        # Skip creation rates (slow operations)
        self.logger.info("Skipping creation rates (slow operations)")
        metrics.issue_creation_rate_7d = 0.0
        metrics.pr_creation_rate_7d = 0.0
        
        # Calculate health score and identify bottlenecks
        metrics.health_score = self._calculate_health_score(metrics)
        metrics.bottleneck = self._identify_bottleneck(metrics)
        
        self.logger.info(f"Analysis complete. Health score: {metrics.health_score:.2f}, Bottleneck: {metrics.bottleneck}")
        self.logger.info(f"  Open issues: {metrics.open_issues}, Unprocessed: {metrics.open_issues - metrics.copilot_labeled}")
        
        return metrics
    
    def _count_open_issues(self, repo: Repository) -> int:
        """Count open issues (excluding PRs)."""
        try:
            issues = repo.get_issues(state='open')
            # Count manually to exclude PRs
            count = sum(1 for issue in issues if issue.pull_request is None)
            return count
        except Exception as e:
            self.logger.error(f"Error counting open issues: {e}")
            return 0
    
    def _count_closed_issues_since(self, repo: Repository, since: datetime) -> int:
        """Count issues closed since a given date."""
        try:
            closed = repo.get_issues(state='closed', since=since)
            return closed.totalCount
        except:
            return 0
    
    def _count_stale_issues(self, repo: Repository, stale_threshold: datetime) -> int:
        """Count issues that haven't been updated since threshold."""
        try:
            count = 0
            checked = 0
            for issue in repo.get_issues(state='open', sort='updated', direction='asc'):
                checked += 1
                if issue.pull_request is None and issue.updated_at < stale_threshold:
                    count += 1
                else:
                    # If we hit a non-stale issue when sorted by oldest first, we're done
                    break
                # Limit to first 50 to avoid timeouts
                if checked >= 50:
                    break
            return count
        except:
            return 0
    
    def _analyze_prs(self, repo: Repository) -> dict:
        """Analyze PR counts by status."""
        try:
            prs = repo.get_pulls(state='open')
            open_count = 0
            draft_count = 0
            ready_count = 0
            
            for pr in prs:
                open_count += 1
                if pr.draft:
                    draft_count += 1
                else:
                    ready_count += 1
            
            return {
                'open': open_count,
                'draft': draft_count,
                'ready': ready_count,
            }
        except:
            return {'open': 0, 'draft': 0, 'ready': 0}
    
    def _count_merged_prs_since(self, repo: Repository, since: datetime) -> int:
        """Count PRs merged since a given date."""
        try:
            merged = repo.get_pulls(state='closed')
            count = 0
            for pr in merged:
                if pr.merged and pr.merged_at and pr.merged_at >= since:
                    count += 1
            return count
        except:
            return 0
    
    def _analyze_labels(self, repo: Repository) -> dict:
        """Analyze Copilot-related label usage."""
        try:
            copilot_count = 0
            pending_review_count = 0
            ready_to_merge_count = 0
            checked = 0
            
            for issue in repo.get_issues(state='open'):
                checked += 1
                labels = [label.name.lower() for label in issue.labels]
                
                # Only count copilot-candidate as processed, not no-github-copilot
                if 'copilot-candidate' in labels:
                    copilot_count += 1
                if any('pending' in label and 'review' in label for label in labels):
                    pending_review_count += 1
                if any('ready' in label and 'merge' in label for label in labels):
                    ready_to_merge_count += 1
                
                # Limit to first 100 to avoid timeouts
                if checked >= 100:
                    break
            
            return {
                'copilot': copilot_count,
                'pending_review': pending_review_count,
                'ready_to_merge': ready_to_merge_count,
            }
        except:
            return {'copilot': 0, 'pending_review': 0, 'ready_to_merge': 0}
    
    def _calculate_avg_issue_age(self, repo: Repository) -> float:
        """Calculate average age of open issues in days."""
        try:
            now = datetime.now(timezone.utc)
            total_age = 0
            count = 0
            
            for issue in repo.get_issues(state='open'):
                if issue.pull_request is None:
                    age = (now - issue.created_at).days
                    total_age += age
                    count += 1
                # Only check first 50
                if count >= 50:
                    break
            
            return total_age / count if count > 0 else 0.0
        except:
            return 0.0
    
    def _calculate_avg_pr_age(self, repo: Repository) -> float:
        """Calculate average age of open PRs in days."""
        try:
            now = datetime.now(timezone.utc)
            total_age = 0
            count = 0
            
            for pr in repo.get_pulls(state='open'):
                age = (now - pr.created_at).days
                total_age += age
                count += 1
            
            return total_age / count if count > 0 else 0.0
        except:
            return 0.0
    
    def _calculate_issue_creation_rate(self, repo: Repository, since: datetime) -> float:
        """Calculate issues created per day over the period."""
        try:
            issues = repo.get_issues(state='all', since=since)
            count = sum(1 for issue in issues if issue.pull_request is None)
            days = (datetime.now(timezone.utc) - since).days
            return count / days if days > 0 else 0.0
        except:
            return 0.0
    
    def _calculate_pr_creation_rate(self, repo: Repository, since: datetime) -> float:
        """Calculate PRs created per day over the period."""
        try:
            prs = repo.get_pulls(state='all')
            count = sum(1 for pr in prs if pr.created_at >= since)
            days = (datetime.now(timezone.utc) - since).days
            return count / days if days > 0 else 0.0
        except:
            return 0.0
    
    def _calculate_health_score(self, metrics: RepoMetrics) -> float:
        """Calculate overall repository health score (0.0-1.0).
        
        Higher score = healthier repository
        Considers: staleness, backlog size, PR status, issue age, and unprocessed issues
        """
        score = 1.0
        
        # Penalize for unprocessed issues (issues without Copilot label)
        # This is critical - fresh issues need attention too!
        unprocessed = max(0, metrics.open_issues - metrics.copilot_labeled)
        if metrics.open_issues > 0 and unprocessed > 0:
            unprocessed_ratio = unprocessed / metrics.open_issues
            score -= min(unprocessed_ratio, 1.0) * 0.4  # Up to 40% penalty for unprocessed
        
        # Penalize for stale issues (30d threshold)
        if metrics.open_issues > 0 and metrics.stale_issues_30d > 0:
            stale_ratio = metrics.stale_issues_30d / min(metrics.open_issues, 50)  # Normalized to sample size
            score -= min(stale_ratio, 1.0) * 0.3  # Up to 30% penalty
        
        # Penalize for 7d stale issues (more severe)
        if metrics.open_issues > 0 and metrics.stale_issues_7d > 0:
            stale_7d_ratio = metrics.stale_issues_7d / min(metrics.open_issues, 50)
            score -= min(stale_7d_ratio, 1.0) * 0.2  # Up to 20% penalty
        
        # Penalize for large backlog
        if metrics.open_issues > 20:
            backlog_penalty = min((metrics.open_issues - 20) / 100, 0.2)
            score -= backlog_penalty  # Up to 20% penalty
        
        # Penalize for old average issue age
        if metrics.avg_issue_age_days > 30:
            age_penalty = min((metrics.avg_issue_age_days - 30) / 180, 0.15)
            score -= age_penalty  # Up to 15% penalty for very old issues
        
        # Penalize for PR backlog
        if metrics.ready_prs > 5:
            pr_penalty = min((metrics.ready_prs - 5) / 15, 0.15)
            score -= pr_penalty  # Up to 15% penalty
        
        # Penalize for old PRs
        if metrics.avg_pr_age_days > 14:
            pr_age_penalty = min((metrics.avg_pr_age_days - 14) / 60, 0.1)
            score -= pr_age_penalty  # Up to 10% penalty
        
        return max(0.0, min(1.0, score))
    
    def _identify_bottleneck(self, metrics: RepoMetrics) -> Optional[str]:
        """Identify the primary bottleneck in the repository."""
        issues = []
        
        # Check for unprocessed issues (highest priority)
        unprocessed = max(0, metrics.open_issues - metrics.copilot_labeled)
        if unprocessed > 5:
            issues.append(("unprocessed_issues", unprocessed))
        
        # Check for stale issues
        if metrics.open_issues > 0 and metrics.stale_issues_30d > 0:
            if metrics.stale_issues_30d / metrics.open_issues > 0.5:
                issues.append(("stale_issues", metrics.stale_issues_30d))
        
        # Check for PR review backlog
        if metrics.ready_prs > 5:
            issues.append(("pr_review_backlog", metrics.ready_prs))
        
        # Check for large issue backlog
        if metrics.open_issues > 50:
            issues.append(("issue_backlog", metrics.open_issues))
        
        # Check for low activity
        if metrics.closed_issues_7d == 0 and metrics.merged_prs_7d == 0:
            issues.append(("low_activity", 0))
        
        # Return the most significant issue
        if issues:
            # Sort by severity (second element of tuple)
            issues.sort(key=lambda x: x[1], reverse=True)
            return issues[0][0]
        
        return None
