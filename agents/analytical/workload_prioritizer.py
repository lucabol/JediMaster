"""Prioritizes items without deciding actions."""
import logging
from github import Github
from core.models import RepoState, PrioritizedWorkload

COPILOT_STATE_LABEL_PREFIX = "copilot-state:"
MERGE_ATTEMPT_LABEL_PREFIX = "copilot-merge-attempt:"


class WorkloadPrioritizer:
    """Sorts and prioritizes work items for optimal processing."""
    
    def __init__(self, github: Github, merge_max_retries: int = 3):
        self.github = github
        self.merge_max_retries = merge_max_retries
        self.logger = logging.getLogger('jedimaster.prioritizer')
    
    def prioritize(self, repo_name: str, repo_state: RepoState) -> PrioritizedWorkload:
        """Sort work items by priority based on repo state."""
        repo = self.github.get_repo(repo_name)
        
        quick_wins = []
        blocked_prs = []
        pending_review_prs = []
        changes_requested_prs = []
        unprocessed_issues = []
        
        # Analyze PRs
        try:
            for pr in repo.get_pulls(state='open'):
                state = self._get_pr_state_label(pr)
                
                if state == 'ready_to_merge':
                    # Check if blocked by retry limit
                    attempt_count = self._get_merge_attempt_count(pr)
                    if attempt_count >= self.merge_max_retries:
                        blocked_prs.append(pr.number)
                    else:
                        quick_wins.append(pr.number)
                elif state == 'pending_review':
                    pending_review_prs.append(pr.number)
                elif state == 'changes_requested':
                    changes_requested_prs.append(pr.number)
                elif state == 'unknown':
                    # PRs without state labels need to be classified
                    # Add them to pending_review to be processed by state machine
                    pending_review_prs.append(pr.number)
                # Note: 'blocked' and 'done' states are intentionally not processed
        except Exception as e:
            self.logger.error(f"Failed to analyze PRs: {e}")
        
        # Analyze issues
        try:
            for issue in repo.get_issues(state='open'):
                if issue.pull_request:
                    continue
                
                labels = {label.name.lower() for label in issue.labels}
                
                # Unprocessed: no copilot labels
                if not labels.intersection({'copilot-candidate', 'no-github-copilot'}):
                    unprocessed_issues.append(issue.number)
        except Exception as e:
            self.logger.error(f"Failed to analyze issues: {e}")
        
        return PrioritizedWorkload(
            quick_wins=quick_wins,
            blocked_prs=blocked_prs,
            pending_review_prs=pending_review_prs,
            changes_requested_prs=changes_requested_prs,
            unprocessed_issues=unprocessed_issues
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
