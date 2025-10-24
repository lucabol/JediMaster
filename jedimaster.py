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
        """Handler for changes_requested state.
        
        Now also handles:
        - draft_in_progress (Copilot working on draft PR)
        """
        repo_full = pr.base.repo.full_name
        results: List[PRRunResult] = []
        reason = (classification or {}).get('reason', 'awaiting_author')
        
        # Check if author pushed new commits since any reviewer requested changes - moves to pending_review
        if metadata.get('has_new_commits_since_any_review'):
            self._set_state_label(pr, STATE_PENDING_REVIEW)
            results.append(
                PRRunResult(
                    repo=repo_full,
                    pr_number=pr.number,
                    title=pr.title,
                    status='state_transition',
                    details='New commits detected after changes requested; returning to review queue',
                    state_before=STATE_CHANGES_REQUESTED,
                    state_after=STATE_PENDING_REVIEW,
                    action='requeue_review',
                )
            )
            return results
        
        # Handle different reasons for changes_requested
        if reason == 'draft_in_progress':
            message = "Draft PR in progress. Copilot is working on this. Mark as ready for review when complete."
            tag = 'copilot:draft-in-progress'
            details = 'Draft PR - Copilot working'
        else:
            # Changes requested by any reviewer (Copilot or human)
            # Find who requested changes
            latest_change_requester = None
            latest_change_time = None
            for reviewer in metadata.get('latest_reviews', {}).values():
                if reviewer['state'] == 'CHANGES_REQUESTED':
                    review_time = reviewer.get('submitted_at')
                    if latest_change_time is None or (review_time and review_time > latest_change_time):
                        latest_change_requester = reviewer['login']
                        latest_change_time = review_time
            
            if latest_change_time:
                review_iso = latest_change_time.isoformat()
                message = f"Waiting for updates after {latest_change_requester} requested changes on {review_iso}. Push new commits when ready."
            else:
                message = "Waiting for updates after reviewer requested changes. Push new commits when ready."
            tag = 'copilot:awaiting-updates'
            details = 'Awaiting author updates'

        self._ensure_comment_with_tag(pr, tag, message)
        results.append(
            PRRunResult(
                repo=repo_full,
                pr_number=pr.number,
                title=pr.title,
                status='changes_requested',
                details=details,
                state_before=STATE_CHANGES_REQUESTED,
                state_after=STATE_CHANGES_REQUESTED,
                action=f'await_updates_{reason}',
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

        # Clean up any old auto-merge-disabled comments (no longer used)
        self._remove_comment_with_tag(pr, 'copilot:auto-merge-disabled')

        if not self.manage_prs:
            # When manage_prs is disabled, don't interfere with ready-to-merge PRs
            # Just record the state and return (orchestrator or manual merge will handle it)
            results.append(
                PRRunResult(
                    repo=repo_full,
                    pr_number=pr.number,
                    title=pr.title,
                    status='ready_to_merge',
                    details='PR ready to merge (managed externally)',
                    state_before=STATE_READY_TO_MERGE,
                    state_after=STATE_READY_TO_MERGE,
                    action='ready_external_merge',
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
        """Handler for blocked state - attempts to unstick PRs.
        
        Note: After classification changes, blocked state should be rare.
        This handler attempts recovery for truly blocked PRs.
        """
        repo_full = pr.base.repo.full_name
        results: List[PRRunResult] = []
        reason = (classification or {}).get('reason', 'unknown')
        
        self.logger.info(f"PR #{pr.number} in blocked state, reason: {reason}. Attempting recovery...")
        
        # For truly blocked PRs, escalate to human after documenting
        # Most cases should now go through changes_requested or pending_review instead
        
        # Add human escalation label for stuck PRs
        if not self._has_label(pr, HUMAN_ESCALATION_LABEL):
            try:
                pr.add_to_labels(HUMAN_ESCALATION_LABEL)
                self.logger.info(f"Added human escalation label to blocked PR #{pr.number}")
            except Exception as e:
                self.logger.error(f"Failed to add escalation label to PR #{pr.number}: {e}")
        
        # Add explanatory comment
        message = f"This PR is in a blocked state (reason: {reason}). A human maintainer should review to determine next steps."
        self._ensure_comment_with_tag(pr, f'copilot:blocked-{reason}', message)
        
        results.append(
            PRRunResult(
                repo=repo_full,
                pr_number=pr.number,
                title=pr.title,
                status='human_escalated',
                details=f'Blocked PR escalated to human: {reason}',
                state_before=STATE_BLOCKED,
                state_after=STATE_BLOCKED,
                action='escalate_blocked',
            )
        )
        return results

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

        # Skip PRs that have been escalated to humans
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

        # Skip PRs assigned to humans (non-Copilot assignees) ONLY if Copilot is NOT assigned
        try:
            assignees = list(pr.assignees) if hasattr(pr, 'assignees') else []
            has_copilot_assignee = any('copilot' in getattr(a, 'login', '').lower() for a in assignees)
            has_human_assignee = any('copilot' not in getattr(a, 'login', '').lower() for a in assignees)
            
            # Only skip if there's a human assignee but NO Copilot assignee
            if has_human_assignee and not has_copilot_assignee:
                human_assignees = [getattr(a, 'login', 'unknown') for a in assignees if 'copilot' not in getattr(a, 'login', '').lower()]
                results.append(
                    PRRunResult(
                        repo=repo_full,
                        pr_number=pr.number,
                        title=pr.title,
                        status='human_assigned',
                        details=f'Assigned to human reviewer(s): {", ".join(human_assignees)} (no Copilot assignment)',
                        action='skip_human_assigned',
                    )
                )
                return results
        except Exception as exc:
            self.logger.debug(f"Failed to check assignees for PR #{getattr(pr, 'number', '?')}: {exc}")

        should_escalate, merge_conflict_count, regular_count = self._should_escalate_for_human(pr)
        if should_escalate:
            self._escalate_pr_to_human(pr, merge_conflict_count, regular_count)
            total_count = merge_conflict_count + regular_count
            results.append(
                PRRunResult(
                    repo=repo_full,
                    pr_number=pr.number,
                    title=pr.title,
                    status='human_escalated',
                    details=f'Escalated: {merge_conflict_count} merge conflict + {regular_count} regular = {total_count} comments.',
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

    def _remove_comment_with_tag(self, pr, tag: str) -> None:
        """Remove comments with a specific tag."""
        marker = f"[{tag}]"
        try:
            existing = pr.get_issue_comments()
            for comment in existing:
                body = comment.body or ''
                if marker in body:
                    try:
                        comment.delete()
                        self.logger.info(f"Removed comment with tag '{tag}' from PR #{pr.number}")
                    except Exception as exc:
                        self.logger.error(f"Failed to delete comment {comment.id} from PR #{pr.number}: {exc}")
        except Exception as exc:
            self.logger.error(f"Failed to enumerate comments for PR #{getattr(pr, 'number', '?')}: {exc}")

    def _has_label(self, pr, label_name: str) -> bool:
        try:
            label_iterable = pr.get_labels() if hasattr(pr, 'get_labels') else pr.labels
            for label in label_iterable:
                if (getattr(label, 'name', '') or '') == label_name:
                    return True
        except Exception as exc:
            self.logger.debug(f"Failed to inspect labels for PR #{getattr(pr, 'number', '?')}: {exc}")
        return False

    def _collect_back_and_forth_stats(self, pr) -> Tuple[int, int, set[str]]:
        """Collect comment statistics, distinguishing merge conflict from regular comments.
        
        Returns:
            Tuple of (merge_conflict_count, regular_count, participants)
        """
        events: List[Tuple[Optional[datetime], str, str]] = []

        def _append_event(created_at, login, body) -> None:
            if not body or not body.strip():
                return
            events.append((created_at, login or '', body or ''))

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

        merge_conflict_count = 0
        regular_count = 0
        participants: set[str] = set()
        
        for _, login, body in events:
            normalized = (login or '').lower()
            participant = 'copilot' if 'copilot' in normalized else 'human'
            participants.add(participant)
            
            # Check if this is a merge conflict comment
            body_lower = body.lower()
            if 'merge conflict' in body_lower or 'resolve conflict' in body_lower:
                merge_conflict_count += 1
            else:
                regular_count += 1

        return merge_conflict_count, regular_count, participants

    def _should_escalate_for_human(self, pr) -> Tuple[bool, int, int]:
        """Check if PR should be escalated to human based on comment counts.
        
        Returns:
            Tuple of (should_escalate, merge_conflict_count, regular_count)
        """
        merge_conflict_count, regular_count, participants = self._collect_back_and_forth_stats(pr)
        
        # Only escalate if both copilot and humans have participated
        if not {'copilot', 'human'}.issubset(participants):
            return False, merge_conflict_count, regular_count
        
        # Escalate if >10 merge conflict comments OR >20 non-merge-conflict comments
        should_escalate = merge_conflict_count > 10 or regular_count > 20
        
        return should_escalate, merge_conflict_count, regular_count

    def _escalate_pr_to_human(self, pr, merge_conflict_count: int, regular_count: int) -> None:
        """Escalate PR to human reviewer and assign a human.
        
        Args:
            pr: The pull request to escalate
            merge_conflict_count: Number of merge conflict related comments
            regular_count: Number of regular comments
        """
        total_count = merge_conflict_count + regular_count
        
        # Determine escalation reason
        if merge_conflict_count > 10:
            reason = f"excessive merge conflict discussions ({merge_conflict_count} merge conflict comments)"
        else:
            reason = f"extensive back-and-forth discussions ({total_count} total comments)"
        
        message = (
            f"This PR has had {reason} between Copilot and contributors. "
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

        # Assign to a human reviewer (get first assignee or author)
        try:
            assignees = list(pr.assignees) if hasattr(pr, 'assignees') else []
            if not assignees:
                # Assign to PR author if no assignees
                author_login = getattr(getattr(pr, 'user', None), 'login', None)
                if author_login:
                    pr.add_to_assignees(author_login)
                    self.logger.info(f"Assigned PR #{pr.number} to author {author_login} for human review")
        except Exception as exc:
            self.logger.error(f"Failed to assign human to PR #{getattr(pr, 'number', '?')}: {exc}")

        # Include comment counts to give maintainers quick context
        self._ensure_comment_with_tag(
            pr,
            'copilot:human-escalation',
            f"{message}\nMerge conflict comments: {merge_conflict_count}, Regular comments: {regular_count}, Total: {total_count}.",
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
        metadata['mergeable_state'] = getattr(pr, 'mergeable_state', None)
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
        
        # Check if ANY reviewer requested changes (not just Copilot)
        any_changes_requested = False
        has_new_commits_since_any_review = False
        for reviewer in latest_reviews.values():
            if reviewer['state'] == 'CHANGES_REQUESTED':
                # Check if there are new commits since this review
                review_time = reviewer.get('submitted_at')
                if review_time and last_commit_time and last_commit_time > review_time:
                    has_new_commits_since_any_review = True
                    continue  # New commits since this review, so changes addressed
                any_changes_requested = True
        metadata['any_changes_requested_pending'] = any_changes_requested
        metadata['has_new_commits_since_any_review'] = has_new_commits_since_any_review

        return metadata

    def _classify_pr_state(self, pr, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """Return the recommended state and reason for a PR."""

        mergeable = metadata.get('mergeable')
        is_draft = metadata.get('is_draft', False)
        has_current_approval = metadata.get('has_current_approval', False)
        has_new_commits = metadata.get('has_new_commits_since_copilot_review', False)
        copilot_changes_pending = metadata.get('copilot_changes_requested_pending', False)
        any_changes_pending = metadata.get('any_changes_requested_pending', False)
        copilot_review_requested = metadata.get('copilot_review_requested', False)
        review_decision = metadata.get('review_decision')
        last_commit_time = metadata.get('last_commit_time')
        requested_reviewers = metadata.get('requested_reviewers', [])

        if metadata.get('merged') or metadata.get('state') == 'closed'):
            return {'state': STATE_DONE, 'reason': 'pr_closed'}

        # Check if there are explicit review requests - these take priority over change requests
        # This handles the case where Copilot pushes changes and re-requests review
        if requested_reviewers and not is_draft:
            self.logger.info(f"PR #{pr.number}: Review requested from {requested_reviewers}, classifying as pending_review")
            return {'state': STATE_PENDING_REVIEW, 'reason': 'review_requested'}

        # If any reviewer (Copilot or human) requested changes, check if addressed
        if any_changes_pending:
            # If there are new commits since the review, treat as addressed
            has_new_commits_since_review = metadata.get('has_new_commits_since_any_review', False)
            if has_new_commits_since_review:
                # Changes were addressed with new commits, continue processing
                self.logger.info(f"PR #{pr.number}: Changes requested but new commits detected, continuing processing")
            else:
                # If it's a draft with merge conflicts, allow Copilot to fix them
                if is_draft and mergeable is False:
                    self.logger.info(f"PR #{pr.number}: Draft with merge conflicts, allowing Copilot to fix")
                    return {'state': STATE_PENDING_REVIEW, 'reason': 'draft_needs_conflict_resolution'}
                return {'state': STATE_CHANGES_REQUESTED, 'reason': 'awaiting_author'}

        if (
            has_current_approval
            and not has_new_commits
            and mergeable is True
            and not is_draft
        ):
            return {'state': STATE_READY_TO_MERGE, 'reason': 'ready'}

        needs_review = False
        self.logger.info(f"PR #{pr.number}: draft={is_draft}, reviewers={requested_reviewers}")
        
        # Check for draft PRs with human reviewers (Copilot finished and wants review)
        if is_draft and requested_reviewers:
            human_reviewers = [r for r in requested_reviewers if 'copilot' not in r.lower()]
            if human_reviewers:
                # Draft with human reviewers requested = Copilot done, needs review
                self.logger.info(f"PR #{pr.number}: Draft with human reviewers {human_reviewers}, treating as needs_review")
                needs_review = True
        
        if copilot_review_requested:
            # If Copilot review is explicitly requested, it needs review regardless of draft status
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

        if needs_review:
            reason = 'awaiting_review'
            if last_commit_time:
                reason += '_after_commit'
            if copilot_review_requested:
                reason += '_copilot_requested'
            return {'state': STATE_PENDING_REVIEW, 'reason': reason}

        # Handle draft PRs - treat as work in progress, not blocked
        if is_draft:
            if copilot_review_requested or requested_reviewers:
                # Draft with review requests - Copilot likely done, needs review
                return {'state': STATE_PENDING_REVIEW, 'reason': 'draft_ready_for_review'}
            else:
                # Draft in progress - Copilot should be working on it
                # BUT if it's been sitting as a draft with changes_requested label for a while,
                # it should move to pending_review for human intervention
                import datetime
                labels = metadata.get('labels', [])
                if 'copilot-state:changes_requested' in labels:
                    # Check if it's been sitting for a while (>1 hour for testing, should be 6 hours)
                    if last_commit_time:
                        time_since_commit = datetime.datetime.now(datetime.timezone.utc) - last_commit_time
                        if time_since_commit.total_seconds() > 3600:  # 1 hour (temporary for testing)
                            # Draft sitting too long - escalate for review
                            return {'state': STATE_PENDING_REVIEW, 'reason': 'draft_stale_needs_review'}
                # Still in progress
                return {'state': STATE_CHANGES_REQUESTED, 'reason': 'draft_in_progress'}

        # Default to pending review instead of blocking (let human decide)
        return {'state': STATE_PENDING_REVIEW, 'reason': 'unclear_state_defaulting_to_review'}

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

    async def orchestrated_run(self, repo_name: str, enable_issue_creation: bool = False) -> 'OrchestrationReport':
        """Execute an orchestrated run on a repository using LLM-based strategic planning.
        
        This method uses the orchestrator agent to:
        1. Analyze repository state (issues, PRs, capacity)
        2. Check resource constraints (API quota, Copilot capacity)
        3. Create strategic execution plan (what workflows, in what order)
        4. Execute the planned workflows
        5. Report outcomes and improvements
        
        Args:
            repo_name: Full repository name (owner/repo)
            enable_issue_creation: Allow orchestrator to create new issues (default: False)
            
        Returns:
            OrchestrationReport with comprehensive metrics
        """
        from datetime import datetime
        from core.models import OrchestrationReport
        from agents.orchestrator import OrchestratorAgent
        
        start_time = datetime.now()
        self.logger.info(f"[Orchestrator] Starting orchestrated run for {repo_name}")
        if enable_issue_creation:
            self.logger.info("[Orchestrator] Issue creation ENABLED")
        else:
            self.logger.info("[Orchestrator] Issue creation DISABLED (use --enable-issue-creation to enable)")
        
        # Import orchestrator
        async with OrchestratorAgent(
            github=self.github,
            azure_foundry_endpoint=self.azure_foundry_endpoint,
            model=None,  # Will use AZURE_AI_MODEL env var
            enable_issue_creation=enable_issue_creation
        ) as orchestrator:
            
            # Step 1: Analyze repository state (fast, no LLM)
            self.logger.info("[Orchestrator] Step 1/4: Analyzing repository state...")
            initial_state = orchestrator.state_analyzer.analyze(repo_name)
            
            self.logger.info("[Orchestrator] Step 2/4: Checking resources...")
            initial_resources = orchestrator.resource_monitor.check_resources(repo_name)
            
            self.logger.info("[Orchestrator] Step 3/4: Prioritizing workload...")
            workload = orchestrator.workload_prioritizer.prioritize(repo_name, initial_state)
            self.logger.info(f"[Orchestrator] Workload result: pending_review_prs={workload.pending_review_prs}, changes_requested_prs={workload.changes_requested_prs}")
            
            # Step 2: Create execution plan (ONE LLM call)
            self.logger.info("[Orchestrator] Step 4/4: Creating execution plan...")
            plan = await orchestrator.create_execution_plan(
                initial_state, initial_resources, workload
            )
            
            self.logger.info(f"[Orchestrator] Strategy: {plan.strategy}")
            for workflow in plan.workflows:
                self.logger.info(f"  - {workflow.name} (batch={workflow.batch_size}): {workflow.reasoning}")
            
            # Step 3: Execute workflows
            self.logger.info("[Orchestrator] Executing workflows...")
            workflow_results = []
            
            for workflow in plan.workflows:
                result = await self._execute_workflow(repo_name, workflow, workload)
                workflow_results.append(result)
            
            # Step 4: Final analysis
            self.logger.info("[Orchestrator] Collecting final metrics...")
            final_state = orchestrator.state_analyzer.analyze(repo_name)
            final_resources = orchestrator.resource_monitor.check_resources(repo_name)
            
            # Calculate metrics
            duration = (datetime.now() - start_time).total_seconds()
            backlog_reduction = (initial_state.open_issues_total + initial_state.open_prs_total) - \
                               (final_state.open_issues_total + final_state.open_prs_total)
            
            # Calculate health scores
            health_before = orchestrator.calculate_health_score(initial_state)
            health_after = orchestrator.calculate_health_score(final_state)
            
            return OrchestrationReport(
                repo=repo_name,
                timestamp=datetime.now(),
                initial_state=initial_state,
                initial_resources=initial_resources,
                prioritized_workload=workload,
                execution_plan=plan,
                workflow_results=workflow_results,
                final_state=final_state,
                final_resources=final_resources,
                total_duration_seconds=duration,
                backlog_reduction=backlog_reduction,
                health_score_before=health_before,
                health_score_after=health_after
            )
    
    async def _execute_workflow(self, repo_name: str, workflow: 'WorkflowStep', workload: 'PrioritizedWorkload') -> 'WorkflowResult':
        """Execute a single workflow step."""
        from core.models import WorkflowResult
        import time
        
        start = time.time()
        self.logger.info(f"[Orchestrator] Executing workflow: {workflow.name} (batch={workflow.batch_size})")
        
        try:
            if workflow.name == 'merge_ready_prs':
                # Merge ready PRs (no LLM needed)
                results = await self._execute_merge_workflow(repo_name, workflow.batch_size, workload.quick_wins)
                return WorkflowResult(
                    workflow_name=workflow.name,
                    success=True,
                    items_processed=len(results),
                    items_succeeded=sum(1 for r in results if r.status == 'merged'),
                    items_failed=sum(1 for r in results if r.status == 'error'),
                    duration_seconds=time.time() - start,
                    details=results
                )
            
            elif workflow.name == 'flag_blocked_prs':
                # Flag blocked PRs (no LLM needed)
                results = await self._execute_flag_blocked_workflow(repo_name, workload.blocked_prs)
                return WorkflowResult(
                    workflow_name=workflow.name,
                    success=True,
                    items_processed=len(results),
                    items_succeeded=len(results),
                    items_failed=0,
                    duration_seconds=time.time() - start,
                    details=results
                )
            
            elif workflow.name == 'review_prs':
                # Review PRs (uses PRDeciderAgent LLM)
                pr_numbers = workload.pending_review_prs[:workflow.batch_size]
                self.logger.info(f"Executing review_prs workflow with PR numbers: {pr_numbers} from workload.pending_review_prs={workload.pending_review_prs}")
                results = await self._execute_review_workflow(repo_name, pr_numbers)
                return WorkflowResult(
                    workflow_name=workflow.name,
                    success=True,
                    items_processed=len(results),
                    items_succeeded=sum(1 for r in results if r.status != 'error'),
                    items_failed=sum(1 for r in results if r.status == 'error'),
                    duration_seconds=time.time() - start,
                    details=results
                )
            
            elif workflow.name == 'process_issues':
                # Process issues (uses DeciderAgent LLM)
                issue_numbers = workload.unprocessed_issues[:workflow.batch_size]
                results = await self._execute_issue_workflow(repo_name, issue_numbers)
                return WorkflowResult(
                    workflow_name=workflow.name,
                    success=True,
                    items_processed=len(results),
                    items_succeeded=sum(1 for r in results if r.status == 'assigned'),
                    items_failed=sum(1 for r in results if r.status == 'error'),
                    duration_seconds=time.time() - start,
                    details=results
                )
            
            elif workflow.name == 'create_issues':
                # Create issues (uses CreatorAgent LLM)
                async with CreatorAgent(
                    self.github_token,
                    self.azure_foundry_endpoint,
                    repo_full_name=repo_name
                ) as creator:
                    results = await creator.create_issues(max_issues=workflow.batch_size)
                    return WorkflowResult(
                        workflow_name=workflow.name,
                        success=True,
                        items_processed=len(results),
                        items_succeeded=sum(1 for r in results if r.get('status') == 'created'),
                        items_failed=sum(1 for r in results if r.get('status') == 'error'),
                        duration_seconds=time.time() - start,
                        details=results
                    )
            
            else:
                self.logger.warning(f"Unknown workflow: {workflow.name}")
                return WorkflowResult(
                    workflow_name=workflow.name,
                    success=False,
                    items_processed=0,
                    items_succeeded=0,
                    items_failed=0,
                    duration_seconds=time.time() - start,
                    error=f"Unknown workflow: {workflow.name}"
                )
                
        except Exception as e:
            self.logger.error(f"Workflow {workflow.name} failed: {e}")
            return WorkflowResult(
                workflow_name=workflow.name,
                success=False,
                items_processed=0,
                items_succeeded=0,
                items_failed=0,
                duration_seconds=time.time() - start,
                error=str(e)
            )
    
    async def _execute_merge_workflow(self, repo_name: str, batch_size: int, pr_numbers: List[int]) -> List[PRRunResult]:
        """Execute merge workflow for ready PRs.
        
        Note: Temporarily enables manage_prs for orchestrated merges.
        """
        results = []
        repo = self.github.get_repo(repo_name)
        
        # Temporarily enable PR management for orchestrated merges
        original_manage_prs = self.manage_prs
        self.manage_prs = True
        
        try:
            for pr_number in pr_numbers[:batch_size]:
                try:
                    pr = repo.get_pull(pr_number)
                    # Use existing state machine to handle the merge
                    pr_results = await self._process_pr_state_machine(pr)
                    results.extend(pr_results)
                except Exception as e:
                    self.logger.error(f"Failed to merge PR #{pr_number}: {e}")
                    results.append(PRRunResult(
                        repo=repo_name,
                        pr_number=pr_number,
                        title=f"PR #{pr_number}",
                        status='error',
                        details=str(e),
                        action='merge_failed'
                    ))
        finally:
            # Restore original setting
            self.manage_prs = original_manage_prs
            
        return results
    
    async def _execute_flag_blocked_workflow(self, repo_name: str, pr_numbers: List[int]) -> List[Dict[str, Any]]:
        """Flag blocked PRs for human review."""
        results = []
        repo = self.github.get_repo(repo_name)
        
        for pr_number in pr_numbers:
            try:
                pr = repo.get_pull(pr_number)
                # Add human escalation label
                pr.add_to_labels(HUMAN_ESCALATION_LABEL)
                # Add comment explaining the situation
                pr.create_comment(
                    f"This PR has exceeded the maximum merge retry limit and needs human review. "
                    f"Please investigate the merge conflicts or other blocking issues."
                )
                self.logger.info(f"Flagged PR #{pr_number} for human review")
                results.append({
                    'pr_number': pr_number,
                    'status': 'flagged',
                    'action': 'added human-review label'
                })
            except Exception as e:
                self.logger.error(f"Failed to flag PR #{pr_number}: {e}")
                results.append({
                    'pr_number': pr_number,
                    'status': 'error',
                    'error': str(e)
                })
        return results
    
    async def _execute_review_workflow(self, repo_name: str, pr_numbers: List[int]) -> List[PRRunResult]:
        """Execute review workflow for pending PRs."""
        results = []
        repo = self.github.get_repo(repo_name)
        
        self.logger.info(f"Review workflow received PR numbers: {pr_numbers}")
        
        for pr_number in pr_numbers:
            try:
                pr = repo.get_pull(pr_number)
                self.logger.info(f"Reviewing PR #{pr_number}: draft={pr.draft}, state={pr.state}")
                # Use existing state machine to handle the review
                pr_results = await self._process_pr_state_machine(pr)
                results.extend(pr_results)
            except Exception as e:
                self.logger.error(f"Failed to review PR #{pr_number}: {e}")
                results.append(PRRunResult(
                    repo=repo_name,
                    pr_number=pr_number,
                    title=f"PR #{pr_number}",
                    status='error',
                    details=str(e),
                    action='review_failed'
                ))
        return results
    
    async def _execute_issue_workflow(self, repo_name: str, issue_numbers: List[int]) -> List[IssueResult]:
        """Execute issue processing workflow."""
        results = []
        repo = self.github.get_repo(repo_name)
        
        for issue_number in issue_numbers:
            try:
                issue = repo.get_issue(issue_number)
                result = await self.process_issue(issue, repo_name)
                results.append(result)
            except Exception as e:
                self.logger.error(f"Failed to process issue #{issue_number}: {e}")
                results.append(IssueResult(
                    repo=repo_name,
                    issue_number=issue_number,
                    title=f"Issue #{issue_number}",
                    url='',
                    status='error',
                    error_message=str(e)
                ))
        return results

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

    def print_orchestration_report(self, report: 'OrchestrationReport'):
        """Print a comprehensive orchestration report."""
        from core.models import OrchestrationReport
        
        print("\n" + "="*80)
        print(f"ORCHESTRATION REPORT: {report.repo}")
        print("="*80)
        
        # Initial State
        print("\nINITIAL STATE:")
        print(f"  Issues: {report.initial_state.open_issues_total} open")
        print(f"    - Unprocessed: {report.initial_state.open_issues_unprocessed}")
        print(f"  PRs: {report.initial_state.open_prs_total} open")
        print(f"    - Ready to merge: {report.initial_state.prs_ready_to_merge} (quick wins!)")
        print(f"    - Pending review: {report.initial_state.prs_pending_review}")
        print(f"    - Changes requested: {report.initial_state.prs_changes_requested}")
        print(f"    - Blocked: {report.initial_state.prs_blocked}")
        
        # Resources
        print("\nRESOURCES:")
        print(f"  GitHub API: {report.initial_resources.github_api_remaining}/{report.initial_resources.github_api_limit} calls")
        print(f"  Copilot Capacity: {report.initial_resources.copilot_active_prs}/{report.initial_resources.copilot_max_concurrent} active PRs ({report.initial_resources.copilot_available_slots} slots available)")
        if report.initial_resources.warnings:
            print("  Warnings:")
            for warning in report.initial_resources.warnings:
                print(f"    - {warning}")
        
        # Strategy
        print("\nSTRATEGY:")
        print(f"  {report.execution_plan.strategy}")
        
        # Workflows
        print("\nWORKFLOWS EXECUTED:")
        for workflow in report.execution_plan.workflows:
            print(f"  • {workflow.name} (batch={workflow.batch_size})")
            if workflow.reasoning:
                print(f"    Reasoning: {workflow.reasoning}")
        
        if report.execution_plan.skip_workflows:
            print("\nWORKFLOWS SKIPPED:")
            for workflow_name in report.execution_plan.skip_workflows:
                print(f"  • {workflow_name}")
        
        # Results
        print("\nRESULTS:")
        for result in report.workflow_results:
            try:
                status = "✓" if result.success else "✗"
                workflow_name = getattr(result, 'workflow_name', str(result))
                print(f"  {status} {workflow_name}:")
                print(f"     Processed: {result.items_processed}, Succeeded: {result.items_succeeded}, Failed: {result.items_failed}")
                print(f"     Duration: {result.duration_seconds:.1f}s")
                if result.error:
                    print(f"     Error: {result.error}")
            except Exception as e:
                print(f"  Error printing result: {e}, result type: {type(result)}")
        
        # Final State
        print("\nFINAL STATE:")
        print(f"  Issues: {report.final_state.open_issues_total} open (was {report.initial_state.open_issues_total})")
        print(f"    - Unprocessed: {report.final_state.open_issues_unprocessed} (was {report.initial_state.open_issues_unprocessed})")
        print(f"  PRs: {report.final_state.open_prs_total} open (was {report.initial_state.open_prs_total})")
        print(f"    - Ready to merge: {report.final_state.prs_ready_to_merge} (was {report.initial_state.prs_ready_to_merge})")
        print(f"    - Pending review: {report.final_state.prs_pending_review} (was {report.initial_state.prs_pending_review})")
        print(f"    - Changes requested: {report.final_state.prs_changes_requested} (was {report.initial_state.prs_changes_requested})")
        print(f"    - Blocked: {report.final_state.prs_blocked} (was {report.initial_state.prs_blocked})")
        print(f"  Backlog reduction: {report.backlog_reduction} items")
        
        # Metrics
        print("\nMETRICS:")
        print(f"  Duration: {report.total_duration_seconds:.1f}s")
        print(f"  Health score: {report.health_score_before:.2f} → {report.health_score_after:.2f} ({report.health_score_after - report.health_score_before:+.2f})")
        
        print("\n" + "="*80)

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
