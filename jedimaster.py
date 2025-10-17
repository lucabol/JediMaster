#!/usr/bin/env python3
"""
JediMaster - A tool to automatically assign GitHub issues to GitHub Copilot
based on LLM evaluation of issue suitability.
"""

import os
import json
import logging
from collections import Counter
from typing import List, Dict, Any, Optional, Tuple
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
import argparse
import requests

from github import Github, GithubException
from dotenv import load_dotenv


from decider import DeciderAgent, PRDeciderAgent
from creator import CreatorAgent
from reporting import format_table







@dataclass
class IssueResult:
    """Represents the result of processing a single issue."""
    repo: str
    issue_number: int
    title: str
    url: str
    status: str  # 'assigned', 'not_assigned', 'already_assigned', 'labeled', 'error'
    reasoning: Optional[str] = None
    error_message: Optional[str] = None


@dataclass
class PRRunResult:
    """Represents the result of processing or merging a pull request."""
    repo: str
    pr_number: int
    title: str
    status: str
    details: Optional[str] = None
    attempts: Optional[int] = None
    state_before: Optional[str] = None
    state_after: Optional[str] = None
    action: Optional[str] = None


@dataclass
class ProcessingReport:
    """Represents the result of processing multiple issues or repositories."""
    total_issues: int = 0
    processed: int = 0
    assigned: int = 0
    already_assigned: int = 0
    not_assigned: int = 0
    labeled: int = 0
    errors: int = 0
    results: List[IssueResult] = field(default_factory=list)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


COPILOT_STATE_LABEL_PREFIX = "copilot-state:"
MERGE_ATTEMPT_LABEL_PREFIX = "merge-attempt-"
HUMAN_ESCALATION_LABEL = "copilot-human-review"

STATE_INTAKE = "intake"
STATE_PENDING_REVIEW = "pending_review"
STATE_CHANGES_REQUESTED = "changes_requested"
STATE_READY_TO_MERGE = "ready_to_merge"
STATE_BLOCKED = "blocked"
STATE_DONE = "done"

COPILOT_LABEL_PALETTE = {
    STATE_PENDING_REVIEW: ("0366d6", "Awaiting Copilot review"),
    STATE_CHANGES_REQUESTED: ("d73a49", "Awaiting author updates"),
    STATE_READY_TO_MERGE: ("28a745", "Ready for merge"),
    STATE_BLOCKED: ("6a737d", "Blocked until manual action"),
    STATE_DONE: ("5319e7", "Processing complete"),
}


