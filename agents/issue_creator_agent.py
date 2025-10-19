"""
IssueCreatorAgent - Suggests and creates issues based on repository analysis.
"""

import logging
from typing import Dict, Any, List
from dataclasses import dataclass


@dataclass
class IssueCreatorResult:
    """Result of issue creation operation."""
    issues_suggested: int
    issues_created: int
    errors: List[str]


class IssueCreatorAgent:
    """Agent responsible for suggesting and creating new issues."""
    
    def __init__(self, github_client, creator_module, config: Dict[str, Any]):
        """
        Initialize the Issue Creator Agent.
        
        Args:
            github_client: GitHub API client
            creator_module: Module containing issue creation logic
            config: Configuration dictionary
        """
        self.github_client = github_client
        self.creator = creator_module
        self.config = config
        self.logger = logging.getLogger(f"{__name__}.IssueCreatorAgent")
    
    async def suggest_and_create_issues(
        self, 
        owner: str, 
        repo: str,
        dry_run: bool = False
    ) -> IssueCreatorResult:
        """
        Suggest and create new issues for the repository.
        
        Args:
            owner: Repository owner
            repo: Repository name
            dry_run: If True, only suggest issues without creating them
            
        Returns:
            IssueCreatorResult with statistics
        """
        self.logger.info(f"Starting issue creation for {owner}/{repo} (dry_run={dry_run})")
        
        suggested = 0
        created = 0
        errors = []
        
        try:
            # Use creator module to suggest and create issues
            issues = await self.creator.suggest_and_create_issues_async(
                owner, repo, self.config, dry_run
            )
            
            suggested = len(issues)
            
            if not dry_run:
                created = sum(1 for issue in issues if issue.get('created', False))
            
            self.logger.info(
                f"Issue creation complete: {suggested} suggested, {created} created"
            )
            
        except Exception as e:
            error_msg = f"Error during issue creation: {e}"
            self.logger.error(error_msg)
            errors.append(error_msg)
        
        return IssueCreatorResult(suggested, created, errors)
