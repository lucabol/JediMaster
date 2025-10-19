"""
IssueTriageAgent - Evaluates and triages issues for Copilot assignment.
"""

import logging
from typing import Dict, Any, List
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class IssueTriageResult:
    """Result of issue triage operation."""
    issues_processed: int
    issues_assigned: int
    issues_labeled: int
    errors: List[str]


class IssueTriageAgent:
    """Agent responsible for triaging issues and assigning them to Copilot."""
    
    def __init__(self, github_client, decider_module, config: Dict[str, Any]):
        """
        Initialize the Issue Triage Agent.
        
        Args:
            github_client: GitHub API client
            decider_module: Module containing decision logic
            config: Configuration dictionary
        """
        self.github_client = github_client
        self.decider = decider_module
        self.config = config
        self.logger = logging.getLogger(f"{__name__}.IssueTriageAgent")
    
    async def triage_issues(self, owner: str, repo: str, batch_size: int = 15) -> IssueTriageResult:
        """
        Triage unprocessed issues in the repository.
        
        Args:
            owner: Repository owner
            repo: Repository name
            batch_size: Maximum number of issues to process in one batch
            
        Returns:
            IssueTriageResult with statistics
        """
        from jedimaster import (
            get_unprocessed_issues,
            assign_issue_to_copilot,
            apply_labels_to_issue,
        )
        
        self.logger.info(f"Starting issue triage for {owner}/{repo}")
        
        processed = 0
        assigned = 0
        labeled = 0
        errors = []
        
        try:
            # Get unprocessed issues
            issues = await get_unprocessed_issues(
                owner, repo, self.github_client, batch_size
            )
            
            if not issues:
                self.logger.info("No unprocessed issues found")
                return IssueTriageResult(0, 0, 0, [])
            
            self.logger.info(f"Found {len(issues)} unprocessed issues (batch size: {batch_size})")
            
            # Process each issue
            for issue in issues:
                issue_number = issue.number
                
                try:
                    # Evaluate issue using decider
                    should_assign, labels = await self.decider.evaluate_issue_async(
                        owner, repo, issue_number, self.config
                    )
                    
                    processed += 1
                    
                    # Assign if recommended
                    if should_assign:
                        success = await assign_issue_to_copilot(
                            owner, repo, issue_number, self.github_client
                        )
                        if success:
                            assigned += 1
                            self.logger.info(f"Assigned issue #{issue_number} to Copilot")
                        else:
                            errors.append(f"Failed to assign issue #{issue_number}")
                    
                    # Apply labels
                    if labels:
                        success = await apply_labels_to_issue(
                            owner, repo, issue_number, labels, self.github_client
                        )
                        if success:
                            labeled += 1
                            self.logger.info(f"Applied labels to issue #{issue_number}: {labels}")
                        else:
                            errors.append(f"Failed to apply labels to issue #{issue_number}")
                    
                except Exception as e:
                    error_msg = f"Error processing issue #{issue_number}: {e}"
                    self.logger.error(error_msg)
                    errors.append(error_msg)
            
            self.logger.info(
                f"Triage complete: {processed} processed, {assigned} assigned, "
                f"{labeled} labeled, {len(errors)} errors"
            )
            
        except Exception as e:
            error_msg = f"Error during issue triage: {e}"
            self.logger.error(error_msg)
            errors.append(error_msg)
        
        return IssueTriageResult(processed, assigned, labeled, errors)