class JediMaster:

    def _mark_pr_ready_for_review(self, pr) -> None:
        """Mark a draft PR as ready for review via GraphQL."""
        try:
            repo_full = pr.base.repo.full_name
            owner, name = repo_full.split('/')
            query = """
            query($owner: String!, $name: String!, $number: Int!) {
              repository(owner: $owner, name: $name) {
                pullRequest(number: $number) {
                  id
                  isDraft
                }
              }
            }
            """
            variables = {"owner": owner, "name": name, "number": pr.number}
            result = self._graphql_request(query, variables)
            if 'errors' in result:
                self.logger.error(f"GraphQL query error while marking PR #{pr.number} ready: {result['errors']}")
                return
            pr_id = result['data']['repository']['pullRequest']['id']
            is_draft = result['data']['repository']['pullRequest']['isDraft']
            if not is_draft:
                return
            mutation = """
            mutation($pullRequestId: ID!) {
              markPullRequestReadyForReview(input: {pullRequestId: $pullRequestId}) {
                pullRequest {
                  isDraft
                }
              }
            }
            """
            mutation_vars = {"pullRequestId": pr_id}
            mutation_result = self._graphql_request(mutation, mutation_vars)
            if 'errors' in mutation_result:
                self.logger.error(f"GraphQL mutation error while marking PR #{pr.number} ready: {mutation_result['errors']}")
        except Exception as exc:
            self.logger.error(f"Failed to mark PR #{getattr(pr, 'number', '?')} as ready for review: {exc}")

    async def _handle_pending_review_state(self, pr, metadata: Dict[str, Any], classification: Optional[Dict[str, Any]] = None) -> List[PRRunResult]:
        repo_full = pr.base.repo.full_name
        results: List[PRRunResult] = []

        # Defensive check to ensure metadata is properly passed
        if not isinstance(metadata, dict):
            self.logger.error(f"Invalid metadata type for PR #{pr.number}: {type(metadata)}")
            return []

        self.logger.debug(f"_handle_pending_review_state called for PR #{pr.number} with metadata keys: {list(metadata.keys())}")

        if metadata.get('has_current_approval') and not metadata.get('has_new_commits_since_copilot_review'):
            self._set_state_label(pr, STATE_READY_TO_MERGE)
            results.append(
                PRRunResult(
                    repo=repo_full,
                    pr_number=pr.number,
                    title=pr.title,
                    status='state_transition',
                    details='Already approved; moved to ready_to_merge',
                    state_before=STATE_PENDING_REVIEW,
                    state_after=STATE_READY_TO_MERGE,
                    action='mark_ready',
                )
            )
            fresh_metadata = self._collect_pr_metadata(pr)
            results.extend(await self._handle_ready_to_merge_state(pr, fresh_metadata))
            return results

        pr_text_header = f"Title: {pr.title}\n\nDescription:\n{pr.body or ''}\n\n"
        diff_content, pre_result = self._fetch_pr_diff(pr, repo_full)
        if pre_result:
            results.append(pre_result)
            return results

        pr_text = pr_text_header + f"Diff:\n{diff_content[:5000]}"

        try:
            agent_result = await self.pr_decider.evaluate_pr(pr_text)
        except Exception as exc:
            self.logger.error(f"PRDecider evaluation failed for PR #{pr.number}: {exc}")
            results.append(
                PRRunResult(
                    repo=repo_full,
                    pr_number=pr.number,
                    title=pr.title,
                    status='error',
                    details=self._shorten_text(str(exc)),
                    state_before=STATE_PENDING_REVIEW,
                    state_after=STATE_PENDING_REVIEW,
                    action='review_failed',
                )
            )
            return results

        if 'comment' in agent_result:
            comment_body = f"@copilot {agent_result['comment']}"
            try:
                pr.create_review(event='REQUEST_CHANGES', body=comment_body)
            except Exception as exc:
                self.logger.error(f"Failed to request changes on PR #{pr.number}: {exc}")
                results.append(
                    PRRunResult(
                        repo=repo_full,
                        pr_number=pr.number,
                        title=pr.title,
                        status='error',
                        details=self._shorten_text(str(exc)),
                        state_before=STATE_PENDING_REVIEW,
                        state_after=STATE_PENDING_REVIEW,
                        action='request_changes_failed',
                    )
                )
                return results

            self._set_state_label(pr, STATE_CHANGES_REQUESTED)
            results.append(
                PRRunResult(
                    repo=repo_full,
                    pr_number=pr.number,
                    title=pr.title,
                    status='changes_requested',
                    details=self._shorten_text(comment_body),
                    state_before=STATE_PENDING_REVIEW,
                    state_after=STATE_CHANGES_REQUESTED,
                    action='request_changes',
                )
            )
            return results

        if agent_result.get('decision') == 'accept':
            if metadata.get('is_draft'):
                self._mark_pr_ready_for_review(pr)
                pr.update()
            try:
                pr.create_review(event='APPROVE', body='Automatically approved by JediMaster.')
            except Exception as exc:
                self.logger.error(f"Failed to approve PR #{pr.number}: {exc}")
                results.append(
                    PRRunResult(
                        repo=repo_full,
                        pr_number=pr.number,
                        title=pr.title,
                        status='error',
                        details=self._shorten_text(str(exc)),
                        state_before=STATE_PENDING_REVIEW,
                        state_after=STATE_PENDING_REVIEW,
                        action='approve_failed',
                    )
                )
                return results

            self._set_state_label(pr, STATE_READY_TO_MERGE)
            results.append(
                PRRunResult(
                    repo=repo_full,
                    pr_number=pr.number,
                    title=pr.title,
                    status='approved',
                    details='Auto-approved by JediMaster',
                    state_before=STATE_PENDING_REVIEW,
                    state_after=STATE_READY_TO_MERGE,
                    action='approve',
                )
            )
            fresh_metadata = self._collect_pr_metadata(pr)
            results.extend(await self._handle_ready_to_merge_state(pr, fresh_metadata))
            return results

        results.append(
            PRRunResult(
                repo=repo_full,
                pr_number=pr.number,
                title=pr.title,
                status='unknown',
                details=self._shorten_text(str(agent_result)),
                state_before=STATE_PENDING_REVIEW,
                state_after=STATE_PENDING_REVIEW,
                action='review_unknown',
            )
        )
        return results

    async def _handle_changes_requested_state(self, pr, metadata: Dict[str, Any], classification: Optional[Dict[str, Any]] = None) -> List[PRRunResult]:
        repo_full = pr.base.repo.full_name
        results: List[PRRunResult] = []
        latest_review = metadata.get('latest_copilot_review') or {}

        if metadata.get('has_new_commits_since_copilot_review'):
            self._set_state_label(pr, STATE_PENDING_REVIEW)
            results.append(
                PRRunResult(
                    repo=repo_full,
                    pr_number=pr.number,
                    title=pr.title,
                    status='state_transition',
                    details='New commits detected; returning to review queue',
                    state_before=STATE_CHANGES_REQUESTED,
                    state_after=STATE_PENDING_REVIEW,
                    action='requeue_review',
                )
            )
            return results

        review_time = latest_review.get('submitted_at')
        if review_time:
            review_iso = review_time.isoformat()
            message = f"Waiting for updates after Copilot requested changes on {review_iso}. Push new commits and re-request review when ready."
        else:
            message = "Waiting for updates after Copilot requested changes. Push new commits and re-request review when ready."

        self._ensure_comment_with_tag(pr, 'copilot:awaiting-updates', message)
        results.append(
            PRRunResult(
                repo=repo_full,
                pr_number=pr.number,
                title=pr.title,
                status='changes_requested',
                details='Awaiting author updates',
                state_before=STATE_CHANGES_REQUESTED,
                state_after=STATE_CHANGES_REQUESTED,
                action='await_updates',
            )
        )
        return results

    async def _handle_ready_to_merge_state(self, pr, metadata: Dict[str, Any], classification: Optional[Dict[str, Any]] = None) -> List[PRRunResult]:
        repo_full = pr.base.repo.full_name
        results: List[PRRunResult] = []

        try:
            pr.update()
        except Exception as exc:
            self.logger.error(f"Failed to refresh PR #{pr.number} before merge: {exc}")

        if not self.manage_prs:
            message = "Auto-merge is disabled; waiting for a maintainer to merge this PR manually."
            self._ensure_comment_with_tag(pr, 'copilot:auto-merge-disabled', message)
            results.append(
                PRRunResult(
                    repo=repo_full,
                    pr_number=pr.number,
                    title=pr.title,
                    status='ready_to_merge',
                    details=message,
                    state_before=STATE_READY_TO_MERGE,
                    state_after=STATE_READY_TO_MERGE,
                    action='auto_merge_disabled',
                )
            )
            return results

        current_attempts = self._get_merge_attempt_count(pr)
        if current_attempts >= self.merge_max_retries:
            self._set_state_label(pr, STATE_BLOCKED)
            self._ensure_comment_with_tag(
                pr,
                'copilot:merge-max-retries',
                f"Auto-merge stopped after {self.merge_max_retries} failed attempts. Please merge manually.",
            )
            results.append(
                PRRunResult(
                    repo=repo_full,
                    pr_number=pr.number,
                    title=pr.title,
                    status='max_retries_exceeded',
                    details='Exceeded automatic merge retry budget',
                    attempts=current_attempts,
                    state_before=STATE_READY_TO_MERGE,
                    state_after=STATE_BLOCKED,
                    action='merge_max_retries',
                )
            )
            return results

        mergeable = getattr(pr, 'mergeable', None)
        if mergeable is False:
            self._set_state_label(pr, STATE_BLOCKED)
            try:
                comment_body = "@copilot Merge conflicts detected. Resolve conflicts and push updates, then re-request review."
                pr.create_issue_comment(comment_body)
            except Exception as exc:
                self.logger.error(f"Failed to create merge conflict comment on PR #{pr.number}: {exc}")
            results.append(
                PRRunResult(
                    repo=repo_full,
                    pr_number=pr.number,
                    title=pr.title,
                    status='merge_error',
                    details='Merge conflicts detected',
                    state_before=STATE_READY_TO_MERGE,
                    state_after=STATE_BLOCKED,
                    action='merge_conflict',
                )
            )
            return results

        if metadata.get('is_draft'):
            self._mark_pr_ready_for_review(pr)
            pr.update()

        attempt = self._increment_merge_attempt_count(pr)
        try:
            merge_result = pr.merge(merge_method='squash', commit_message=f"Auto-merged by JediMaster: {pr.title}")
        except Exception as exc:
            self.logger.error(f"Merge attempt failed for PR #{pr.number}: {exc}")
            self._ensure_comment_with_tag(
                pr,
                'copilot:merge-exception',
                f"Auto-merge failed: {exc}. Please investigate and retry.",
            )
            self._set_state_label(pr, STATE_BLOCKED)
            results.append(
                PRRunResult(
                    repo=repo_full,
                    pr_number=pr.number,
                    title=pr.title,
                    status='merge_error',
                    details=self._shorten_text(str(exc)),
                    attempts=attempt,
                    state_before=STATE_READY_TO_MERGE,
                    state_after=STATE_BLOCKED,
                    action='merge_exception',
                )
            )
            return results

        if getattr(merge_result, 'merged', False):
            self._remove_merge_attempt_labels(pr)
            self._set_state_label(pr, STATE_DONE)
            closed_issues: List[int] = []
            try:
                closed_issues = self._close_linked_issues(pr.base.repo, pr.number, pr.title)
            except Exception as exc:
                self.logger.error(f"Failed closing linked issues for PR #{pr.number}: {exc}")
            try:
                self._delete_pr_branch(pr)
            except Exception as exc:
                self.logger.error(f"Failed to delete branch for PR #{pr.number}: {exc}")

            details = 'Auto-merged successfully'
            if closed_issues:
                details += f"; closed issues {closed_issues}"

            results.append(
                PRRunResult(
                    repo=repo_full,
                    pr_number=pr.number,
                    title=pr.title,
                    status='merged',
                    details=details,
                    attempts=attempt,
                    state_before=STATE_READY_TO_MERGE,
                    state_after=STATE_DONE,
                    action='merge',
                )
            )
            return results

        failure_message = getattr(merge_result, 'message', 'Merge failed for unknown reasons')
        self._ensure_comment_with_tag(
            pr,
            'copilot:merge-failed',
            f"Auto-merge failed: {failure_message}. Please resolve and retry.",
        )
        self._set_state_label(pr, STATE_BLOCKED)
        results.append(
            PRRunResult(
                repo=repo_full,
                pr_number=pr.number,
                title=pr.title,
                status='merge_error',
                details=self._shorten_text(failure_message),
                attempts=attempt,
                state_before=STATE_READY_TO_MERGE,
                state_after=STATE_BLOCKED,
                action='merge_failed',
            )
        )
        return results

    async def _handle_blocked_state(self, pr, metadata: Dict[str, Any], classification: Optional[Dict[str, Any]] = None) -> List[PRRunResult]:
        repo_full = pr.base.repo.full_name
        reason = (classification or {}).get('reason', 'waiting_signal')

        message_map = {
            'draft': "This PR is still marked as a draft. Mark it ready for review when Copilot's work is complete.",
            'merge_conflict': "Merge protection is blocking automatic merge. Resolve conflicts or required checks.",
            'waiting_signal': "Waiting for a manual signal before proceeding. Re-request review when ready.",
        }
        message = message_map.get(reason, "Waiting for manual action before continuing.")
        tag = f'copilot:blocked-{reason}'
        self._ensure_comment_with_tag(pr, tag, message)

        return [
            PRRunResult(
                repo=repo_full,
                pr_number=pr.number,
                title=pr.title,
                status='blocked',
                details=message,
                state_before=STATE_BLOCKED,
                state_after=STATE_BLOCKED,
                action=f'blocked_{reason}',
            )
        ]

    async def _handle_done_state(self, pr, metadata: Dict[str, Any]) -> List[PRRunResult]:
        repo_full = pr.base.repo.full_name
        self._remove_merge_attempt_labels(pr)
        return [
            PRRunResult(
                repo=repo_full,
                pr_number=pr.number,
                title=pr.title,
                status='state_transition',
                details='Cleanup complete',
                state_before=STATE_DONE,
                state_after=STATE_DONE,
                action='done_cleanup',
            )
        ]

    async def _process_pr_state_machine(self, pr) -> List[PRRunResult]:
        results: List[PRRunResult] = []
        repo_full = pr.base.repo.full_name

        if self._has_label(pr, HUMAN_ESCALATION_LABEL):
            results.append(
                PRRunResult(
                    repo=repo_full,
                    pr_number=pr.number,
                    title=pr.title,
                    status='human_escalated',
                    details='Escalated to human reviewer (label present).',
                    action='skip_human_escalation',
                )
            )
            return results

        should_escalate, comment_count = self._should_escalate_for_human(pr)
        if should_escalate:
            self._escalate_pr_to_human(pr, comment_count)
            results.append(
                PRRunResult(
                    repo=repo_full,
                    pr_number=pr.number,
                    title=pr.title,
                    status='human_escalated',
                    details=f'Escalated to human reviewer after {comment_count} comments.',
                    action='apply_human_escalation',
                )
            )
            return results

        metadata = self._collect_pr_metadata(pr)
        classification = self._classify_pr_state(pr, metadata)
        self.logger.info(f"PR #{pr.number} classified as: {classification}")
        desired_state = classification['state']
        current_state = self._get_state_label(pr)

        if current_state is None:
            self._set_state_label(pr, desired_state)
            results.append(
                PRRunResult(
                    repo=repo_full,
                    pr_number=pr.number,
                    title=pr.title,
                    status='state_transition',
                    details=f"Initial classification: {classification['reason']}",
                    state_before=STATE_INTAKE,
                    state_after=desired_state,
                    action='classify',
                )
            )
            current_state = desired_state
        elif current_state != desired_state:
            previous_state = current_state
            self._set_state_label(pr, desired_state)
            results.append(
                PRRunResult(
                    repo=repo_full,
                    pr_number=pr.number,
                    title=pr.title,
                    status='state_transition',
                    details=f"Reclassified: {classification['reason']}",
                    state_before=previous_state,
                    state_after=desired_state,
                    action='reclassify',
                )
            )
            current_state = desired_state

        handler_map = {
            STATE_PENDING_REVIEW: self._handle_pending_review_state,
            STATE_CHANGES_REQUESTED: self._handle_changes_requested_state,
            STATE_READY_TO_MERGE: self._handle_ready_to_merge_state,
            STATE_BLOCKED: self._handle_blocked_state,
        }

        if current_state == STATE_DONE:
            # Only clean up if residual merge attempt labels remain.
            try:
                labels = pr.get_labels() if hasattr(pr, 'get_labels') else pr.labels
                if any((getattr(label, 'name', '') or '').startswith(MERGE_ATTEMPT_LABEL_PREFIX) for label in labels):
                    results.extend(self._handle_done_state(pr, metadata))
            except Exception:
                pass
            return results

        handler = handler_map.get(current_state)
        if handler:
            try:
                # Debug logging to help identify the metadata scoping issue
                self.logger.debug(f"About to call handler for PR #{pr.number} state {current_state}")
                self.logger.debug(f"Metadata keys: {list(metadata.keys()) if isinstance(metadata, dict) else 'NOT_DICT'}")
                handler_results = await handler(pr, metadata, classification)
                results.extend(handler_results)
            except Exception as exc:
                import traceback
                self.logger.error(f"Handler failure for PR #{pr.number} state {current_state}: {exc}")
                self.logger.error(f"Full traceback: {traceback.format_exc()}")
                results.append(
                    PRRunResult(
                        repo=repo_full,
                        pr_number=pr.number,
                        title=pr.title,
                        status='error',
                        details=self._shorten_text(str(exc)),
                        state_before=current_state,
                        state_after=current_state,
                        action='handler_error',
                    )
                )
        return results

    async def manage_pull_requests(self, repo_name: str, batch_size: int = 15) -> List[PRRunResult]:
        results: List[PRRunResult] = []
        try:
            repo = self.github.get_repo(repo_name)
            pulls = list(repo.get_pulls(state='open'))
            if batch_size:
                pulls = pulls[:batch_size]
            self.logger.info(f"[StateMachine] Managing {len(pulls)} open PRs in {repo_name}")
            for pr in pulls:
                pr_results = await self._process_pr_state_machine(pr)
                results.extend(pr_results)
        except Exception as exc:
            self.logger.error(f"Failed to manage PRs in {repo_name}: {exc}")
            results.append(
                PRRunResult(
                    repo=repo_name,
                    pr_number=0,
                    title='PR management error',
                    status='error',
                    details=self._shorten_text(str(exc)),
                    action='manage_failure',
                )
            )
        return results

    # Helper methods for state machine

    def _remove_merge_attempt_labels(self, pr) -> None:
        try:
            label_iterable = pr.get_labels() if hasattr(pr, 'get_labels') else pr.labels
            for label in list(label_iterable):
                name = getattr(label, 'name', '') or ''
                if name.startswith(MERGE_ATTEMPT_LABEL_PREFIX):
                    try:
                        pr.remove_from_labels(name)
                    except Exception as exc:
                        self.logger.debug(f"Failed to remove merge attempt label {name} from PR #{pr.number}: {exc}")
        except Exception as exc:
            self.logger.error(f"Failed to clean merge attempt labels for PR #{getattr(pr, 'number', '?')}: {exc}")
    
    def _close_linked_issues(self, repo, pr_number: int, pr_title: str) -> List[int]:
        """Close issues that are linked to the merged PR and return list of closed issue numbers."""
        closed_issues: List[int] = []
        
        try:
            # GraphQL query to find issues that close with this PR
            query = """
            query($owner: String!, $name: String!, $number: Int!) {
              repository(owner: $owner, name: $name) {
                pullRequest(number: $number) {
                  closingIssuesReferences(first: 50) {
                    edges {
                      node {
                        number
                        state
                        title
                      }
                    }
                  }
                }
              }
            }
            """
            
            variables = {
                "owner": repo.owner.login,
                "name": repo.name,
                "number": pr_number
            }
            
            result = self._graphql_request(query, variables)
            if "errors" in result:
                self.logger.error(f"GraphQL errors when fetching linked issues for PR #{pr_number}: {result['errors']}")
                return closed_issues
                
            closing_issues = result["data"]["repository"]["pullRequest"]["closingIssuesReferences"]["edges"]
            pr_url = f"https://github.com/{repo.full_name}/pull/{pr_number}"
            
            # Close each linked issue
            for edge in closing_issues:
                issue_data = edge["node"]
                issue_number = issue_data["number"]
                issue_state = issue_data["state"]
                
                try:
                    if issue_state == 'OPEN':
                        issue = repo.get_issue(issue_number)
                        
                        # Add a comment before closing
                        close_comment = f"Closed by PR #{pr_number}: {pr_url}"
                        issue.create_comment(close_comment)
                        
                        # Close the issue
                        issue.edit(state='closed')
                        
                        self.logger.info(f"Closed issue #{issue_number} linked to PR #{pr_number}")
                        closed_issues.append(issue_number)
                    else:
                        self.logger.info(f"Issue #{issue_number} linked to PR #{pr_number} was already closed")
                        
                except Exception as e:
                    self.logger.error(f"Failed to close linked issue #{issue_number} for PR #{pr_number}: {e}")
            
            if closed_issues:
                self.logger.info(f"Successfully closed {len(closed_issues)} issues linked to PR #{pr_number}: {closed_issues}")
            else:
                self.logger.debug(f"No open linked issues found for PR #{pr_number}")
                
        except Exception as e:
            self.logger.error(f"Error processing linked issues for PR #{pr_number}: {e}")
        
        return closed_issues
    
    def _delete_pr_branch(self, pr) -> bool:
        """Delete the branch associated with a pull request after successful merge."""
        try:
            head_repo = pr.head.repo
            head_branch_name = pr.head.ref
            base_repo = pr.base.repo
            
            # Only delete the branch if it's from the same repository (not a fork)
            if head_repo.full_name != base_repo.full_name:
                self.logger.info(f"PR #{pr.number} is from a fork ({head_repo.full_name}), skipping branch deletion")
                return False
            
            # Don't delete protected branches (main, master, develop, etc.)
            protected_branches = ['main', 'master', 'develop', 'development', 'staging', 'production']
            if head_branch_name.lower() in protected_branches:
                self.logger.info(f"PR #{pr.number} branch '{head_branch_name}' is a protected branch, skipping deletion")
                return False
            
            # Delete the branch
            git_ref = head_repo.get_git_ref(f"heads/{head_branch_name}")
            git_ref.delete()
            
            self.logger.info(f"Successfully deleted branch '{head_branch_name}' for PR #{pr.number}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error deleting branch for PR #{pr.number}: {e}")
            return False
    
    def _repo_has_topic(self, repo, topic: str) -> bool:
        """Check if a repository has a specific topic."""
        try:
            topics = repo.get_topics()
            return topic in topics
        except Exception as e:
            self.logger.warning(f"Could not fetch topics for {repo.full_name}: {e}")
            return False

    def _file_exists_in_repo(self, repo, filename: str) -> bool:
        """Check if a file exists in the root of the repository."""
        try:
            repo.get_contents(filename)
            return True
        except Exception:
            return False

    def _shorten_text(self, text: Optional[str], limit: int = 80) -> str:
        if not text:
            return ""
        cleaned = " ".join(text.strip().split())
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: limit - 1] + "…"

    def _friendly_issue_status(self, status: str) -> str:
        mapping = {
            'assigned': 'assigned ✅',
            'labeled': 'labeled 🏷️',
            'not_assigned': 'not assigned',
            'already_assigned': 'already assigned 🔁',
            'error': 'error ⚠️',
        }
        return mapping.get(status, status.replace('_', ' '))

    def _friendly_pr_status(self, status: str) -> str:
        mapping = {
            'approved': 'approved ✅',
            'changes_requested': 'changes requested ✏️',
            'skipped': 'skipped',
            'error': 'error ⚠️',
            'unknown': 'unknown',
            'merged': 'merged ✅',
            'merge_error': 'merge error ⚠️',
            'max_retries_exceeded': 'max retries 🚫',
            'state_changed': 'state changed',
            'state_transition': 'state transition',
            'blocked': 'blocked ⛔',
            'ready_to_merge': 'ready to merge 🚦',
            'human_escalated': 'human escalated 🔍',
        }
        return mapping.get(status, status.replace('_', ' '))

    def _get_state_label(self, pr) -> Optional[str]:
        """Return the current copilot-state label for the PR (without prefix)."""
        try:
            label_iterable = pr.get_labels() if hasattr(pr, 'get_labels') else pr.labels
            for label in label_iterable:
                name = getattr(label, 'name', '') or ''
                if name.startswith(COPILOT_STATE_LABEL_PREFIX):
                    return name[len(COPILOT_STATE_LABEL_PREFIX):]
        except Exception as exc:
            self.logger.error(f"Failed to read state label for PR #{getattr(pr, 'number', '?')}: {exc}")
        return None

    def _ensure_label_exists(self, repo, name: str, color: str, description: str) -> None:
        """Ensure a label exists on the repository."""
        try:
            repo.get_label(name)
        except Exception:
            try:
                repo.create_label(name=name, color=color, description=description)
            except GithubException as ghe:
                if ghe.status == 422:
                    # Race condition: label already exists – safe to ignore.
                    return
                raise

    def _clear_state_labels(self, pr) -> None:
        try:
            label_iterable = pr.get_labels() if hasattr(pr, 'get_labels') else pr.labels
            for label in list(label_iterable):
                name = getattr(label, 'name', '') or ''
                if name.startswith(COPILOT_STATE_LABEL_PREFIX):
                    try:
                        pr.remove_from_labels(name)
                    except Exception as exc:
                        self.logger.error(f"Failed to remove label {name} from PR #{pr.number}: {exc}")
        except Exception as exc:
            self.logger.error(f"Failed to clear state labels for PR #{getattr(pr, 'number', '?')}: {exc}")

    def _set_state_label(self, pr, state: str) -> None:
        desired = f"{COPILOT_STATE_LABEL_PREFIX}{state}"
        try:
            repo = pr.base.repo
        except Exception:
            repo = getattr(pr, 'repository', None)

        current_state = self._get_state_label(pr)
        if current_state == state:
            return

        self._clear_state_labels(pr)

        color, description = COPILOT_LABEL_PALETTE.get(
            state,
            ("cccccc", f"Copilot state: {state}"),
        )

        if repo is not None:
            try:
                self._ensure_label_exists(repo, desired, color, description)
            except Exception as exc:
                self.logger.error(f"Failed to ensure label {desired} on {repo.full_name}: {exc}")
                return

        try:
            pr.add_to_labels(desired)
        except Exception as exc:
            self.logger.error(f"Failed to apply state label {desired} to PR #{pr.number}: {exc}")

    def _remove_merge_attempt_labels(self, pr) -> None:
        try:
            label_iterable = pr.get_labels() if hasattr(pr, 'get_labels') else pr.labels
            for label in list(label_iterable):
                name = getattr(label, 'name', '') or ''
                if name.startswith(MERGE_ATTEMPT_LABEL_PREFIX):
                    try:
                        pr.remove_from_labels(name)
                    except Exception as exc:
                        self.logger.debug(f"Failed to remove merge attempt label {name} from PR #{pr.number}: {exc}")
        except Exception as exc:
            self.logger.error(f"Failed to clean merge attempt labels for PR #{getattr(pr, 'number', '?')}: {exc}")

    def _ensure_comment_with_tag(self, pr, tag: str, message: str) -> None:
        """Create a single comment tagged with marker text if not already present."""
        marker = f"[{tag}]"
        try:
            existing = pr.get_issue_comments()
            for comment in existing:
                body = comment.body or ''
                if marker in body:
                    return
        except Exception as exc:
            self.logger.error(f"Failed to enumerate comments for PR #{getattr(pr, 'number', '?')}: {exc}")
            return

        body = f"{marker}\n{message}"
        try:
            pr.create_issue_comment(body)
        except Exception as exc:
            self.logger.error(f"Failed to create tagged comment on PR #{getattr(pr, 'number', '?')}: {exc}")

    def _has_label(self, pr, label_name: str) -> bool:
        try:
            label_iterable = pr.get_labels() if hasattr(pr, 'get_labels') else pr.labels
            for label in label_iterable:
                if (getattr(label, 'name', '') or '') == label_name:
                    return True
        except Exception as exc:
            self.logger.debug(f"Failed to inspect labels for PR #{getattr(pr, 'number', '?')}: {exc}")
        return False

    def _collect_back_and_forth_stats(self, pr) -> Tuple[int, set[str]]:
        events: List[Tuple[Optional[datetime], str]] = []

        def _append_event(created_at, login, body) -> None:
            if not body or not body.strip():
                return
            events.append((created_at, login or ''))

        try:
            for comment in pr.get_issue_comments():
                _append_event(getattr(comment, 'created_at', None), getattr(getattr(comment, 'user', None), 'login', ''), getattr(comment, 'body', ''))
        except Exception as exc:
            self.logger.debug(f"Failed to load issue comments for PR #{getattr(pr, 'number', '?')}: {exc}")

        try:
            for comment in pr.get_review_comments():
                _append_event(getattr(comment, 'created_at', None), getattr(getattr(comment, 'user', None), 'login', ''), getattr(comment, 'body', ''))
        except Exception as exc:
            self.logger.debug(f"Failed to load review comments for PR #{getattr(pr, 'number', '?')}: {exc}")

        try:
            for review in pr.get_reviews():
                created = getattr(review, 'submitted_at', None) or getattr(review, 'created_at', None)
                _append_event(created, getattr(getattr(review, 'user', None), 'login', ''), getattr(review, 'body', ''))
        except Exception as exc:
            self.logger.debug(f"Failed to load reviews for PR #{getattr(pr, 'number', '?')}: {exc}")

        events = [event for event in events if event[0] is not None]
        events.sort(key=lambda item: item[0])

        count = 0
        participants: set[str] = set()
        for _, login in events:
            normalized = (login or '').lower()
            participant = 'copilot' if 'copilot' in normalized else 'human'
            participants.add(participant)
            count += 1

        return count, participants

    def _should_escalate_for_human(self, pr) -> Tuple[bool, int]:
        count, participants = self._collect_back_and_forth_stats(pr)
        if count > 9 and {'copilot', 'human'}.issubset(participants):
            return True, count
        return False, count

    def _escalate_pr_to_human(self, pr, comment_count: int) -> None:
        message = (
            "This PR has had more than nine back-and-forth comments between Copilot and contributors. "
            "Escalating to a human reviewer for follow-up."
        )
        try:
            repo = pr.base.repo
            self._ensure_label_exists(
                repo,
                HUMAN_ESCALATION_LABEL,
                "8b949e",
                "Copilot handed off to a human reviewer after extensive discussion.",
            )
        except Exception as exc:
            self.logger.error(f"Failed to ensure human escalation label for PR #{getattr(pr, 'number', '?')}: {exc}")

        if not self._has_label(pr, HUMAN_ESCALATION_LABEL):
            try:
                pr.add_to_labels(HUMAN_ESCALATION_LABEL)
            except Exception as exc:
                self.logger.error(f"Failed to apply human escalation label to PR #{getattr(pr, 'number', '?')}: {exc}")

        # Include comment count to give maintainers quick context.
        self._ensure_comment_with_tag(
            pr,
            'copilot:human-escalation',
            f"{message}\nDetected comment exchanges: {comment_count}.",
        )

    def _collect_pr_metadata(self, pr) -> Dict[str, Any]:
        """Collect key PR metadata needed for state classification."""

        def _normalize_dt(value: Optional[datetime]) -> Optional[datetime]:
            if value is None:
                return None
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)

        try:
            pr.update()
        except Exception as exc:
            self.logger.error(f"Failed to refresh PR #{getattr(pr, 'number', '?')}: {exc}")

        metadata: Dict[str, Any] = {}
        metadata['number'] = getattr(pr, 'number', None)
        metadata['title'] = getattr(pr, 'title', '')
        metadata['state'] = getattr(pr, 'state', '')
        metadata['merged'] = getattr(pr, 'merged', False)
        metadata['is_draft'] = getattr(pr, 'draft', False)
        metadata['author'] = getattr(getattr(pr, 'user', None), 'login', None)
        metadata['mergeable'] = getattr(pr, 'mergeable', None)
        metadata['head_sha'] = getattr(getattr(pr, 'head', None), 'sha', None)

        labels = []
        try:
            label_iterable = pr.get_labels() if hasattr(pr, 'get_labels') else pr.labels
            labels = [getattr(label, 'name', '') or '' for label in label_iterable]
        except Exception as exc:
            self.logger.debug(f"Failed to load labels for PR #{metadata['number']}: {exc}")
        metadata['labels'] = labels

        requested_users = []
        try:
            users, _teams = pr.get_review_requests()
            requested_users = [user.login for user in users if getattr(user, 'login', None)]
        except Exception as exc:
            self.logger.warning(f"Failed to fetch review requests for PR #{metadata['number']}: {exc}")
        metadata['requested_reviewers'] = requested_users
        metadata['copilot_review_requested'] = any('copilot' in login.lower() for login in requested_users)

        latest_reviews: Dict[str, Dict[str, Any]] = {}
        try:
            reviews = list(pr.get_reviews())
        except Exception as exc:
            self.logger.error(f"Failed to fetch reviews for PR #{metadata['number']}: {exc}")
            reviews = []

        for review in reviews:
            login = getattr(getattr(review, 'user', None), 'login', None)
            if not login:
                continue
            state = (getattr(review, 'state', '') or '').upper()
            submitted_at = getattr(review, 'submitted_at', None) or getattr(review, 'created_at', None)
            submitted_at = _normalize_dt(submitted_at)
            existing = latest_reviews.get(login)
            if existing is None or (submitted_at and submitted_at > existing.get('submitted_at')):
                latest_reviews[login] = {
                    'login': login,
                    'state': state,
                    'submitted_at': submitted_at,
                }

        metadata['latest_reviews'] = latest_reviews

        latest_copilot_review = None
        for reviewer in latest_reviews.values():
            if 'copilot' in reviewer['login'].lower():
                if latest_copilot_review is None:
                    latest_copilot_review = reviewer
                elif reviewer['submitted_at'] and reviewer['submitted_at'] > (latest_copilot_review.get('submitted_at') or datetime.min.replace(tzinfo=timezone.utc)):
                    latest_copilot_review = reviewer
        metadata['latest_copilot_review'] = latest_copilot_review

        approved_reviews = [r for r in latest_reviews.values() if r['state'] == 'APPROVED']
        metadata['approved_by'] = [r['login'] for r in approved_reviews]

        # Determine latest commit information
        last_commit = None
        last_commit_time: Optional[datetime] = None
        last_commit_sha: Optional[str] = None
        try:
            commits = pr.get_commits()
            try:
                last_commit = commits.reversed[0]
            except Exception:
                for commit in commits:
                    last_commit = commit
            if last_commit is not None:
                last_commit_sha = getattr(last_commit, 'sha', None)
                commit_obj = getattr(last_commit, 'commit', None)
                if commit_obj is not None:
                    candidate = getattr(getattr(commit_obj, 'author', None), 'date', None) or getattr(getattr(commit_obj, 'committer', None), 'date', None)
                    last_commit_time = _normalize_dt(candidate)
        except Exception as exc:
            self.logger.error(f"Failed to inspect commits for PR #{metadata['number']}: {exc}")

        metadata['last_commit_sha'] = last_commit_sha
        metadata['last_commit_time'] = last_commit_time

        latest_copilot_state = latest_copilot_review['state'] if latest_copilot_review else None
        metadata['latest_copilot_state'] = latest_copilot_state

        has_new_commits_since_copilot_review = bool(
            last_commit_time
            and latest_copilot_review
            and latest_copilot_review.get('submitted_at')
            and last_commit_time > latest_copilot_review['submitted_at']
        )
        metadata['has_new_commits_since_copilot_review'] = has_new_commits_since_copilot_review

        has_current_approval = False
        for review_data in approved_reviews:
            submitted_at = review_data.get('submitted_at')
            if last_commit_time and submitted_at and submitted_at < last_commit_time:
                continue
            has_current_approval = True
            break
        metadata['has_current_approval'] = has_current_approval

        metadata['has_copilot_approval'] = any(
            'copilot' in review['login'].lower()
            and review['state'] == 'APPROVED'
            and (not last_commit_time or (review.get('submitted_at') and review['submitted_at'] >= last_commit_time))
            for review in approved_reviews
        )

        if any(review['state'] == 'CHANGES_REQUESTED' for review in latest_reviews.values()):
            metadata['review_decision'] = 'CHANGES_REQUESTED'
        elif has_current_approval:
            metadata['review_decision'] = 'APPROVED'
        else:
            metadata['review_decision'] = 'REVIEW_REQUIRED'

        metadata['copilot_changes_requested_pending'] = bool(
            latest_copilot_review
            and latest_copilot_review.get('state') == 'CHANGES_REQUESTED'
            and not has_new_commits_since_copilot_review
        )

        return metadata

    def _classify_pr_state(self, pr, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """Return the recommended state and reason for a PR."""

        mergeable = metadata.get('mergeable')
        is_draft = metadata.get('is_draft', False)
        has_current_approval = metadata.get('has_current_approval', False)
        has_new_commits = metadata.get('has_new_commits_since_copilot_review', False)
        copilot_changes_pending = metadata.get('copilot_changes_requested_pending', False)
        copilot_review_requested = metadata.get('copilot_review_requested', False)
        review_decision = metadata.get('review_decision')
        last_commit_time = metadata.get('last_commit_time')

        if metadata.get('merged') or metadata.get('state') == 'closed':
            return {'state': STATE_DONE, 'reason': 'pr_closed'}

        if copilot_changes_pending:
            return {'state': STATE_CHANGES_REQUESTED, 'reason': 'awaiting_author'}

        if (
            has_current_approval
            and not has_new_commits
            and mergeable is True
            and not is_draft
        ):
            return {'state': STATE_READY_TO_MERGE, 'reason': 'ready'}

        needs_review = False
        requested_reviewers = metadata.get('requested_reviewers', [])
        self.logger.info(f"PR #{pr.number}: draft={is_draft}, reviewers={requested_reviewers}")
        
        if copilot_review_requested:
            # If Copilot review is explicitly requested, it needs review regardless of draft status
            needs_review = True
        elif requested_reviewers and not is_draft:
            # If any reviewers are requested on a non-draft PR, it needs review
            # This handles the case where a blocked PR gets a review re-request
            needs_review = True
        elif not is_draft and not has_current_approval and not copilot_changes_pending:
            if review_decision == 'REVIEW_REQUIRED':
                needs_review = True
            elif review_decision == 'APPROVED' and has_new_commits:
                needs_review = True
            elif review_decision not in ('APPROVED', 'CHANGES_REQUESTED'):
                needs_review = True
        elif not is_draft and has_new_commits and not has_current_approval:
            # Special case: PR was recently changed from draft to ready (likely Copilot finished)
            # Check if there are recent commits that might indicate Copilot just finished
            if last_commit_time:
                # If last commit was recent (within last hour), assume it needs review
                import datetime
                time_since_commit = datetime.datetime.now(datetime.timezone.utc) - last_commit_time
                if time_since_commit.total_seconds() < 3600:  # 1 hour
                    needs_review = True

        # Key insight: If a draft PR has human reviewers requested, Copilot likely finished work
        if is_draft and requested_reviewers:
            # Draft with human reviewers requested suggests Copilot finished and wants human review
            human_reviewers = [r for r in requested_reviewers if 'copilot' not in r.lower()]
            if human_reviewers:
                needs_review = True

        if needs_review:
            reason = 'awaiting_review'
            if last_commit_time:
                reason += '_after_commit'
            if copilot_review_requested:
                reason += '_copilot_requested'
            return {'state': STATE_PENDING_REVIEW, 'reason': reason}

        # Only block for draft if there's no active review request
        if is_draft:
            if copilot_review_requested:
                # Draft but review requested - Copilot might be done, treat as pending review
                return {'state': STATE_PENDING_REVIEW, 'reason': 'copilot_review_on_draft'}
            else:
                return {'state': STATE_BLOCKED, 'reason': 'draft'}

        if mergeable is False and has_current_approval:
            return {'state': STATE_BLOCKED, 'reason': 'merge_conflict'}

        return {'state': STATE_BLOCKED, 'reason': 'waiting_signal'}

    def _fetch_pr_diff(self, pr, repo_full_name: str) -> tuple[Optional[str], Optional[PRRunResult]]:
        """Return the textual diff for a PR or an early result if unavailable."""
        diff_chunks: List[str] = []
        try:
            files = list(pr.get_files())
        except Exception as exc:
            self.logger.warning(f"Failed to get files for PR #{pr.number} – falling back to raw diff: {exc}")
            files = []

        if files:
            for file in files:
                patch = getattr(file, 'patch', None)
                filename = getattr(file, 'filename', 'unknown')
                if patch:
                    diff_chunks.append(f"\n--- {filename} ---\n{patch}\n")

        if not diff_chunks:
            # Fallback to diff endpoint
            try:
                headers = {
                    "Accept": "application/vnd.github.v3.diff",
                    "Authorization": f"Bearer {self.github_token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                }
                response = requests.get(pr.diff_url, headers=headers, timeout=20)
                response.raise_for_status()
                if response.text.strip():
                    diff_chunks.append(response.text)
            except Exception as exc:
                tag = 'copilot:no-diff'
                message = (
                    "I could not retrieve the file changes for this PR automatically. "
                    "If this PR still needs review, please ensure commits are pushed and try again."
                )
                self._ensure_comment_with_tag(pr, tag, message)
                return None, PRRunResult(
                    repo=repo_full_name,
                    pr_number=pr.number,
                    title=pr.title,
                    status='skipped',
                    details='Unable to retrieve diff contents',
                    state_before=STATE_PENDING_REVIEW,
                    state_after=STATE_PENDING_REVIEW,
                    action='diff_unavailable',
                )

        if not diff_chunks:
            tag = 'copilot:no-files'
            message = (
                "No file changes were detected in this PR. "
                "If work is still in progress, push your commits before requesting review."
            )
            self._ensure_comment_with_tag(pr, tag, message)
            return None, PRRunResult(
                repo=repo_full_name,
                pr_number=pr.number,
                title=pr.title,
                status='skipped',
                details='No files to review',
                state_before=STATE_PENDING_REVIEW,
                state_after=STATE_PENDING_REVIEW,
                action='no_files',
            )

        # Return the combined diff content
        return "\n".join(diff_chunks), None

    def _set_state_label(self, pr, state: str) -> None:
        """Ensure exactly one state label is set on the PR."""
        # Find the current state label and remove it
        current_state = None
        try:
            labels_to_remove = []
            label_iterable = pr.get_labels() if hasattr(pr, 'get_labels') else pr.labels
            for label in label_iterable:
                name = getattr(label, 'name', '') or ''
                if name.startswith(COPILOT_STATE_LABEL_PREFIX):
                    if current_state != name:
                        labels_to_remove.append(name)
                    current_state = name

            for label_name in labels_to_remove:
                pr.remove_from_labels(label_name)
        except Exception as exc:
            self.logger.debug(f"Failed to clean existing state labels from PR #{pr.number}: {exc}")

        # Apply the desired state label
        desired = f"{COPILOT_STATE_LABEL_PREFIX}{state}"
        if current_state == desired:
            return  # Already set correctly

        try:
            pr.add_to_labels(desired)
        except Exception as exc:
            self.logger.error(f"Failed to apply state label {desired} to PR #{pr.number}: {exc}")

    def _remove_merge_attempt_labels(self, pr) -> None:
        try:
            label_iterable = pr.get_labels() if hasattr(pr, 'get_labels') else pr.labels
            for label in list(label_iterable):
                name = getattr(label, 'name', '') or ''
                if name.startswith(MERGE_ATTEMPT_LABEL_PREFIX):
                    try:
                        pr.remove_from_labels(name)
                    except Exception as exc:
                        self.logger.debug(f"Failed to remove merge attempt label {name} from PR #{pr.number}: {exc}")
        except Exception as exc:
            self.logger.error(f"Failed to clean merge attempt labels for PR #{getattr(pr, 'number', '?')}: {exc}")

    def _get_issue_id_and_bot_id(self, repo_owner: str, repo_name: str, issue_number: int) -> tuple:
        """Get issue ID and bot ID for GraphQL assignment."""
        query = """
        query($owner: String!, $name: String!, $issueNumber: Int!) {
          repository(owner: $owner, name: $name) {
            issue(number: $issueNumber) {
              id
            }
            suggestedActors(capabilities: [CAN_BE_ASSIGNED], first: 100) {
              nodes {
                login
                __typename
                ... on Bot {
                  id
                }
                ... on User {
                  id
                }
              }
            }
          }
        }
        """
        variables = {
            "owner": repo_owner,
            "name": repo_name,
            "issueNumber": issue_number
        }
        try:
            result = self._graphql_request(query, variables)
            if "errors" in result:
                self.logger.error(f"GraphQL errors: {result['errors']}")
                return None, None, f"GraphQL errors: {result['errors']}"
            data = result["data"]
            issue_id = data["repository"]["issue"]["id"]
            bot_id = None
            suggested_actors = data["repository"]["suggestedActors"]["nodes"]
            for actor in suggested_actors:
                login = actor["login"]
                if login == "copilot-swe-agent" or "copilot" in login.lower():
                    bot_id = actor["id"]
                    self.logger.info(f"Found Copilot actor: {login} (type: {actor.get('__typename', 'Unknown')})")
                    break
            if not bot_id:
                self.logger.warning(f"No Copilot coding agent found in suggested actors for {repo_owner}/{repo_name}")
                if suggested_actors:
                    actor_logins = [actor["login"] for actor in suggested_actors]
                    self.logger.info(f"Available suggested actors: {actor_logins}")
                else:
                    self.logger.info("No suggested actors found - Copilot may not be enabled for this repository")
            return issue_id, bot_id, None
        except Exception as e:
            self.logger.error(f"Error getting issue and bot IDs: {e}")
            return None, None, str(e)

    def _assign_issue_via_graphql(self, issue_id: str, bot_id: str) -> tuple:
        """Assign an issue to a bot using GraphQL mutation."""
        mutation = """
        mutation($assignableId: ID!, $actorIds: [ID!]!) {
          replaceActorsForAssignable(input: {assignableId: $assignableId, actorIds: $actorIds}) {
            assignable {
              ... on Issue {
                id
                title
                assignees(first: 10) {
                  nodes {
                    login
                  }
                }
              }
            }
          }
        }
        """
        variables = {
            "assignableId": issue_id,
            "actorIds": [bot_id]
        }
        try:
            result = self._graphql_request(mutation, variables)
            if "errors" in result:
                self.logger.error(f"GraphQL mutation errors: {result['errors']}")
                return False, f"GraphQL mutation errors: {result['errors']}"
            assignees = result["data"]["replaceActorsForAssignable"]["assignable"]["assignees"]["nodes"]
            assigned_logins = [assignee["login"] for assignee in assignees]
            self.logger.info(f"Successfully assigned issue. Current assignees: {assigned_logins}")
            return True, None
        except Exception as e:
            self.logger.error(f"Error assigning issue via GraphQL: {e}")
            return False, str(e)

    def fetch_issues(self, repo_name: str, batch_size: int = 15):
        """Fetch open issues that haven't been processed yet.
        
        Args:
            repo_name: The repository name in format 'owner/repo'
            batch_size: Maximum number of unprocessed issues to return (default 15)
            
        Returns:
            List of unprocessed issues (limited by batch_size)
        """
        repo = self.github.get_repo(repo_name)
        all_issues = repo.get_issues(state='open')
        
        unprocessed_issues = []
        processed_labels = {'copilot-candidate', 'no-github-copilot'}
        
        for issue in all_issues:
            # Skip pull requests
            if issue.pull_request:
                continue
                
            # Check if already processed (has our labels)
            issue_label_names = {label.name.lower() for label in issue.labels}
            if issue_label_names.intersection(processed_labels):
                continue  # Skip already processed issues
                
            unprocessed_issues.append(issue)
            
            # Stop when we have enough for this batch
            if len(unprocessed_issues) >= batch_size:
                break
                
        self.logger.info(f"Found {len(unprocessed_issues)} unprocessed issues (batch size: {batch_size})")
        return unprocessed_issues

    async def process_issue(self, issue, repo_name: str) -> IssueResult:
        """Process a single issue and return an IssueResult."""
        try:
            # Skip if already assigned to Copilot
            if any('copilot' in (assignee.login or '').lower() for assignee in issue.assignees):
                return IssueResult(
                    repo=repo_name,
                    issue_number=issue.number,
                    title=issue.title,
                    url=issue.html_url,
                    status='already_assigned',
                    reasoning="Already assigned to Copilot."
                )
            # Evaluate with DeciderAgent
            result = await self.decider.evaluate_issue({'title': issue.title, 'body': issue.body or ''})
            
            # Check if agent returned an error
            if result.get('decision', '').lower() == 'error':
                self.logger.error(f"Agent evaluation failed for issue #{issue.number}: {result.get('reasoning')}")
                return IssueResult(
                    repo=repo_name,
                    issue_number=issue.number,
                    title=issue.title,
                    url=issue.html_url,
                    status='error',
                    reasoning=result.get('reasoning'),
                    error_message=result.get('reasoning')
                )
            
            if result.get('decision', '').lower() == 'yes':
                if not self.just_label:
                    try:
                        repo = issue.repository
                        repo_full_name = repo.full_name.split('/')
                        repo_owner = repo_full_name[0]
                        repo_name_only = repo_full_name[1]
                        issue_id, bot_id, lookup_error = self._get_issue_id_and_bot_id(repo_owner, repo_name_only, issue.number)
                        if issue_id and bot_id:
                            success, assign_error = self._assign_issue_via_graphql(issue_id, bot_id)
                            if success:
                                status = 'assigned'
                                # Add label only on successful assignment
                                try:
                                    issue.add_to_labels('copilot-candidate')
                                except Exception as e:
                                    self.logger.warning(f"Failed to add label to issue #{issue.number}: {e}")
                            else:
                                assign_error = assign_error or "Unknown GraphQL assignment error"
                                self.logger.error(f"GraphQL assignment failed for issue #{issue.number}: {assign_error}")
                                return IssueResult(
                                    repo=repo_name,
                                    issue_number=issue.number,
                                    title=issue.title,
                                    url=issue.html_url,
                                    status='error',
                                    reasoning=result.get('reasoning'),
                                    error_message=assign_error or "GraphQL assignment failed"
                                )
                        else:
                            error_message = lookup_error or "Could not find issue ID or suitable bot"
                            self.logger.error(f"Could not find issue ID or suitable bot for issue #{issue.number}: {error_message}")
                            return IssueResult(
                                repo=repo_name,
                                issue_number=issue.number,
                                title=issue.title,
                                url=issue.html_url,
                                status='error',
                                reasoning=result.get('reasoning'),
                                error_message=error_message
                            )
                    except Exception as e:
                        self.logger.error(f"Failed to assign Copilot to issue #{issue.number}: {e}")
                        return IssueResult(
                            repo=repo_name,
                            issue_number=issue.number,
                            title=issue.title,
                            url=issue.html_url,
                            status='error',
                            reasoning=result.get('reasoning'),
                            error_message=str(e)
                        )
                else:
                    status = 'labeled'
                    # Add label when in just-label mode
                    try:
                        issue.add_to_labels('copilot-candidate')
                    except Exception as e:
                        self.logger.warning(f"Failed to add label to issue #{issue.number}: {e}")
                return IssueResult(
                    repo=repo_name,
                    issue_number=issue.number,
                    title=issue.title,
                    url=issue.html_url,
                    status=status,
                    reasoning=result.get('reasoning')
                )
            else:
                # Add 'no-github-copilot' label if not suitable
                try:
                    repo = issue.repository
                    no_copilot_label = None
                    for label in repo.get_labels():
                        if label.name.lower() == 'no-github-copilot':
                            no_copilot_label = label
                            break
                    if not no_copilot_label:
                        no_copilot_label = repo.create_label(
                            name="no-github-copilot",
                            color="ededed",
                            description="Issue not suitable for GitHub Copilot"
                        )
                    issue.add_to_labels(no_copilot_label)
                    self.logger.info(f"Added 'no-github-copilot' label to issue #{issue.number}")
                except Exception as e:
                    self.logger.error(f"Could not add 'no-github-copilot' label to issue #{issue.number}: {e}")
                    return IssueResult(
                        repo=repo_name,
                        issue_number=issue.number,
                        title=issue.title,
                        url=issue.html_url,
                        status='error',
                        reasoning=result.get('reasoning'),
                        error_message=f"Failed to add 'no-github-copilot' label: {e}"
                    )
                return IssueResult(
                    repo=repo_name,
                    issue_number=issue.number,
                    title=issue.title,
                    url=issue.html_url,
                    status='not_assigned',
                    reasoning=result.get('reasoning')
                )
        except Exception as e:
            self.logger.error(f"Error processing issue #{getattr(issue, 'number', '?')}: {e}")
            return IssueResult(
                repo=repo_name,
                issue_number=getattr(issue, 'number', 0),
                title=getattr(issue, 'title', 'Unknown'),
                url=getattr(issue, 'html_url', ''),
                status='error',
                error_message=str(e)
            )
    def __init__(self, github_token: str, azure_foundry_endpoint: str, just_label: bool = False, use_topic_filter: bool = True, manage_prs: bool = False):
        self.github_token = github_token
        self.azure_foundry_endpoint = azure_foundry_endpoint
        self.github = Github(github_token)
        self.just_label = just_label
        self.use_topic_filter = use_topic_filter
        self.manage_prs = manage_prs
        self.logger = self._setup_logger()
        # Get merge retry limit from environment
        self.merge_max_retries = self._get_merge_max_retries()
        # Agents will be initialized in async context managers
        self._decider = None
        self._pr_decider = None

    async def __aenter__(self):
        """Async context manager entry - initialize agents."""
        self._decider = DeciderAgent(self.azure_foundry_endpoint)
        self._pr_decider = PRDeciderAgent(self.azure_foundry_endpoint)
        await self._decider.__aenter__()
        await self._pr_decider.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit - cleanup agents."""
        if self._pr_decider:
            await self._pr_decider.__aexit__(exc_type, exc_val, exc_tb)
        if self._decider:
            await self._decider.__aexit__(exc_type, exc_val, exc_tb)

    @property
    def decider(self):
        """Access decider agent."""
        if self._decider is None:
            raise RuntimeError("JediMaster must be used as async context manager")
        return self._decider

    @property
    def pr_decider(self):
        """Access PR decider agent."""
        if self._pr_decider is None:
            raise RuntimeError("JediMaster must be used as async context manager")
        return self._pr_decider

    def _get_merge_max_retries(self) -> int:
        """Get the maximum number of merge retry attempts from environment variable."""
        try:
            max_retries = int(os.getenv('MERGE_MAX_RETRIES', '3'))
            if max_retries < 1:
                self.logger.warning(f"MERGE_MAX_RETRIES must be >= 1, using default of 3")
                return 3
            return max_retries
        except ValueError:
            self.logger.warning(f"Invalid MERGE_MAX_RETRIES value, using default of 3")
            return 3

    def _get_merge_attempt_count(self, pr) -> int:
        """Get the current merge attempt count from PR labels."""
        try:
            labels = [label.name for label in pr.labels]
            for label in labels:
                if label.startswith(MERGE_ATTEMPT_LABEL_PREFIX):
                    try:
                        return int(label.split('-')[-1])
                    except ValueError:
                        continue
            return 0
        except Exception as e:
            self.logger.error(f"Error getting merge attempt count for PR #{pr.number}: {e}")
            return 0

    def _increment_merge_attempt_count(self, pr) -> int:
        """Increment the merge attempt counter and return the new count."""
        try:
            current_count = self._get_merge_attempt_count(pr)
            new_count = current_count + 1
            
            # Remove old attempt label if it exists
            if current_count > 0:
                old_label_name = f'{MERGE_ATTEMPT_LABEL_PREFIX}{current_count}'
                try:
                    pr.remove_from_labels(old_label_name)
                except Exception as e:
                    self.logger.debug(f"Could not remove old label {old_label_name}: {e}")
            
            # Add new attempt label
            new_label_name = f'{MERGE_ATTEMPT_LABEL_PREFIX}{new_count}'
            
            # Create label if it doesn't exist
            try:
                repo = pr.repository if hasattr(pr, 'repository') else pr.base.repo
                try:
                    repo.get_label(new_label_name)
                except:
                    repo.create_label(
                        name=new_label_name,
                        color="ff9500",
                        description=f"This PR has had {new_count} merge attempt(s)"
                    )
                
                pr.add_to_labels(new_label_name)
                self.logger.info(f"Incremented merge attempt count to {new_count} for PR #{pr.number}")
                
            except Exception as e:
                self.logger.error(f"Failed to add merge attempt label to PR #{pr.number}: {e}")
            
            return new_count
        except Exception as e:
            self.logger.error(f"Error incrementing merge attempt count for PR #{pr.number}: {e}")
            return 1  # Default to 1 if we can't track properly

    async def merge_reviewed_pull_requests(self, repo_name: str, batch_size: int = 10):
        """Legacy wrapper maintained for compatibility; delegates to manage_pull_requests."""
        self.logger.info(
            "merge_reviewed_pull_requests is deprecated – calling manage_pull_requests instead."
        )
        return await self.manage_pull_requests(repo_name, batch_size=batch_size)

    async def process_pull_requests(self, repo_name: str, batch_size: int = 15):
        """Legacy wrapper maintained for compatibility; delegates to manage_pull_requests."""
        self.logger.info(
            "process_pull_requests is deprecated – calling manage_pull_requests instead."
        )
        return await self.manage_pull_requests(repo_name, batch_size=batch_size)

    def _setup_logger(self) -> logging.Logger:
        logger = logging.getLogger('jedimaster')
        logger.setLevel(logging.INFO)
        if not logger.handlers:
            handler = logging.StreamHandler()
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        return logger

    def _check_rate_limit_status(self) -> tuple[bool, str]:
        """Check if we're hitting GitHub API rate limits.
        
        Returns:
            tuple: (is_rate_limited, status_message)
        """
        try:
            rate_limit = self.github.get_rate_limit()
            
            # Debug logging to understand the rate limit object structure
            self.logger.debug(f"Rate limit object type: {type(rate_limit)}")
            self.logger.debug(f"Rate limit object attributes: {dir(rate_limit)}")
            
            # Handle different rate limit object structures
            if hasattr(rate_limit, 'core'):
                # New structure
                self.logger.info("Using rate_limit.core structure")
                core_remaining = rate_limit.core.remaining
                core_limit = rate_limit.core.limit
                reset_time = rate_limit.core.reset
                self.logger.debug(f"Core rate limit: {core_remaining}/{core_limit}, reset: {reset_time}")
            else:
                # Fallback to older structure or direct attributes
                self.logger.info("Using fallback rate limit structure")
                core_remaining = getattr(rate_limit, 'remaining', getattr(rate_limit, 'limit', 5000) - getattr(rate_limit, 'used', 0))
                core_limit = getattr(rate_limit, 'limit', 5000)
                reset_time = getattr(rate_limit, 'reset', None)
                self.logger.debug(f"Fallback rate limit: {core_remaining}/{core_limit}, reset: {reset_time}")
            
            # Log the raw values we extracted
            self.logger.info(f"GitHub API rate limit check: {core_remaining}/{core_limit} remaining")
            
            # Consider it rate limited if we have less than 10% remaining
            rate_limit_threshold = max(10, core_limit * 0.1)
            
            if core_remaining <= rate_limit_threshold:
                if reset_time:
                    try:
                        reset_str = reset_time.strftime('%H:%M:%S')
                    except:
                        reset_str = str(reset_time)
                    return True, f"Rate limit: {core_remaining}/{core_limit} remaining, resets at {reset_str}"
                else:
                    return True, f"Rate limit: {core_remaining}/{core_limit} remaining"
            
            return False, f"Rate limit OK: {core_remaining}/{core_limit} remaining"
            
        except Exception as e:
            self.logger.warning(f"Failed to check rate limit status: {e}")
            return False, "Rate limit check failed"

    def _graphql_request(self, query: str, variables: Optional[Dict] = None) -> Dict:
        url = "https://api.github.com/graphql"
        headers = {
            "Authorization": f"Bearer {self.github_token}",
            "Content-Type": "application/json",
        }
        payload: Dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables
        response = requests.post(url, json=payload, headers=headers)
        try:
            response.raise_for_status()
        except requests.HTTPError as http_err:
            body_preview = response.text[:500]
            raise RuntimeError(
                f"GraphQL request failed with status {response.status_code}: {body_preview}"
            ) from http_err
        try:
            return response.json()
        except ValueError as json_err:
            body_preview = response.text[:500]
            raise RuntimeError(
                f"Failed to decode GraphQL response as JSON: {body_preview}"
            ) from json_err


    async def process_user(self, username: str) -> ProcessingReport:
        filter_method = "topic 'managed-by-coding-agent'" if self.use_topic_filter else ".coding_agent file"
        self.logger.info(f"Processing user: {username} (filtering by {filter_method})")
        try:
            user = self.github.get_user(username)
            all_repos = user.get_repos()
            filtered_repos = []
            for repo in all_repos:
                if self.use_topic_filter:
                    if self._repo_has_topic(repo, "managed-by-coding-agent"):
                        filtered_repos.append(repo.full_name)
                        self.logger.info(f"Found topic 'managed-by-coding-agent' in repository: {repo.full_name}")
                else:
                    if self._file_exists_in_repo(repo, ".coding_agent"):
                        filtered_repos.append(repo.full_name)
                        self.logger.info(f"Found .coding_agent file in repository: {repo.full_name}")
            if not filtered_repos:
                filter_desc = "topic 'managed-by-coding-agent'" if self.use_topic_filter else ".coding_agent file"
                self.logger.info(f"No repositories found with {filter_desc} for user {username}")
                return ProcessingReport()
            filter_desc = "topic 'managed-by-coding-agent'" if self.use_topic_filter else ".coding_agent file"
            self.logger.info(f"Found {len(filtered_repos)} repositories with {filter_desc}")
            return await self.process_repositories(filtered_repos)
        except GithubException as e:
            error_msg = f"Error accessing user {username}: {e}"
            self.logger.error(error_msg)
            return ProcessingReport(
                errors=1,
                results=[IssueResult(
                    repo=f"user/{username}",
                    issue_number=0,
                    title=f"User Error: {username}",
                    url='',
                    status='error',
                    error_message=error_msg
                )]
            )
        except Exception as e:
            error_msg = f"Unexpected error processing user {username}: {e}"
            self.logger.error(error_msg)
            return ProcessingReport(
                errors=1,
                results=[IssueResult(
                    repo=f"user/{username}",
                    issue_number=0,
                    title=f"User Error: {username}",
                    url='',
                    status='error',
                    error_message=error_msg
                )]
            )

    async def process_repositories(self, repo_names: List[str]) -> ProcessingReport:
        all_results = []
        pr_results = []
        for repo_name in repo_names:
            self.logger.info(f"Processing repository: {repo_name}")
            try:
                if self.manage_prs:
                    pr_results.extend(await self.manage_pull_requests(repo_name))
                else:
                    # Only process issues if not doing PR processing
                    issues = self.fetch_issues(repo_name)
                    for issue in issues:
                        if issue.pull_request:
                            continue
                        result = await self.process_issue(issue, repo_name)
                        all_results.append(result)
            except Exception as e:
                self.logger.error(f"Failed to process repository {repo_name}: {e}")
                if not self.manage_prs:  # Only add issue error results when processing issues
                    all_results.append(IssueResult(
                        repo=repo_name,
                        issue_number=0,
                        title=f"Repository Error: {repo_name}",
                        url='',
                        status='error',
                        error_message=str(e)
                    ))
        
        # Calculate statistics based on what was actually processed
        if self.manage_prs:
            # When processing PRs, create a minimal report focused on PR results
            report = ProcessingReport(
                total_issues=0,  # No issues processed
                assigned=0,
                not_assigned=0,
                already_assigned=0,
                labeled=0,
                errors=0,
                results=[]  # No issue results
            )
            report.pr_results = pr_results
        else:
            # When processing issues, create standard issue report
            assigned_count = sum(1 for r in all_results if r.status == 'assigned')
            not_assigned_count = sum(1 for r in all_results if r.status == 'not_assigned')
            already_assigned_count = sum(1 for r in all_results if r.status == 'already_assigned')
            labeled_count = sum(1 for r in all_results if r.status == 'labeled')
            error_count = sum(1 for r in all_results if r.status == 'error')
            report = ProcessingReport(
                total_issues=len(all_results),
                assigned=assigned_count,
                not_assigned=not_assigned_count,
                already_assigned=already_assigned_count,
                labeled=labeled_count,
                errors=error_count,
                results=all_results
            )
        return report

    def save_report(self, report: ProcessingReport, filename: Optional[str] = None) -> str:
        out_filename: str
        if filename is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out_filename = f"jedimaster_report_{timestamp}.json"
        else:
            out_filename = filename
        with open(out_filename, 'w', encoding='utf-8') as f:
            json.dump(asdict(report), f, indent=2, ensure_ascii=False)
        self.logger.info(f"Report saved to {out_filename}")
        return out_filename

    def print_summary(
        self,
        report: ProcessingReport,
        context: str = "issues",
        pr_results: Optional[List[PRRunResult]] = None,
    ):
        print("\nJEDIMASTER PROCESSING SUMMARY")
        summary_rows = [("Timestamp", report.timestamp)]

        if context == "prs":
            results = pr_results if pr_results is not None else report.pr_results
            summary_rows.append(("Mode", "PR review"))
            summary_rows.append(("Pull requests reviewed", len(results)))
            status_counts = Counter(r.status for r in results)
            ordered_statuses = [
                "changes_requested",
                "approved",
                "skipped",
                "state_changed",
                "error",
                "unknown",
            ]
            for status in ordered_statuses:
                count = status_counts.get(status, 0)
                if count:
                    summary_rows.append((self._friendly_pr_status(status), count))
            for status, count in status_counts.items():
                if status not in ordered_statuses and count:
                    summary_rows.append((self._friendly_pr_status(status), count))
            print(format_table(["Metric", "Value"], summary_rows))
            if not results:
                print("\nNo pull requests met the criteria for review.")
            return

        if context == "merge":
            results = pr_results if pr_results is not None else report.pr_results
            summary_rows.append(("Mode", "Auto-merge"))
            summary_rows.append(("Pull requests evaluated", len(results)))
            status_counts = Counter(r.status for r in results)
            ordered_statuses = [
                "merged",
                "merge_error",
                "max_retries_exceeded",
                "skipped",
                "error",
            ]
            for status in ordered_statuses:
                count = status_counts.get(status, 0)
                if count:
                    summary_rows.append((self._friendly_pr_status(status), count))
            for status, count in status_counts.items():
                if status not in ordered_statuses and count:
                    summary_rows.append((self._friendly_pr_status(status), count))
            print(format_table(["Metric", "Value"], summary_rows))
            if not results:
                print("\nNo reviewed pull requests were eligible for auto-merge.")
            return

        summary_rows.extend([
            ("Total Issues", report.total_issues),
            ("Assigned", report.assigned),
            ("Labeled", report.labeled),
            ("Not Assigned", report.not_assigned),
            ("Already Assigned", report.already_assigned),
            ("Errors", report.errors),
        ])
        print(format_table(["Metric", "Value"], summary_rows))

        detail_rows = []
        for result in report.results:
            detail_rows.append([
                result.repo,
                f"#{result.issue_number}",
                self._friendly_issue_status(result.status),
                self._shorten_text(result.reasoning or result.error_message or ""),
            ])

        print()
        print(
            format_table(
                ["Repo", "Issue", "Status", "Details"],
                detail_rows,
                empty_message="No issues processed",
            )
        )

    def print_pr_results(self, heading: str, pr_results: List[PRRunResult]):
        print(f"\n{heading}")
        rows = []
        for result in pr_results:
            details = result.details or ""
            if result.attempts is not None:
                attempt_text = f"attempt {result.attempts}"
                details = f"{details} ({attempt_text})" if details else attempt_text
            rows.append(
                [
                    result.repo,
                    f"#{result.pr_number}",
                    self._shorten_text(result.title, 60),
                    self._friendly_pr_status(result.status),
                    self._shorten_text(details),
                ]
            )

        print(
            format_table(
                ["Repo", "PR", "Title", "Status", "Details"],
                rows,
                empty_message="No pull requests",
            )
        )


async def main():
    """Main entry point for the JediMaster script."""
    parser = argparse.ArgumentParser(description='JediMaster - Label or assign GitHub issues to Copilot and optionally process PRs')

    # Create mutually exclusive group for repositories vs user
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('repositories', nargs='*',
                       help='GitHub repositories to process (format: owner/repo)')
    group.add_argument('--user', '-u',
                       help='GitHub username to process (will process repos with topic "managed-by-coding-agent" or .coding_agent file)')

    parser.add_argument('--output', '-o',
                       help='Output filename for the report (default: auto-generated)')
    parser.add_argument('--save-report', action='store_true',
                       help='Save detailed report to JSON file (default: no)')
    parser.add_argument('--verbose', '-v', action='store_true',
                       help='Enable verbose logging')
    parser.add_argument('--just-label', action='store_true',
                       help='Only add labels to issues, do not assign them to Copilot')
    parser.add_argument('--use-file-filter', action='store_true',
                       help='Use .coding_agent file filtering instead of topic filtering (slower but backwards compatible)')

    parser.add_argument('--manage-prs', action='store_true',
                       help='Process pull requests through the state machine (review, merge, etc.) instead of processing issues')

    parser.add_argument('--create-issues', action='store_true',
                       help='Use CreatorAgent to suggest and open new issues in the specified repositories')
    parser.add_argument('--similarity-threshold', type=float, metavar='THRESHOLD',
                       help='Similarity threshold for duplicate detection when creating issues (0.0-1.0, default: 0.9 with OpenAI embeddings, 0.5 with local similarity)')

    args = parser.parse_args()

    # Validate arguments
    if not args.user and not args.repositories:
        parser.error("Either specify repositories or use --user option")

    # Determine similarity mode and threshold
    use_openai_similarity = args.similarity_threshold is not None
    similarity_threshold = args.similarity_threshold if args.similarity_threshold is not None else 0.9
    
    # Validate similarity threshold
    if not (0.0 <= similarity_threshold <= 1.0):
        parser.error("Similarity threshold must be between 0.0 and 1.0")

    # Load environment variables from .env file (if it exists)
    load_dotenv(override=True)

    # Get credentials from environment (either from .env or system environment)
    github_token = os.getenv('GITHUB_TOKEN')
    azure_foundry_endpoint = os.getenv('AZURE_AI_FOUNDRY_ENDPOINT')

    if not github_token:
        print("Error: GITHUB_TOKEN environment variable is required")
        print("Set it in .env file or as a system environment variable")
        return 1

    def _mask_token(token: str) -> str:
        if len(token) <= 10:
            return token
        return f"{token[:6]}...{token[-4:]}"

    print(f"Using GITHUB_TOKEN: {_mask_token(github_token)}")

    if not azure_foundry_endpoint:
        print("Error: AZURE_AI_FOUNDRY_ENDPOINT environment variable is required")
        print("Set it in .env file or as a system environment variable")
        print("Authentication to Azure AI Foundry will use managed identity (DefaultAzureCredential)")
        return 1

    # Set up logging level
    if args.verbose:
        logging.getLogger('jedimaster').setLevel(logging.DEBUG)


    try:
        use_topic_filter = not args.use_file_filter

        # If --create-issues is set, use CreatorAgent for each repo
        if args.create_issues:
            if args.user:
                print("--create-issues does not support --user mode. Please specify repositories explicitly.")
                return 1
            if not args.repositories:
                print("No repositories specified for --create-issues.")
                return 1
            for repo_full_name in args.repositories:
                print(f"\n[CreatorAgent] Suggesting and opening issues for {repo_full_name}...")
                if use_openai_similarity:
                    print(f"Using OpenAI embeddings with similarity threshold: {similarity_threshold}")
                else:
                    print(f"Using local word-based similarity detection (threshold: 0.5)")
                async with CreatorAgent(github_token, azure_foundry_endpoint, None, repo_full_name, similarity_threshold=similarity_threshold, use_openai_similarity=use_openai_similarity) as creator:
                    await creator.create_issues()
            return 0

        async with JediMaster(
            github_token,
            azure_foundry_endpoint,
            just_label=args.just_label,
            use_topic_filter=use_topic_filter,
            manage_prs=args.manage_prs
        ) as jedimaster:

            # Process based on input type
            if args.user:
                print(f"Processing user: {args.user}")
                report = await jedimaster.process_user(args.user)
                repo_names = [r.repo for r in report.results] if report.results else []
            else:
                print(f"Processing {len(args.repositories)} repositories...")
                report = await jedimaster.process_repositories(args.repositories)
                repo_names = args.repositories

            # Process repositories
            if args.manage_prs:
                print("Processing pull requests through state machine...")
            else:
                print("Processing issues for assignment...")
            
            # All repository and issue processing is handled in process_repositories now
            # based on the manage_prs flag

            # Save and display results
            if args.save_report:
                filename = jedimaster.save_report(report, args.output)
                print(f"\nDetailed report saved to: {filename}")
            else:
                print("\nReport not saved (use --save-report to save to file)")
            summary_context = "issues"
            summary_pr_results: Optional[List[PRRunResult]] = None
            # Display results based on mode
            if args.manage_prs:
                jedimaster.print_pr_results("PULL REQUEST MANAGEMENT RESULTS", report.pr_results if hasattr(report, 'pr_results') else [])
            else:
                jedimaster.print_summary(report, context="issues")
            return 0

    except Exception as e:
        print(f"Fatal error: {e}")
        return 1

async def process_issues_api(input_data: dict) -> dict:
    """API function to process all issues from a list of repositories via Azure Functions or other callers."""
    github_token = os.getenv('GITHUB_TOKEN')
    azure_foundry_endpoint = os.getenv('AZURE_AI_FOUNDRY_ENDPOINT')
    if not github_token or not azure_foundry_endpoint:
        return {"error": "Missing GITHUB_TOKEN or AZURE_AI_FOUNDRY_ENDPOINT in environment"}
    try:
        just_label = _get_issue_action_from_env()
    except Exception as e:
        return {"error": str(e)}
    
    repo_names = input_data.get('repo_names')
    if not repo_names or not isinstance(repo_names, list):
        return {"error": "Missing or invalid repo_names (should be a list) in input"}
    
    try:
        async with JediMaster(github_token, azure_foundry_endpoint, just_label=just_label) as jm:
            report = await jm.process_repositories(repo_names)
            return asdict(report)
    except Exception as e:
        return {"error": str(e)}

async def process_user_api(input_data: dict) -> dict:
    """API function to process all repositories for a user via Azure Functions or other callers."""
    github_token = os.getenv('GITHUB_TOKEN')
    azure_foundry_endpoint = os.getenv('AZURE_AI_FOUNDRY_ENDPOINT')
    if not github_token or not azure_foundry_endpoint:
        return {"error": "Missing GITHUB_TOKEN or AZURE_AI_FOUNDRY_ENDPOINT in environment"}
    try:
        just_label = _get_issue_action_from_env()
    except Exception as e:
        return {"error": str(e)}
    
    username = input_data.get('username')
    if not username:
        return {"error": "Missing username in input"}
    
    try:
        async with JediMaster(github_token, azure_foundry_endpoint, just_label=just_label) as jm:
            report = await jm.process_user(username)
            return asdict(report)
    except Exception as e:
        return {"error": str(e)}
        return asdict(report)
    except Exception as e:
        return {"error": str(e)}
def _get_issue_action_from_env() -> bool:
    """
    Retrieve and validate the ISSUE_ACTION environment variable.
    Returns True if action is 'label', False if 'assign'.
    Raises ValueError for invalid values.
    If not set, defaults to 'label'.
    """
    action = os.getenv('ISSUE_ACTION')
    if action is None:
        return True  # Default to labeling
    action = action.strip().lower()
    if action == 'label':
        return True
    elif action == 'assign':
        return False
    else:
        raise ValueError(f"Invalid ISSUE_ACTION: {action}. Must be 'assign' or 'label'.")

if __name__ == '__main__':
    import asyncio
    exit(asyncio.run(main()))
