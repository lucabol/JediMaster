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


HUMAN_ESCALATION_LABEL = "copilot-human-review"
COPILOT_ERROR_LABEL_PREFIX = "copilot-error-retry-"


class JediMaster:

    def _mark_pr_ready_for_review(self, pr) -> bool:
        """Mark a draft PR as ready for review via GraphQL.
        
        Returns True if successfully marked ready, False otherwise.
        """
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
                return False
            pr_id = result['data']['repository']['pullRequest']['id']
            is_draft = result['data']['repository']['pullRequest']['isDraft']
            if not is_draft:
                self.logger.info(f"PR #{pr.number} is already ready for review")
                return True
            
            self.logger.info(f"Marking draft PR #{pr.number} as ready for review")
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
                return False
            
            new_draft_status = mutation_result['data']['markPullRequestReadyForReview']['pullRequest']['isDraft']
            self.logger.info(f"Successfully marked PR #{pr.number} as ready (isDraft: {new_draft_status})")
            return not new_draft_status
        except Exception as exc:
            self.logger.error(f"Failed to mark PR #{getattr(pr, 'number', '?')} as ready for review: {exc}")
            return False

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

        self.logger.info(f"PR #{pr.number} ready to merge - checking draft status")
        if metadata.get('is_draft'):
            self.logger.info(f"PR #{pr.number} is a draft, marking as ready for review")
            if not self._mark_pr_ready_for_review(pr):
                self.logger.error(f"Failed to mark PR #{pr.number} as ready - cannot merge")
                results.append(
                    PRRunResult(
                        repo=repo_full,
                        pr_number=pr.number,
                        title=pr.title,
                        status='merge_error',
                        details='Failed to convert from draft to ready for review',
                        state_before=STATE_READY_TO_MERGE,
                        state_after=STATE_BLOCKED,
                        action='draft_conversion_failed',
                    )
                )
                return results
            # Force refresh the PR to get updated draft status
            try:
                repo = pr.base.repo
                pr = repo.get_pull(pr.number)
                self.logger.info(f"PR #{pr.number} refreshed, new draft status: {pr.draft}")
            except Exception as exc:
                self.logger.error(f"Failed to refresh PR #{pr.number} after marking ready: {exc}")

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
                # Remove all existing labels before adding human escalation label
                existing_labels = list(pr.get_labels())
                for label in existing_labels:
                    pr.remove_from_labels(label.name)
                pr.add_to_labels(HUMAN_ESCALATION_LABEL)
                self.logger.info(f"Added human escalation label to blocked PR #{pr.number} (removed {len(existing_labels)} other labels)")
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
        """
        Ultra-simplified PR workflow:
        1. Skip if Copilot not assigned
        2. Skip if needs human intervention
        3. Skip if Copilot is actively working
        4. Skip if PR is closed/merged
        5. Otherwise: review and either merge or comment/reassign
        """
        results: List[PRRunResult] = []
        repo_full = pr.base.repo.full_name
        repo = pr.base.repo

        # Skip PRs without Copilot assigned
        assignees = list(pr.assignees) if hasattr(pr, 'assignees') else []
        has_copilot_assigned = any('copilot' in assignee.login.lower() for assignee in assignees)
        if not has_copilot_assigned:
            print(f"  PR #{pr.number}: {pr.title[:60]} → Skipped (Copilot not assigned)")
            results.append(
                PRRunResult(
                    repo=repo_full,
                    pr_number=pr.number,
                    title=pr.title,
                    status='skipped',
                    details='Copilot not assigned to this PR',
                    action='skip',
                )
            )
            return results

        # Skip PRs that need human intervention
        if self._has_label(pr, HUMAN_ESCALATION_LABEL):
            print(f"  PR #{pr.number}: {pr.title[:60]} → Needs human review")
            results.append(
                PRRunResult(
                    repo=repo_full,
                    pr_number=pr.number,
                    title=pr.title,
                    status='human_escalated',
                    details='Escalated to human reviewer',
                    action='skip',
                )
            )
            return results

        # Skip if Copilot is actively working
        if self._is_copilot_actively_working(pr.number, repo):
            print(f"  PR #{pr.number}: {pr.title[:60]} → Copilot working")
            results.append(
                PRRunResult(
                    repo=repo_full,
                    pr_number=pr.number,
                    title=pr.title,
                    status='copilot_working',
                    details='Copilot is actively working',
                    action='skip',
                )
            )
            return results

        # Check if Copilot stopped with an error - handle retries
        copilot_work_status = self._get_copilot_work_status(pr)
        if copilot_work_status.get('last_error'):
            error_message = copilot_work_status.get('last_error', '')
            error_lower = error_message.lower()
            
            # Check if it's a rate limit error
            if 'rate limit' in error_lower:
                # Reassign to Copilot so it can retry when the rate limit clears
                pr.create_comment(
                    "@copilot Please retry this PR when the rate limit clears."
                )
                print(f"  PR #{pr.number}: {pr.title[:60]} → Rate limited (reassigned to Copilot)")
                results.append(
                    PRRunResult(
                        repo=repo_full,
                        pr_number=pr.number,
                        title=pr.title,
                        status='rate_limited',
                        details='Copilot hit rate limit, reassigned for retry',
                        action='reassign',
                    )
                )
                return results
            else:
                # Other errors - check retry count
                current_retries = self._get_copilot_error_retry_count(pr)
                if current_retries >= self.merge_max_retries:
                    # Exceeded max retries, escalate to human
                    if not self._has_label(pr, HUMAN_ESCALATION_LABEL):
                        pr.add_to_labels(HUMAN_ESCALATION_LABEL)
                        pr.create_comment(
                            f"@copilot has encountered errors {current_retries} times. "
                            f"Escalating to human review.\n\nLast error: {error_message[:200]}"
                        )
                    print(f"  PR #{pr.number}: {pr.title[:60]} → Escalated (too many errors)")
                    results.append(
                        PRRunResult(
                            repo=repo_full,
                            pr_number=pr.number,
                            title=pr.title,
                            status='human_escalated',
                            details=f'Exceeded max Copilot error retries ({current_retries})',
                            action='escalate_errors',
                        )
                    )
                    return results
                else:
                    # Increment retry count and reassign to Copilot
                    new_retry_count = self._increment_copilot_error_retry_count(pr)
                    pr.create_comment(
                        f"@copilot encountered an error. Retry {new_retry_count}/{self.merge_max_retries}.\n\n"
                        f"Error: {error_message[:200]}"
                    )
                    print(f"  PR #{pr.number}: {pr.title[:60]} → Copilot error retry {new_retry_count}/{self.merge_max_retries}")
                    results.append(
                        PRRunResult(
                            repo=repo_full,
                            pr_number=pr.number,
                            title=pr.title,
                            status='error_retry',
                            details=f'Copilot error, retry {new_retry_count}',
                            action='retry_error',
                        )
                    )
                    return results

        # Refresh PR data
        try:
            pr.update()
        except Exception as exc:
            if self.verbose:
                self.logger.error(f"Failed to refresh PR #{pr.number}: {exc}")

        # Skip if PR is closed/merged
        if pr.state == 'closed' or pr.merged:
            print(f"  PR #{pr.number}: {pr.title[:60]} → Closed/merged")
            results.append(
                PRRunResult(
                    repo=repo_full,
                    pr_number=pr.number,
                    title=pr.title,
                    status='closed',
                    details='PR is closed or merged',
                    action='skip',
                )
            )
            return results

        # Check if we already approved this PR and it's mergeable - if so, merge it
        if self._is_already_approved_by_us(pr) and getattr(pr, 'mergeable', None) is True:
            return await self._merge_pr(pr)
        
        # For all other PRs: review and act
        return await self._review_and_act_on_pr(pr)
    
    async def _cleanup_closed_pr(self, pr) -> List[PRRunResult]:
        """Clean up closed/merged PRs."""
        results: List[PRRunResult] = []
        repo_full = pr.base.repo.full_name
        
        # Nothing special to do for closed PRs currently
        results.append(
            PRRunResult(
                repo=repo_full,
                pr_number=pr.number,
                title=pr.title,
                status='closed',
                details='PR is closed or merged',
                action='skip',
            )
        )
        return results
    
    async def _review_and_act_on_pr(self, pr) -> List[PRRunResult]:
        """
        Review a PR and take action:
        - If approved and mergeable: merge it
        - Otherwise: request changes and reassign to Copilot
        """
        results: List[PRRunResult] = []
        repo_full = pr.base.repo.full_name
        
        # Get PR diff
        pr_text_header = f"Title: {pr.title}\n\nDescription:\n{pr.body or ''}\n\n"
        diff_content, pre_result = self._fetch_pr_diff(pr, repo_full)
        if pre_result:
            print(f"  PR #{pr.number}: {pr.title[:60]} → {pre_result.status} ({pre_result.details})")
            results.append(pre_result)
            return results

        pr_text = pr_text_header + f"Diff:\n{diff_content[:5000]}"

        # Call agent to evaluate PR
        try:
            agent_result = await self.pr_decider.evaluate_pr(pr_text)
        except Exception as exc:
            print(f"  PR #{pr.number}: {pr.title[:60]} → Error (review failed)")
            if self.verbose:
                self.logger.error(f"PRDecider evaluation failed for PR #{pr.number}: {exc}")
            results.append(
                PRRunResult(
                    repo=repo_full,
                    pr_number=pr.number,
                    title=pr.title,
                    status='error',
                    details=self._shorten_text(str(exc)),
                    action='review_failed',
                )
            )
            return results

        # Check if the agent result is an error (not actual feedback)
        comment_text = agent_result.get('comment', '')
        if comment_text.startswith('Error:'):
            print(f"  PR #{pr.number}: {pr.title[:60]} → Skipped (agent error)")
            if self.verbose:
                self.logger.error(f"Skipping PR #{pr.number} due to agent error: {comment_text}")
            results.append(
                PRRunResult(
                    repo=repo_full,
                    pr_number=pr.number,
                    title=pr.title,
                    status='error',
                    details=self._shorten_text(comment_text),
                    action='agent_error',
                )
            )
            return results
        
        # Check if PR is approved by agent and mergeable
        is_approved = agent_result.get('decision') == 'accept'
        is_mergeable = getattr(pr, 'mergeable', None) is True
        
        if is_approved and is_mergeable:
            # Attempt to merge immediately
            return await self._merge_pr(pr)
        else:
            # Request changes and reassign to Copilot
            # But first check if there are too many comments
            total_comments = self._count_total_comments(pr)
            
            if total_comments > self.max_comments:
                # Too many comments, escalate to human
                if not self._has_label(pr, HUMAN_ESCALATION_LABEL):
                    pr.add_to_labels(HUMAN_ESCALATION_LABEL)
                    comment = agent_result.get('comment', '')
                    if comment:
                        pr.create_comment(
                            f"This PR has {total_comments} comments (exceeds limit of {self.max_comments}). "
                            f"Escalating to human review.\n\nAgent feedback: {comment}"
                        )
                    else:
                        pr.create_comment(
                            f"This PR has {total_comments} comments (exceeds limit of {self.max_comments}). "
                            f"Escalating to human review."
                        )
                print(f"  PR #{pr.number}: {pr.title[:60]} → Escalated (too many comments: {total_comments})")
                results.append(
                    PRRunResult(
                        repo=repo_full,
                        pr_number=pr.number,
                        title=pr.title,
                        status='human_escalated',
                        details=f'Exceeded max comments ({total_comments} > {self.max_comments})',
                        action='escalate_comments',
                    )
                )
            else:
                # Normal flow: request changes and reassign
                # The comment should always be present at this point (errors handled above)
                comment = agent_result.get('comment', '')
                if not comment:
                    print(f"  PR #{pr.number}: {pr.title[:60]} → Skipped (no comment)")
                    if self.verbose:
                        self.logger.warning(f"Skipping PR #{pr.number}: agent result has neither 'accept' nor 'comment'")
                    results.append(
                        PRRunResult(
                            repo=repo_full,
                            pr_number=pr.number,
                            title=pr.title,
                            status='skipped',
                            details='Agent returned no decision or comment',
                            action='skip_no_comment',
                        )
                    )
                    return results
                
                comment_body = f"@copilot {comment}"
                try:
                    pr.create_review(event='REQUEST_CHANGES', body=comment_body)
                    print(f"  PR #{pr.number}: {pr.title[:60]} → Changes requested")
                    results.append(
                        PRRunResult(
                            repo=repo_full,
                            pr_number=pr.number,
                            title=pr.title,
                            status='changes_requested',
                            details=self._shorten_text(comment_body),
                            action='request_changes',
                        )
                    )
                except Exception as exc:
                    print(f"  PR #{pr.number}: {pr.title[:60]} → Error (comment failed)")
                    if self.verbose:
                        self.logger.error(f"Failed to request changes on PR #{pr.number}: {exc}")
                    results.append(
                        PRRunResult(
                            repo=repo_full,
                            pr_number=pr.number,
                            title=pr.title,
                            status='error',
                            details=self._shorten_text(str(exc)),
                            action='request_changes_failed',
                        )
                    )
        
        return results
    
    async def _merge_pr(self, pr) -> List[PRRunResult]:
        """Attempt to merge an approved PR."""
        results: List[PRRunResult] = []
        repo_full = pr.base.repo.full_name
        repo = pr.base.repo
        
        # Check if PR is draft and convert to ready if needed
        if getattr(pr, 'draft', False):
            self.logger.info(f"PR #{pr.number} is a draft, marking as ready for review before merge")
            if not self._mark_pr_ready_for_review(pr):
                self.logger.error(f"Failed to mark PR #{pr.number} as ready - cannot merge")
                print(f"  PR #{pr.number}: {pr.title[:60]} → Error (draft conversion failed)")
                results.append(
                    PRRunResult(
                        repo=repo_full,
                        pr_number=pr.number,
                        title=pr.title,
                        status='merge_error',
                        details='Failed to convert from draft to ready for review',
                        action='draft_conversion_failed',
                    )
                )
                return results
            # Refresh PR to get updated draft status
            try:
                pr = repo.get_pull(pr.number)
                self.logger.info(f"PR #{pr.number} refreshed after marking ready, new draft status: {pr.draft}")
            except Exception as exc:
                self.logger.error(f"Failed to refresh PR #{pr.number} after marking ready: {exc}")
        
        try:
            # Try to merge
            pr.merge(merge_method='squash')
            
            # Clean up retry labels on successful merge
            self._remove_copilot_error_retry_labels(pr)
            
            # Close linked issues
            closed_issues = self._close_linked_issues(repo, pr.number, pr.title)
            
            # Delete branch if configured
            try:
                self._delete_pr_branch(pr)
            except Exception as exc:
                if self.verbose:
                    self.logger.debug(f"Failed to delete branch for PR #{pr.number}: {exc}")
            
            details = f'Merged successfully'
            if closed_issues:
                details += f' (closed issues: {closed_issues})'
                print(f"  PR #{pr.number}: {pr.title[:60]} → Merged (closed issues: {closed_issues})")
            else:
                print(f"  PR #{pr.number}: {pr.title[:60]} → Merged")
            
            results.append(
                PRRunResult(
                    repo=repo_full,
                    pr_number=pr.number,
                    title=pr.title,
                    status='merged',
                    details=details,
                    action='merge',
                )
            )
        except Exception as exc:
            print(f"  PR #{pr.number}: {pr.title[:60]} → Error (merge failed)")
            if self.verbose:
                self.logger.error(f"Failed to merge PR #{pr.number}: {exc}")
            results.append(
                PRRunResult(
                    repo=repo_full,
                    pr_number=pr.number,
                    title=pr.title,
                    status='merge_error',
                    details=self._shorten_text(str(exc)),
                    action='merge_failed',
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
            print(f"\nProcessing {len(pulls)} open PRs:")
            for pr in pulls:
                pr_results = await self._process_pr_state_machine(pr)
                results.extend(pr_results)
        except Exception as exc:
            if self.verbose:
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

    def _is_already_approved_by_us(self, pr) -> bool:
        """
        Check if we (our bot) already approved this PR.
        Look for the most recent review from our perspective - if it's APPROVED, return True.
        """
        try:
            reviews = list(pr.get_reviews())
            if not reviews:
                return False
            
            # Check if the last review is APPROVED with our standard message
            for review in reversed(reviews):
                if review.body and 'Changes look good!' in review.body:
                    return review.state == 'APPROVED'
            
            return False
        except Exception as exc:
            self.logger.error(f"Error checking if PR #{pr.number} was approved by us: {exc}")
            return False
    
    def _is_copilot_actively_working(self, pr_number: int, repo) -> bool:
        """
        Check if Copilot is actively working on a PR by examining timeline comments.
        
        Returns True if Copilot started work but hasn't finished or stopped with error yet.
        """
        try:
            # Get the PR object
            pr = repo.get_pull(pr_number)
            
            # Use the existing _get_copilot_work_status which properly checks comments
            status = self._get_copilot_work_status(pr)
            result = status.get('is_working', False)
            
            self.logger.debug(f"PR #{pr_number}: Copilot actively working = {result}")
            return result
            
        except Exception as e:
            self.logger.error(f"Error checking if Copilot is working on PR #{pr_number}: {e}")
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


    def _get_copilot_work_status(self, pr) -> Dict[str, Any]:
        """
        Analyze timeline events to determine if Copilot is actively working.
        
        Returns dict with:
            - is_working: bool (Copilot currently working)
            - last_start: datetime or None
            - last_finish: datetime or None  
            - last_error: str or None
            - error_time: datetime or None
        """
        try:
            timeline = pr.as_issue().get_timeline()
            
            copilot_start = None
            copilot_finish = None
            copilot_error = None
            copilot_error_time = None
            
            for event in timeline:
                # Only check comment events
                event_type = getattr(event, 'event', None)
                if event_type != 'commented':
                    continue
                
                body = getattr(event, 'body', '') or ''
                created_at = getattr(event, 'created_at', None)
                
                # Normalize timezone
                if created_at and created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=timezone.utc)
                
                # Check for Copilot work events (case-insensitive)
                body_lower = body.lower()
                if 'copilot started work' in body_lower:
                    copilot_start = created_at
                    
                elif 'copilot finished work' in body_lower:
                    copilot_finish = created_at
                    
                elif 'copilot stopped work' in body_lower and 'error' in body_lower:
                    copilot_error = body[:500]  # Truncate long error messages
                    copilot_error_time = created_at
            
            # Determine if Copilot is currently working
            # Working = started but not finished/errored (or finish/error is before start)
            is_working = False
            if copilot_start:
                # Check if there's a more recent finish/error
                if copilot_finish and copilot_finish > copilot_start:
                    is_working = False  # Finished after starting
                elif copilot_error_time and copilot_error_time > copilot_start:
                    is_working = False  # Stopped with error after starting  
                else:
                    is_working = True  # Started but not finished/errored
            
            result = {
                'is_working': is_working,
                'last_start': copilot_start,
                'last_finish': copilot_finish,
                'last_error': copilot_error,
                'error_time': copilot_error_time
            }
            
            return result
            
        except Exception as exc:
            if self.verbose:
                self.logger.warning(f"Failed to check Copilot work status for PR #{pr.number}: {exc}")
            return {
                'is_working': False,
                'last_start': None,
                'last_finish': None,
                'last_error': None,
                'error_time': None
            }

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

        # Get Copilot work status from timeline events
        metadata['copilot_work_status'] = self._get_copilot_work_status(pr)

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
        
        # Get Copilot work status from timeline
        copilot_work = metadata.get('copilot_work_status', {})
        is_copilot_working = copilot_work.get('is_working', False)
        copilot_error = copilot_work.get('last_error')

        if metadata.get('merged') or metadata.get('state') == 'closed':
            return {'state': STATE_DONE, 'reason': 'pr_closed'}

        # If Copilot is actively working, don't interrupt
        if is_copilot_working:
            return {'state': STATE_CHANGES_REQUESTED, 'reason': 'copilot_working'}
        
        # If Copilot stopped with an error, handle it
        if copilot_error:
            error_lower = copilot_error.lower()
            if 'rate limit' in error_lower:
                return {'state': STATE_CHANGES_REQUESTED, 'reason': 'rate_limit_wait'}
            else:
                # Other errors - escalate to human
                return {'state': STATE_BLOCKED, 'reason': 'copilot_error'}

        # Check for draft PRs where Copilot finished but PR still draft
        if is_draft and copilot_work.get('last_finish'):
            # Copilot finished but PR is still draft - needs human to mark ready
            return {'state': STATE_PENDING_REVIEW, 'reason': 'copilot_finished_needs_ready'}

        # Check if there are explicit review requests - these take priority over change requests
        # This handles the case where Copilot pushes changes and re-requests review
        if requested_reviewers and not is_draft:
            return {'state': STATE_PENDING_REVIEW, 'reason': 'review_requested'}

        # If any reviewer (Copilot or human) requested changes, check if addressed
        if any_changes_pending:
            # If there are new commits since the review, treat as addressed
            has_new_commits_since_review = metadata.get('has_new_commits_since_any_review', False)
            if has_new_commits_since_review:
                # Changes were addressed with new commits, continue processing
                pass
            else:
                # If it's a draft with merge conflicts, allow Copilot to fix them
                if is_draft and mergeable is False:
                    return {'state': STATE_PENDING_REVIEW, 'reason': 'draft_needs_conflict_resolution'}
                return {'state': STATE_CHANGES_REQUESTED, 'reason': 'awaiting_author'}

        if (
            has_current_approval
            and not has_new_commits
            and mergeable is True
        ):
            return {'state': STATE_READY_TO_MERGE, 'reason': 'ready'}

        needs_review = False
        
        # Check for draft PRs with human reviewers (Copilot finished and wants review)
        if is_draft and requested_reviewers:
            human_reviewers = [r for r in requested_reviewers if 'copilot' not in r.lower()]
            if human_reviewers:
                # Draft with human reviewers requested = Copilot done, needs review
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
                if self.verbose:
                    self.logger.error(f"GraphQL mutation errors: {result['errors']}")
                return False, f"GraphQL mutation errors: {result['errors']}"
            assignees = result["data"]["replaceActorsForAssignable"]["assignable"]["assignees"]["nodes"]
            assigned_logins = [assignee["login"] for assignee in assignees]
            return True, None
        except Exception as e:
            if self.verbose:
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
        
        print(f"\nProcessing {len(unprocessed_issues)} unprocessed issues:")
        return unprocessed_issues

    async def process_issue(self, issue, repo_name: str) -> IssueResult:
        """Process a single issue and return an IssueResult."""
        try:
            # Evaluate with DeciderAgent
            result = await self.decider.evaluate_issue({'title': issue.title, 'body': issue.body or ''})
            
            # Check if agent returned an error
            if result.get('decision', '').lower() == 'error':
                print(f"  Issue #{issue.number}: {issue.title[:60]} → Error (evaluation failed)")
                if self.verbose:
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
                                print(f"  Issue #{issue.number}: {issue.title[:60]} → Assigned to Copilot")
                                # Add label only on successful assignment
                                try:
                                    issue.add_to_labels('copilot-candidate')
                                except Exception as e:
                                    if self.verbose:
                                        self.logger.warning(f"Failed to add label to issue #{issue.number}: {e}")
                            else:
                                assign_error = assign_error or "Unknown GraphQL assignment error"
                                print(f"  Issue #{issue.number}: {issue.title[:60]} → Error (assignment failed)")
                                if self.verbose:
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
                            print(f"  Issue #{issue.number}: {issue.title[:60]} → Error (bot lookup failed)")
                            if self.verbose:
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
                        print(f"  Issue #{issue.number}: {issue.title[:60]} → Error (exception during assignment)")
                        if self.verbose:
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
                    print(f"  Issue #{issue.number}: {issue.title[:60]} → Labeled (suitable for Copilot)")
                    # Add label when in just-label mode
                    try:
                        issue.add_to_labels('copilot-candidate')
                    except Exception as e:
                        if self.verbose:
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
                print(f"  Issue #{issue.number}: {issue.title[:60]} → Not suitable for Copilot")
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
                except Exception as e:
                    if self.verbose:
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
            print(f"  Issue #{getattr(issue, 'number', '?')}: {getattr(issue, 'title', 'Unknown')[:60]} → Error (processing exception)")
            if self.verbose:
                self.logger.error(f"Error processing issue #{getattr(issue, 'number', '?')}: {e}")
            return IssueResult(
                repo=repo_name,
                issue_number=getattr(issue, 'number', 0),
                title=getattr(issue, 'title', 'Unknown'),
                url=getattr(issue, 'html_url', ''),
                status='error',
                error_message=str(e)
            )
    def __init__(self, github_token: str, azure_foundry_endpoint: str, just_label: bool = False, use_topic_filter: bool = True, manage_prs: bool = False, verbose: bool = False):
        self.github_token = github_token
        self.azure_foundry_endpoint = azure_foundry_endpoint
        self.github = Github(github_token)
        self.just_label = just_label
        self.use_topic_filter = use_topic_filter
        self.manage_prs = manage_prs
        self.verbose = verbose
        self.logger = self._setup_logger()
        
        # Log masked token for verification
        token_length = len(github_token)
        if token_length > 10:
            masked_token = github_token[:6] + "*" * (token_length - 10) + github_token[-4:]
        elif token_length > 4:
            masked_token = "*" * (token_length - 4) + github_token[-4:]
        else:
            masked_token = "*" * token_length
        self.logger.info(f"[JediMaster] Using GitHub token: {masked_token} (length: {token_length})")
        
        # Get merge retry limit from environment
        self.merge_max_retries = self._get_merge_max_retries()
        # Get max comments limit from environment
        self.max_comments = self._get_max_comments()
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

    def _get_max_comments(self) -> int:
        """Get the maximum number of comments allowed before escalation from environment variable."""
        try:
            max_comments = int(os.getenv('MAX_COMMENTS', '35'))
            if max_comments < 1:
                self.logger.warning(f"MAX_COMMENTS must be >= 1, using default of 35")
                return 35
            return max_comments
        except ValueError:
            self.logger.warning(f"Invalid MAX_COMMENTS value, using default of 35")
            return 35

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

    def _get_copilot_error_retry_count(self, pr) -> int:
        """Get the current Copilot error retry count from PR labels."""
        try:
            labels = [label.name for label in pr.labels]
            for label in labels:
                if label.startswith(COPILOT_ERROR_LABEL_PREFIX):
                    try:
                        return int(label.split('-')[-1])
                    except ValueError:
                        continue
            return 0
        except Exception as e:
            self.logger.error(f"Error getting Copilot error retry count for PR #{pr.number}: {e}")
            return 0

    def _increment_copilot_error_retry_count(self, pr) -> int:
        """Increment the Copilot error retry counter and return the new count."""
        try:
            current_count = self._get_copilot_error_retry_count(pr)
            new_count = current_count + 1
            
            # Remove old retry label if it exists
            if current_count > 0:
                old_label_name = f'{COPILOT_ERROR_LABEL_PREFIX}{current_count}'
                try:
                    pr.remove_from_labels(old_label_name)
                except Exception as e:
                    self.logger.debug(f"Could not remove old label {old_label_name}: {e}")
            
            # Add new retry label
            new_label_name = f'{COPILOT_ERROR_LABEL_PREFIX}{new_count}'
            
            # Create label if it doesn't exist
            try:
                repo = pr.repository if hasattr(pr, 'repository') else pr.base.repo
                try:
                    repo.get_label(new_label_name)
                except:
                    repo.create_label(
                        name=new_label_name,
                        color="ff6b6b",
                        description=f"Copilot encountered errors, retry {new_count}"
                    )
                
                pr.add_to_labels(new_label_name)
                self.logger.info(f"Incremented Copilot error retry count to {new_count} for PR #{pr.number}")
                
            except Exception as e:
                self.logger.error(f"Failed to add Copilot error retry label to PR #{pr.number}: {e}")
            
            return new_count
        except Exception as e:
            self.logger.error(f"Error incrementing Copilot error retry count for PR #{pr.number}: {e}")
            return 1

    def _remove_copilot_error_retry_labels(self, pr) -> None:
        """Remove all Copilot error retry labels from a PR."""
        try:
            label_iterable = pr.get_labels() if hasattr(pr, 'get_labels') else pr.labels
            for label in list(label_iterable):
                name = getattr(label, 'name', '') or ''
                if name.startswith(COPILOT_ERROR_LABEL_PREFIX):
                    try:
                        pr.remove_from_labels(name)
                    except Exception as exc:
                        self.logger.debug(f"Failed to remove Copilot error retry label {name} from PR #{pr.number}: {exc}")
        except Exception as exc:
            self.logger.error(f"Failed to clean Copilot error retry labels for PR #{getattr(pr, 'number', '?')}: {exc}")

    def _count_total_comments(self, pr) -> int:
        """Count the total number of comments, reviews, and review comments on a PR."""
        total_count = 0
        
        try:
            # Count issue comments
            total_count += pr.get_issue_comments().totalCount
        except Exception as exc:
            self.logger.debug(f"Failed to count issue comments for PR #{pr.number}: {exc}")
        
        try:
            # Count review comments
            total_count += pr.get_review_comments().totalCount
        except Exception as exc:
            self.logger.debug(f"Failed to count review comments for PR #{pr.number}: {exc}")
        
        try:
            # Count reviews (not including the body-less ones)
            reviews = list(pr.get_reviews())
            for review in reviews:
                if review.body and review.body.strip():
                    total_count += 1
        except Exception as exc:
            self.logger.debug(f"Failed to count reviews for PR #{pr.number}: {exc}")
        
        return total_count

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
        logger.setLevel(logging.DEBUG if self.verbose else logging.INFO)
        # Prevent propagation to root logger to avoid duplicate messages
        logger.propagate = False
        if not logger.handlers:
            handler = logging.StreamHandler()
            if self.verbose:
                # Verbose: show timestamp, file, line number, and level
                formatter = logging.Formatter('[%(asctime)s - %(pathname)s:%(lineno)d - %(levelname)s] %(message)s')
            else:
                # Non-verbose: only show the message
                formatter = logging.Formatter('%(message)s')
            handler.setFormatter(formatter)
            logger.addHandler(handler)
        else:
            # Update existing handler formatters if verbose setting changed
            for handler in logger.handlers:
                if self.verbose:
                    formatter = logging.Formatter('[%(asctime)s - %(pathname)s:%(lineno)d - %(levelname)s] %(message)s')
                else:
                    formatter = logging.Formatter('%(message)s')
                handler.setFormatter(formatter)
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

    async def run_simplified_workflow(self, repo_name: str, max_copilot_concurrent: int = 10, batch_size: int = 15) -> Dict[str, Any]:
        """
        Simplified workflow that:
        1. Processes PRs and counts active Copilot assignments
        2. Processes issues, assigning only if below max concurrent limit
        
        Args:
            repo_name: Repository name (owner/repo)
            max_copilot_concurrent: Maximum number of PRs Copilot can work on simultaneously (default: 10)
            batch_size: Maximum number of items to process (default: 15)
            
        Returns:
            Dictionary with processing results and metrics
        """
        from datetime import datetime
        start_time = datetime.now()
        
        print(f"\n{'='*80}")
        print(f"Starting workflow for {repo_name}")
        print(f"Max concurrent Copilot assignments: {max_copilot_concurrent}")
        print(f"{'='*80}")
        
        try:
            repo = self.github.get_repo(repo_name)
            
            # Step 1: Process PRs and count active Copilot work
            print(f"\nStep 1/2: Processing pull requests...")
            pr_results = await self.manage_pull_requests(repo_name, batch_size=batch_size)
            
            # Count how many PRs Copilot is actively working on
            active_copilot_count = 0
            for pr in repo.get_pulls(state='open'):
                if self._is_copilot_actively_working(pr.number, repo):
                    active_copilot_count += 1
            
            print(f"\nCopilot actively working on {active_copilot_count}/{max_copilot_concurrent} PRs")
            
            # Step 2: Process issues if we have capacity
            issue_results = []
            issues_assigned = 0
            available_slots = max(0, max_copilot_concurrent - active_copilot_count)
            
            if available_slots > 0:
                print(f"\nStep 2/2: Processing issues (up to {available_slots} assignments available)...")
                
                issues = self.fetch_issues(repo_name, batch_size=batch_size)
                for issue in issues:
                    if issue.pull_request:
                        continue
                    
                    # Stop if we've reached the assignment limit
                    if issues_assigned >= available_slots:
                        print(f"\nReached max assignments ({available_slots}), stopping issue processing")
                        break
                    
                    result = await self.process_issue(issue, repo_name)
                    issue_results.append(result)
                    
                    # Count successful assignments
                    if result.status == 'assigned':
                        issues_assigned += 1
            else:
                print(f"\nStep 2/2: Skipping issue processing")
                print(f"Copilot at capacity ({active_copilot_count}/{max_copilot_concurrent})")
            
            # Calculate duration and metrics
            duration = (datetime.now() - start_time).total_seconds()
            
            report = {
                'repo': repo_name,
                'success': True,
                'duration_seconds': duration,
                'pr_results': pr_results,
                'issue_results': issue_results,
                'metrics': {
                    'prs_processed': len(pr_results),
                    'issues_processed': len(issue_results),
                    'issues_assigned': issues_assigned,
                    'copilot_active_count': active_copilot_count,
                    'copilot_max_concurrent': max_copilot_concurrent,
                    'copilot_available_slots': available_slots,
                }
            }
            
            print(f"\n{'='*80}")
            print(f"Workflow complete:")
            print(f"  - {len(pr_results)} PRs processed")
            print(f"  - {issues_assigned} issues assigned to Copilot")
            print(f"  - Duration: {duration:.1f}s")
            print(f"{'='*80}")
            return report
            
        except Exception as e:
            print(f"\nError in workflow: {e}")
            if self.verbose:
                self.logger.error(f"[SimplifiedWorkflow] Error in workflow: {e}")
                import traceback
                self.logger.error(traceback.format_exc())
            return {
                'repo': repo_name,
                'success': False,
                'error': str(e),
                'duration_seconds': (datetime.now() - start_time).total_seconds()
            }

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
            enable_issue_creation=enable_issue_creation,
            github_token=self.github_token
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
            
            # Track Copilot capacity dynamically during execution
            copilot_capacity_tracker = initial_resources.copilot_available_slots
            
            for workflow in plan.workflows:
                result = await self._execute_workflow(repo_name, workflow, workload, copilot_capacity_tracker)
                workflow_results.append(result)
                
                # Update capacity tracker if we assigned issues (which create PRs)
                if workflow.name == 'process_issues' and result.success:
                    assigned_count = result.items_succeeded
                    copilot_capacity_tracker = max(0, copilot_capacity_tracker - assigned_count)
                    self.logger.info(f"[Orchestrator] Copilot capacity reduced by {assigned_count}, now {copilot_capacity_tracker} slots available")
            
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
    
    async def _execute_workflow(self, repo_name: str, workflow: 'WorkflowStep', workload: 'PrioritizedWorkload', copilot_available_slots: int = None) -> 'WorkflowResult':
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
                # Include both pending_review AND changes_requested PRs (the state machine will decide what to do)
                combined_prs = workload.pending_review_prs + workload.changes_requested_prs
                pr_numbers = combined_prs[:workflow.batch_size]
                self.logger.info(f"Executing review_prs workflow with PR numbers: {pr_numbers} (pending={workload.pending_review_prs}, changes_requested={workload.changes_requested_prs})")
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
                # Limit batch size by Copilot capacity to prevent overload
                effective_batch_size = workflow.batch_size
                if copilot_available_slots is not None:
                    if copilot_available_slots <= 0:
                        self.logger.warning(f"[Orchestrator] Skipping process_issues: No Copilot capacity available")
                        return WorkflowResult(
                            workflow_name=workflow.name,
                            success=True,
                            items_processed=0,
                            items_succeeded=0,
                            items_failed=0,
                            duration_seconds=0,
                            details=[]
                        )
                    effective_batch_size = min(workflow.batch_size, copilot_available_slots)
                    self.logger.info(f"[Orchestrator] Limiting process_issues batch from {workflow.batch_size} to {effective_batch_size} based on Copilot capacity")
                
                issue_numbers = workload.unprocessed_issues[:effective_batch_size]
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
                # Remove all existing labels before adding human escalation label
                existing_labels = list(pr.get_labels())
                for label in existing_labels:
                    pr.remove_from_labels(label.name)
                # Add human escalation label
                pr.add_to_labels(HUMAN_ESCALATION_LABEL)
                # Add comment explaining the situation
                pr.create_comment(
                    f"This PR has exceeded the maximum merge retry limit and needs human review. "
                    f"Please investigate the merge conflicts or other blocking issues."
                )
                self.logger.info(f"Flagged PR #{pr_number} for human review (removed {len(existing_labels)} other labels)")
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
            
            # Count PRs with human review label
            human_review_count = sum(1 for r in results if r.status == 'human_escalated')
            if human_review_count > 0:
                summary_rows.append(("PRs escalated to human review", human_review_count))
            
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
            
            # Count PRs with human review label
            human_review_count = sum(1 for r in results if r.status == 'human_escalated')
            if human_review_count > 0:
                summary_rows.append(("PRs escalated to human review", human_review_count))
            
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
