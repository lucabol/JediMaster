"""
CreatorAgent - Uses LLM to suggest and open new GitHub issues based on repository context.
"""

import os
import logging
import json
import numpy as np
import re
from typing import List, Dict, Any, Optional, Set
from github import Github
from agent_framework.azure import AzureAIAgentClient
from azure.identity.aio import DefaultAzureCredential
from reporting import format_table


class CreatorAgent:
    """Agent that uses LLM to suggest and open new GitHub issues."""
    
    def __init__(self, github_token: str, azure_foundry_endpoint: str, azure_foundry_api_key: str = None, repo_full_name: str = None, model: str = None, similarity_threshold: float = 0.9, use_openai_similarity: bool = False):
        self.github_token = github_token
        self.azure_foundry_endpoint = azure_foundry_endpoint
        self.repo_full_name = repo_full_name
        # Load configuration from environment variables
        self.model = model or os.getenv('AZURE_AI_MODEL', 'model-router')
        self.similarity_threshold = similarity_threshold
        self.use_openai_similarity = use_openai_similarity
        
        self._credential: Optional[DefaultAzureCredential] = None
        self._client: Optional[AzureAIAgentClient] = None
        self.github = Github(github_token)
        self.logger = logging.getLogger('jedimaster.creator')
        
        self.system_prompt = (
            "You are an expert AI assistant tasked with analyzing a GitHub repository and suggesting actionable, concrete issues that could be opened to improve the project. "
            "You MUST return exactly the requested number of issues as a JSON object with an 'issues' key containing an array of issue objects. "
            "Focus on different categories of improvements: bug fixes, new features, code quality improvements, documentation enhancements, testing, and performance optimizations. "
            "Each issue should be specific, actionable, and relevant to the code or documentation. "
            "Do not include duplicate or trivial issues. Make sure each issue is distinct and addresses different aspects of the project. "
            "CRITICAL: Return ONLY a JSON object with an 'issues' key containing an array of objects with 'title' and 'body' fields. "
            "Example format: {'issues': [{'title': 'Issue 1', 'body': 'Description 1'}, {'title': 'Issue 2', 'body': 'Description 2'}]}"
        )

    async def __aenter__(self):
        """Async context manager entry."""
        self._credential = DefaultAzureCredential()
        self._client = AzureAIAgentClient(async_credential=self._credential)
        # Don't need to enter the client - it's not a context manager itself
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        # Close credential properly
        if self._credential:
            await self._credential.__aexit__(exc_type, exc_val, exc_tb)

    async def _run_agent(self, agent_name: str, prompt: str) -> str:
        """
        Helper method to create and run an agent with the system prompt.
        
        Args:
            agent_name: Name for the agent instance
            prompt: User prompt to send to the agent
            
        Returns:
            Raw text response from the agent
            
        Raises:
            ValueError: If agent returns empty response
        """
        async with self._client.create_agent(
            name=agent_name,
            instructions=self.system_prompt,
            model=self.model
        ) as agent:
            result = await agent.run(prompt)
            result_text = result.text
            
            if not result_text:
                self.logger.error(f"Agent returned empty response. Full response: {result}")
                raise ValueError("Agent returned empty response")
            
            self.logger.debug(f"Agent raw response: {result_text}")
            return result_text

    def _shorten(self, text: Optional[str], limit: int = 80) -> str:
        if not text:
            return ""
        cleaned = " ".join(text.split())
        if len(cleaned) <= limit:
            return cleaned
        return cleaned[: limit - 1] + "…"

    def _gather_repo_context(self, max_chars: int = 12000) -> str:
        """Gather as much repo context as possible (README, then all other files recursively) within max_chars."""
        repo = self.github.get_repo(self.repo_full_name)
        context_parts = []
        total_chars = 0
        # Add README first if available
        try:
            readme = repo.get_readme()
            readme_content = readme.decoded_content.decode('utf-8')
            context_parts.append(f"# README.md\n{readme_content[:max_chars]}")
            total_chars += min(len(readme_content), max_chars)
        except Exception:
            pass

        def gather_files(path=""):
            files = []
            try:
                contents = repo.get_contents(path)
                if isinstance(contents, list):
                    for item in contents:
                        # Exclude README.md (case-insensitive)
                        if item.type == 'file' and item.name.lower() != 'readme.md':
                            files.append(item)
                        elif item.type == 'dir':
                            files.extend(gather_files(item.path))
                else:
                    # It's a file, not a directory
                    if contents.type == 'file' and contents.name.lower() != 'readme.md':
                        files.append(contents)
            except Exception:
                pass
            return files

        all_files = gather_files("")
        for f in all_files:
            if total_chars >= max_chars:
                break
            try:
                content = f.decoded_content.decode('utf-8')
                allowed = max_chars - total_chars
                snippet = content[:allowed]
                context_parts.append(f"# {f.path}\n{snippet}")
                total_chars += len(snippet)
            except Exception:
                continue

        context = "\n\n".join(context_parts)
        return context[:max_chars]

    def _get_existing_issues(self) -> List[Dict[str, Any]]:
        """Fetch existing open issues from the repository."""
        try:
            repo = self.github.get_repo(self.repo_full_name)
            
            # Get only open issues
            open_issues = list(repo.get_issues(state='open'))
            
            existing_issues = []
            for issue in open_issues:
                # Skip pull requests (they also show up in issues)
                if issue.pull_request:
                    continue
                    
                existing_issues.append({
                    'number': issue.number,
                    'title': issue.title,
                    'state': issue.state,
                    'url': issue.html_url
                })
            
            self.logger.info(f"Found {len(existing_issues)} open issues in {self.repo_full_name}")
            return existing_issues
            
        except Exception as e:
            self.logger.error(f"Failed to fetch existing issues: {e}")
            return []

    def _normalize_title(self, title: str) -> Set[str]:
        """Normalize title to a set of meaningful words for local similarity comparison."""
        # Convert to lowercase and remove punctuation
        normalized = re.sub(r'[^\w\s]', ' ', title.lower())
        
        # Split into words and filter out stop words and short words
        stop_words = {
            'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by',
            'from', 'up', 'about', 'into', 'through', 'during', 'before', 'after', 'above', 'below',
            'between', 'among', 'is', 'are', 'was', 'were', 'be', 'been', 'being', 'have', 'has', 'had',
            'do', 'does', 'did', 'will', 'would', 'could', 'should', 'may', 'might', 'must', 'can',
            'fix', 'add', 'update', 'improve', 'enhance', 'implement', 'create', 'remove', 'delete',
            'issue', 'bug', 'feature', 'support', 'help', 'need', 'make', 'change', 'modify'
        }
        
        words = set()
        for word in normalized.split():
            # Keep words that are 3+ characters and not stop words
            if len(word) >= 3 and word not in stop_words:
                words.add(word)
        
        return words

    def _calculate_local_similarity(self, title1: str, title2: str) -> float:
        """Calculate local similarity using Jaccard similarity of normalized word sets."""
        words1 = self._normalize_title(title1)
        words2 = self._normalize_title(title2)
        
        if not words1 and not words2:
            return 1.0  # Both empty
        if not words1 or not words2:
            return 0.0  # One empty
        
        # Jaccard similarity: intersection over union
        intersection = len(words1.intersection(words2))
        union = len(words1.union(words2))
        
        return intersection / union if union > 0 else 0.0


    async def _get_embeddings(self, texts: List[str]) -> List[List[float]]:
        """Get embeddings for a list of texts using Azure OpenAI (not through Agent Framework)."""
        try:
            # Import Azure OpenAI client for embeddings only
            from openai import AsyncAzureOpenAI
            from azure.identity import get_bearer_token_provider
            
            # Parse endpoint to get base URL
            import urllib.parse
            parsed = urllib.parse.urlparse(self.azure_foundry_endpoint)
            base_endpoint = f"{parsed.scheme}://{parsed.netloc}"
            query_params = urllib.parse.parse_qs(parsed.query)
            api_version = query_params.get('api-version', ['2024-12-01-preview'])[0]
            
            # Create token provider
            token_provider = get_bearer_token_provider(
                self._credential, 
                "https://cognitiveservices.azure.com/.default"
            )
            
            # Create async client for embeddings
            async with AsyncAzureOpenAI(
                azure_endpoint=base_endpoint,
                azure_ad_token_provider=token_provider,
                api_version=api_version
            ) as embeddings_client:
                response = await embeddings_client.embeddings.create(
                    model="text-embedding-ada-002",
                    input=texts
                )
                return [data.embedding for data in response.data]
        except Exception as e:
            self.logger.error(f"Failed to get embeddings: {e}")
            return []

    def _calculate_similarity(self, embedding1: List[float], embedding2: List[float]) -> float:
        """Calculate cosine similarity between two embeddings."""
        try:
            # Convert to numpy arrays for easier calculation
            vec1 = np.array(embedding1)
            vec2 = np.array(embedding2)
            
            # Calculate cosine similarity
            dot_product = np.dot(vec1, vec2)
            magnitude1 = np.linalg.norm(vec1)
            magnitude2 = np.linalg.norm(vec2)
            
            if magnitude1 == 0 or magnitude2 == 0:
                return 0.0
            
            similarity = dot_product / (magnitude1 * magnitude2)
            return float(similarity)
        except Exception as e:
            self.logger.error(f"Failed to calculate similarity: {e}")
            return 0.0

    async def _check_for_similar_issues(self, suggested_issues: List[Dict[str, str]], existing_issues: List[Dict[str, Any]]) -> tuple[List[Dict[str, str]], List[Dict[str, Any]]]:
        """
        Check suggested issues against existing issues for similarity.
        Returns (unique_issues, similar_issues_info)
        """
        if not existing_issues or not suggested_issues:
            return suggested_issues, []

        unique_issues = []
        similar_issues_info = []
        
        if self.use_openai_similarity:
            # Use OpenAI embeddings for semantic similarity (slower but more accurate)
            self.logger.info(f"Using OpenAI embeddings for similarity detection against {len(existing_issues)} open issues")
            suggested_titles = [issue['title'] for issue in suggested_issues]
            existing_titles = [issue['title'] for issue in existing_issues]
            
            # Get embeddings for all titles
            all_titles = suggested_titles + existing_titles
            embeddings = await self._get_embeddings(all_titles)
            
            if not embeddings or len(embeddings) != len(all_titles):
                self.logger.warning("Failed to get embeddings, falling back to local similarity")
                return await self._check_for_similar_issues_local(suggested_issues, existing_issues)
            
            # Split embeddings back
            suggested_embeddings = embeddings[:len(suggested_titles)]
            existing_embeddings = embeddings[len(suggested_titles):]
            
            for i, suggested_issue in enumerate(suggested_issues):
                suggested_embedding = suggested_embeddings[i]
                is_similar = False
                most_similar_issue = None
                highest_similarity = 0.0
                
                # Check against all existing issues
                for j, existing_issue in enumerate(existing_issues):
                    existing_embedding = existing_embeddings[j]
                    similarity = self._calculate_similarity(suggested_embedding, existing_embedding)
                    
                    if similarity > highest_similarity:
                        highest_similarity = similarity
                        most_similar_issue = existing_issue
                    
                    if similarity >= self.similarity_threshold:
                        is_similar = True
                        break
                
                if is_similar and most_similar_issue:
                    similar_issues_info.append({
                        'suggested_title': suggested_issue['title'],
                        'existing_title': most_similar_issue['title'],
                        'existing_number': most_similar_issue['number'],
                        'existing_state': most_similar_issue['state'],
                        'existing_url': most_similar_issue['url'],
                        'similarity_score': highest_similarity
                    })
                    self.logger.info(f"Skipping similar issue: '{suggested_issue['title']}' (similarity: {highest_similarity:.3f} with #{most_similar_issue['number']})")
                else:
                    unique_issues.append(suggested_issue)
        else:
            # Use local similarity (faster)
            self.logger.info(f"Using local word-based similarity detection against {len(existing_issues)} open issues")
            unique_issues, similar_issues_info = await self._check_for_similar_issues_local(suggested_issues, existing_issues)
        
        return unique_issues, similar_issues_info

    async def _check_for_similar_issues_local(self, suggested_issues: List[Dict[str, str]], existing_issues: List[Dict[str, Any]]) -> tuple[List[Dict[str, str]], List[Dict[str, Any]]]:
        """Local similarity check using word overlap."""
        unique_issues = []
        similar_issues_info = []
        
        # Use 0.5 as threshold for local similarity (word overlap is different from semantic similarity)
        local_threshold = 0.5
        self.logger.debug(f"Comparing {len(suggested_issues)} suggestions against {len(existing_issues)} open issues with local threshold {local_threshold}")
        
        for suggested_issue in suggested_issues:
            is_similar = False
            most_similar_issue = None
            highest_similarity = 0.0
            
            # Check against all existing issues
            for existing_issue in existing_issues:
                similarity = self._calculate_local_similarity(suggested_issue['title'], existing_issue['title'])
                
                if similarity > highest_similarity:
                    highest_similarity = similarity
                    most_similar_issue = existing_issue
                
                if similarity >= local_threshold:
                    is_similar = True
                    break
            
            if is_similar and most_similar_issue:
                similar_issues_info.append({
                    'suggested_title': suggested_issue['title'],
                    'existing_title': most_similar_issue['title'],
                    'existing_number': most_similar_issue['number'],
                    'existing_state': most_similar_issue['state'],
                    'existing_url': most_similar_issue['url'],
                    'similarity_score': highest_similarity
                })
                self.logger.info(f"Skipping similar issue: '{suggested_issue['title']}' (local similarity: {highest_similarity:.3f} with #{most_similar_issue['number']})")
            else:
                unique_issues.append(suggested_issue)
        
        return unique_issues, similar_issues_info

    async def suggest_issues(self, max_issues: int = 5) -> List[Dict[str, str]]:
        """Call agent to suggest issues based on repo context. Stores the conversation for inspection."""
        context = self._gather_repo_context()
        user_prompt = (
            f"Given the following repository context, suggest exactly {max_issues} new GitHub issues. "
            f"You MUST return exactly {max_issues} distinct issues as a JSON object with an 'issues' key containing an array of {max_issues} issue objects. "
            "Each element should be an object with 'title' and 'body' fields. "
            "Focus on different categories: bug fixes, new features, code quality, documentation, testing, and performance. "
            f"Return ONLY the JSON object with an 'issues' key containing {max_issues} issues, no other text.\n\n"
            f"Repository context:\n{context}"
        )
        
        try:
            # Use helper method to run agent
            result_text = await self._run_agent("IssueCreatorAgent", user_prompt)
            
            self.last_conversation = {
                "system": self.system_prompt,
                "user": user_prompt,
                "llm_response": result_text
            }
            
            try:
                # Clean up the response text (remove any markdown code blocks)
                cleaned_response = result_text.strip()
                if cleaned_response.startswith('```json'):
                    cleaned_response = cleaned_response[7:]
                if cleaned_response.endswith('```'):
                    cleaned_response = cleaned_response[:-3]
                cleaned_response = cleaned_response.strip()
                
                issues = json.loads(cleaned_response)
                
                self.logger.info(f"Agent response type: {type(issues)}")
                self.logger.info(f"Agent response content: {issues}")
                
                # First check if it's a dict with an 'issues' key (our expected format)
                if isinstance(issues, dict) and 'issues' in issues and isinstance(issues['issues'], list):
                    self.logger.info(f"Found issues in 'issues' key: {len(issues['issues'])} items")
                    return issues['issues'][:max_issues]
                
                # Fallback: Expect a JSON array of issues (legacy format)
                elif isinstance(issues, list):
                    return issues[:max_issues]
                elif isinstance(issues, dict):
                    # Handle other wrapper objects like {"suggestions": [...]} or {"items": [...]}
                    for key in ['suggestions', 'items']:
                        if key in issues and isinstance(issues[key], list):
                            self.logger.info(f"Found issues in key '{key}': {len(issues[key])} items")
                            return issues[key][:max_issues]
                    
                    # Handle dict with numeric string keys (e.g., {"0": {...}, "1": {...}})
                    numeric_keys = [k for k in issues.keys() if k.isdigit()]
                    if numeric_keys:
                        self.logger.info(f"Found numeric keys: {numeric_keys}")
                        # Convert to list by sorting the numeric keys
                        sorted_keys = sorted(numeric_keys, key=int)
                        numeric_issues = [issues[k] for k in sorted_keys]
                        return numeric_issues[:max_issues]
                    
                    # Handle single issue dict with 'title' and 'body'
                    if 'title' in issues and 'body' in issues:
                        return [issues]
                    
                    # Check if it's an error response
                    if 'error' in issues:
                        self.logger.error(f"Agent returned error: {issues['error']}")
                        return []
                
                # If we get here, the format is unexpected
                self.logger.error(f"Unexpected agent response format: {type(issues)}")
                self.logger.error(f"Response keys: {list(issues.keys()) if isinstance(issues, dict) else 'Not a dict'}")
                if isinstance(issues, dict) and len(issues) < 10:  # Only log if not too verbose
                    self.logger.error(f"Full response content: {issues}")
                return []
            except json.JSONDecodeError as e:
                self.logger.error(f"Failed to parse agent response as JSON: {e}")
                return []
            except Exception as e:
                self.logger.error(f"Failed to parse agent response: {e}")
                return []
        except Exception as e:
            self.logger.error(f"Error in suggest_issues: {e}")
            return []

    def open_issues(self, issues: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        """Open the suggested issues in the GitHub repo."""
        repo = self.github.get_repo(self.repo_full_name)
        results = []
        for issue in issues:
            try:
                created = repo.create_issue(title=issue['title'], body=issue.get('body', ''))
                self.logger.info(f"Created issue: {created.html_url}")
                results.append({
                    'title': issue['title'],
                    'url': created.html_url,
                    'status': 'created'
                })
            except Exception as e:
                self.logger.error(f"Failed to create issue '{issue['title']}': {e}")
                results.append({
                    'title': issue['title'],
                    'status': 'error',
                    'error': str(e)
                })
        return results

    async def create_issues(self, max_issues: int = 5) -> List[Dict[str, Any]]:
        """Suggest and open new issues in the repo, checking for duplicates."""
        # Get existing issues first
        existing_issues = self._get_existing_issues()
        
        # Suggest new issues
        suggested_issues = await self.suggest_issues(max_issues=max_issues)
        if not suggested_issues:
            self.logger.warning("No issues suggested by agent.")
            print("\nISSUE CREATION SUMMARY")
            summary_rows = [
                ("Suggested", 0),
                ("Existing open", len(existing_issues)),
                ("Skipped (similar)", 0),
                ("To create", 0),
            ]
            print(format_table(["Metric", "Count"], summary_rows))
            print()
            print(
                format_table(
                    ["Title", "Status", "Details"],
                    [],
                    empty_message="No issues created",
                )
            )
            return []
        
        # Check for similar issues
        unique_issues, similar_issues_info = await self._check_for_similar_issues(suggested_issues, existing_issues)
        
        summary_rows = [
            ("Suggested", len(suggested_issues)),
            ("Existing open", len(existing_issues)),
            ("Skipped (similar)", len(similar_issues_info)),
            ("To create", len(unique_issues)),
        ]
        print("\nISSUE CREATION SUMMARY")
        print(format_table(["Metric", "Count"], summary_rows))

        similar_rows = [
            [
                self._shorten(info['suggested_title'], 50),
                f"#{info['existing_number']}",
                f"{info['similarity_score']:.2f}",
                self._shorten(info['existing_title'], 40),
            ]
            for info in similar_issues_info
        ]
        print()
        print(
            format_table(
                ["Suggested", "Existing", "Similarity", "Existing Title"],
                similar_rows,
                empty_message="No similar issues detected",
            )
        )

        if not unique_issues:
            print()
            print(
                format_table(
                    ["Title", "Status", "Details"],
                    [],
                    empty_message="No issues created",
                )
            )
            return []

        creation_results = self.open_issues(unique_issues)

        status_map = {
            'created': 'created ✅',
            'error': 'error ⚠️',
        }
        detail_rows = [
            [
                self._shorten(item['title'], 90),
                status_map.get(item.get('status', ''), item.get('status', 'unknown')), 
                self._shorten(item.get('url') or item.get('error') or ''),
            ]
            for item in creation_results
        ]

        print()
        print(
            format_table(
                ["Title", "Status", "Details"],
                detail_rows,
                empty_message="No issues created",
            )
        )

        return creation_results
