"""
GraphQL client for GitHub API operations.
Provides GraphQL-based alternatives to REST API operations used in JediMaster.
"""

import json
import logging
from typing import Dict, Any, List, Optional
import requests


class GitHubGraphQLClient:
    """GraphQL client for GitHub API operations."""
    
    def __init__(self, token: str):
        """Initialize the GraphQL client with GitHub token."""
        self.token = token
        self.endpoint = "https://api.github.com/graphql"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self.logger = logging.getLogger('jedimaster.graphql')
    
    def execute_query(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Execute a GraphQL query against GitHub API."""
        payload = {
            "query": query,
            "variables": variables or {}
        }
        
        try:
            response = requests.post(
                self.endpoint,
                headers=self.headers,
                json=payload,
                timeout=30
            )
            response.raise_for_status()
            
            result = response.json()
            
            if "errors" in result:
                error_msg = "; ".join([error.get("message", "Unknown error") for error in result["errors"]])
                raise Exception(f"GraphQL errors: {error_msg}")
            
            return result.get("data", {})
            
        except requests.exceptions.RequestException as e:
            self.logger.error(f"HTTP error in GraphQL request: {e}")
            raise
        except Exception as e:
            self.logger.error(f"Error executing GraphQL query: {e}")
            raise
    
    def get_repository_info(self, owner: str, name: str) -> Dict[str, Any]:
        """Get repository information including node ID."""
        query = """
        query GetRepository($owner: String!, $name: String!) {
            repository(owner: $owner, name: $name) {
                id
                name
                owner {
                    login
                }
                url
            }
        }
        """
        
        variables = {"owner": owner, "name": name}
        result = self.execute_query(query, variables)
        return result.get("repository", {})
    
    def get_issues(self, owner: str, name: str, first: int = 100) -> List[Dict[str, Any]]:
        """Fetch open issues from a repository using GraphQL."""
        query = """
        query GetIssues($owner: String!, $name: String!, $first: Int!) {
            repository(owner: $owner, name: $name) {
                issues(first: $first, states: OPEN, orderBy: {field: CREATED_AT, direction: DESC}) {
                    nodes {
                        id
                        number
                        title
                        body
                        url
                        labels(first: 20) {
                            nodes {
                                name
                                color
                                description
                            }
                        }
                        assignees(first: 10) {
                            nodes {
                                login
                                id
                            }
                        }
                        comments(last: 10) {
                            nodes {
                                body
                                author {
                                    login
                                }
                                createdAt
                            }
                        }
                    }
                    pageInfo {
                        hasNextPage
                        endCursor
                    }
                }
            }
        }
        """
        
        variables = {"owner": owner, "name": name, "first": first}
        result = self.execute_query(query, variables)
        repository = result.get("repository", {})
        issues = repository.get("issues", {})
        return issues.get("nodes", [])
    
    def add_assignees_to_issue(self, issue_id: str, assignee_ids: List[str]) -> bool:
        """Add assignees to an issue using GraphQL mutation."""
        mutation = """
        mutation AddAssigneesToAssignable($assignableId: ID!, $assigneeIds: [ID!]!) {
            addAssigneesToAssignable(input: {assignableId: $assignableId, assigneeIds: $assigneeIds}) {
                assignable {
                    assignees(first: 10) {
                        nodes {
                            login
                        }
                    }
                }
                clientMutationId
            }
        }
        """
        
        variables = {
            "assignableId": issue_id,
            "assigneeIds": assignee_ids
        }
        
        try:
            result = self.execute_query(mutation, variables)
            return True
        except Exception as e:
            self.logger.warning(f"Failed to add assignees to issue {issue_id}: {e}")
            return False
    
    def add_labels_to_issue(self, issue_id: str, label_ids: List[str]) -> bool:
        """Add labels to an issue using GraphQL mutation."""
        mutation = """
        mutation AddLabelsToLabelable($labelableId: ID!, $labelIds: [ID!]!) {
            addLabelsToLabelable(input: {labelableId: $labelableId, labelIds: $labelIds}) {
                labelable {
                    labels(first: 20) {
                        nodes {
                            name
                        }
                    }
                }
                clientMutationId
            }
        }
        """
        
        variables = {
            "labelableId": issue_id,
            "labelIds": label_ids
        }
        
        try:
            result = self.execute_query(mutation, variables)
            return True
        except Exception as e:
            self.logger.warning(f"Failed to add labels to issue {issue_id}: {e}")
            return False
    
    def create_label(self, repo_id: str, name: str, color: str, description: str = "") -> Optional[str]:
        """Create a new label in repository using GraphQL mutation."""
        mutation = """
        mutation CreateLabel($repositoryId: ID!, $name: String!, $color: String!, $description: String) {
            createLabel(input: {repositoryId: $repositoryId, name: $name, color: $color, description: $description}) {
                label {
                    id
                    name
                    color
                    description
                }
                clientMutationId
            }
        }
        """
        
        variables = {
            "repositoryId": repo_id,
            "name": name,
            "color": color,
            "description": description
        }
        
        try:
            result = self.execute_query(mutation, variables)
            label = result.get("createLabel", {}).get("label", {})
            return label.get("id")
        except Exception as e:
            self.logger.warning(f"Failed to create label {name}: {e}")
            return None
    
    def get_repository_labels(self, owner: str, name: str) -> List[Dict[str, Any]]:
        """Get all labels from a repository."""
        query = """
        query GetLabels($owner: String!, $name: String!) {
            repository(owner: $owner, name: $name) {
                labels(first: 100) {
                    nodes {
                        id
                        name
                        color
                        description
                    }
                }
            }
        }
        """
        
        variables = {"owner": owner, "name": name}
        result = self.execute_query(query, variables)
        repository = result.get("repository", {})
        labels = repository.get("labels", {})
        return labels.get("nodes", [])
    
    def add_comment_to_issue(self, issue_id: str, body: str) -> bool:
        """Add a comment to an issue using GraphQL mutation."""
        mutation = """
        mutation AddComment($subjectId: ID!, $body: String!) {
            addComment(input: {subjectId: $subjectId, body: $body}) {
                commentEdge {
                    node {
                        id
                        body
                    }
                }
                clientMutationId
            }
        }
        """
        
        variables = {
            "subjectId": issue_id,
            "body": body
        }
        
        try:
            result = self.execute_query(mutation, variables)
            return True
        except Exception as e:
            self.logger.warning(f"Failed to add comment to issue {issue_id}: {e}")
            return False
    
    def get_user_id(self, login: str) -> Optional[str]:
        """Get user ID by login name."""
        query = """
        query GetUser($login: String!) {
            user(login: $login) {
                id
                login
            }
        }
        """
        
        variables = {"login": login}
        try:
            result = self.execute_query(query, variables)
            user = result.get("user", {})
            return user.get("id")
        except Exception as e:
            self.logger.warning(f"Failed to get user ID for {login}: {e}")
            return None