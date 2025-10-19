"""Analyzes repository state without making decisions."""
import logging
from datetime import datetime, timedelta, timezone
from github import Github
from core.models import RepoState

# Label prefix for merge attempts
MERGE_ATTEMPT_LABEL_PREFIX = "copilot-merge-attempt:"
COPILOT_STATE_LABEL_PREFIX = "copilot-state:"


class RepoStateAnalyzer:
    """Analyzes current repository state."""
    
    def __init__(self, github: Github, merge_max_retries: int = 3):
        self.github = github
        self.merge_max_retries = merge_max_retries
        self.logger = logging.getLogger('jedimaster.analyzer')
    
    def analyze(self, repo_name: str) -> RepoState:
        """Analyze repository and return current state."""
        repo = self.github.get_repo(repo_name)
        now = datetime.now(timezone.utc)
        
        # Analyze issues
        all_issues = list(repo.get_issues(state='open'))
        issues = [i for i in all_issues if not i.pull_request]
        
        unprocessed_issues = []
        copilot_assigned_issues = []
        
        for issue in issues:
            labels = {label.name.lower() for label in issue.labels}
            
            # Unprocessed: no copilot labels
            if not labels.intersection({'copilot-candidate', 'no-github-copilot'}):
                unprocessed_issues.append(issue)
            
            # Copilot assigned
            if any('copilot' in (a.login or '').lower() for a in issue.assignees):
                copilot_assigned_issues.append(issue)
        
        # Analyze PRs
        all_prs = list(repo.get_pulls(state='open'))
        
        pr_states = {
            'pending_review': [],
            'changes_requested': [],
            'ready_to_merge': [],
            'blocked': [],
            'done': []
        }
        
        for pr in all_prs:
            # Get PR state label
            state = self._get_pr_state_label(pr)
            if state in pr_states:
                pr_states[state].append(pr)
            
            # Check if blocked (exceeded retry limit)
            if state == 'ready_to_merge':
                # Check merge attempt count
                attempt_count = self._get_merge_attempt_count(pr)
                if attempt_count >= self.merge_max_retries:
                    # Move from ready_to_merge to blocked
                    pr_states['ready_to_merge'].remove(pr)
                    pr_states['blocked'].append(pr)
        
        # Calculate derived metrics
        copilot_active_issues = len(copilot_assigned_issues)
        copilot_active_prs = sum(len(prs) for state, prs in pr_states.items() if state != 'done')
        quick_wins = len(pr_states['ready_to_merge'])
        blocked_count = len(pr_states['blocked'])
        
        return RepoState(
            repo=repo_name,
            timestamp=now,
            open_issues_total=len(issues),
            open_issues_unprocessed=len(unprocessed_issues),
            open_issues_assigned_to_copilot=copilot_active_issues,
            open_prs_total=len(all_prs),
            prs_pending_review=len(pr_states['pending_review']),
            prs_changes_requested=len(pr_states['changes_requested']),
            prs_ready_to_merge=len(pr_states['ready_to_merge']),
            prs_blocked=blocked_count,
            prs_done=len(pr_states['done']),
            copilot_active_issues=copilot_active_issues,
            copilot_active_prs=copilot_active_prs,
            quick_wins_available=quick_wins,
            truly_blocked_prs=blocked_count
        )
    
    def _get_pr_state_label(self, pr) -> str:
        """Extract copilot-state label from PR."""
        try:
            for label in pr.labels:
                if label.name.startswith(COPILOT_STATE_LABEL_PREFIX):
                    return label.name[len(COPILOT_STATE_LABEL_PREFIX):]
        except Exception as e:
            self.logger.debug(f"Failed to get state label for PR #{pr.number}: {e}")
        return 'unknown'
    
    def _get_merge_attempt_count(self, pr) -> int:
        """Get number of merge attempts from labels."""
        try:
            for label in pr.labels:
                if label.name.startswith(MERGE_ATTEMPT_LABEL_PREFIX):
                    attempt_str = label.name[len(MERGE_ATTEMPT_LABEL_PREFIX):]
                    try:
                        return int(attempt_str)
                    except ValueError:
                        pass
        except Exception as e:
            self.logger.debug(f"Failed to get merge attempt count for PR #{pr.number}: {e}")
        return 0
