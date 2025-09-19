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
from azure_ai_foundry_utils import create_azure_ai_foundry_client, get_chat_client, get_embeddings_client


class CreatorAgent:
    """Agent that uses LLM to suggest and open new GitHub issues."""
    def __init__(self, github_token: str, azure_foundry_endpoint: str, azure_foundry_api_key: str = None, repo_full_name: str = None, model: str = None, similarity_threshold: float = 0.9, use_openai_similarity: bool = False):
        self.github_token = github_token
        self.azure_foundry_endpoint = azure_foundry_endpoint
        self.azure_foundry_api_key = azure_foundry_api_key
        self.repo_full_name = repo_full_name
        # Load configuration from environment variables
        self.model = model or os.getenv('AZURE_AI_MODEL', 'model-router')
        self.similarity_threshold = similarity_threshold
        self.use_openai_similarity = use_openai_similarity
        
        # Create Azure AI Foundry client
        self.project_client = create_azure_ai_foundry_client(azure_foundry_endpoint, azure_foundry_api_key)
        self.client = get_chat_client(self.project_client)
        self.embeddings_client = get_embeddings_client(self.project_client)
        self.github = Github(github_token)
        self.logger = logging.getLogger('jedimaster.creator')
        self.system_prompt = (
            "You are an expert AI assistant tasked with analyzing a GitHub repository and suggesting actionable, concrete issues that could be opened to improve the project. "
            "You MUST return exactly the requested number of issues as a JSON array where each element is an object with 'title' and 'body' fields. "
            "Focus on different categories of improvements: bug fixes, new features, code quality improvements, documentation enhancements, testing, and performance optimizations. "
            "Each issue should be specific, actionable, and relevant to the code or documentation. "
            "Do not include duplicate or trivial issues. Make sure each issue is distinct and addresses different aspects of the project. "
            "CRITICAL: Return ONLY a JSON array of objects, not a single object or a dict with nested arrays. "
            "Example format: [{'title': 'Issue 1', 'body': 'Description 1'}, {'title': 'Issue 2', 'body': 'Description 2'}]"
        )

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

    def _get_embeddings(self, texts: List[str]) -> List[List[float]]:
        """Get embeddings for a list of texts using text-embedding-ada-002 model."""
        try:
            response = self.embeddings_client.embeddings.create(
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

    def _check_for_similar_issues(self, suggested_issues: List[Dict[str, str]], existing_issues: List[Dict[str, Any]]) -> tuple[List[Dict[str, str]], List[Dict[str, Any]]]:
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
            embeddings = self._get_embeddings(all_titles)
            
            if not embeddings or len(embeddings) != len(all_titles):
                self.logger.warning("Failed to get embeddings, falling back to local similarity")
                return self._check_for_similar_issues_local(suggested_issues, existing_issues)
            
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
            unique_issues, similar_issues_info = self._check_for_similar_issues_local(suggested_issues, existing_issues)
        
        return unique_issues, similar_issues_info

    def _check_for_similar_issues_local(self, suggested_issues: List[Dict[str, str]], existing_issues: List[Dict[str, Any]]) -> tuple[List[Dict[str, str]], List[Dict[str, Any]]]:
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

    def suggest_issues(self, max_issues: int = 5) -> List[Dict[str, str]]:
        """Call LLM to suggest issues based on repo context. Stores the conversation for inspection."""
        context = self._gather_repo_context()
        user_prompt = (
            f"Given the following repository context, suggest exactly {max_issues} new GitHub issues. "
            f"You MUST return exactly {max_issues} distinct issues as a JSON array. "
            "Each element should be an object with 'title' and 'body' fields. "
            "Focus on different categories: bug fixes, new features, code quality, documentation, testing, and performance. "
            f"Return ONLY the JSON array with {max_issues} issues, no other text.\n\n"
            f"Repository context:\n{context}"
        )
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,  # type: ignore
            temperature=0.3,
            max_tokens=4000,  # Increased to account for reasoning tokens
            response_format={"type": "json_object"}
        )
        result_text = response.choices[0].message.content
        self.last_conversation = {
            "system": self.system_prompt,
            "user": user_prompt,
            "llm_response": result_text
        }
        if not result_text:
            self.logger.error(f"LLM returned empty response. Full response: {response}")
            raise ValueError("LLM returned empty response")
        try:
            # Clean up the response text (remove any markdown code blocks)
            cleaned_response = result_text.strip()
            if cleaned_response.startswith('```json'):
                cleaned_response = cleaned_response[7:]
            if cleaned_response.endswith('```'):
                cleaned_response = cleaned_response[:-3]
            cleaned_response = cleaned_response.strip()
            
            issues = json.loads(cleaned_response)
            
            self.logger.info(f"LLM response type: {type(issues)}")
            self.logger.info(f"LLM response content: {issues}")
            
            # Expect a JSON array of issues
            if isinstance(issues, list):
                return issues[:max_issues]
            elif isinstance(issues, dict):
                # Handle wrapper objects like {"issues": [...]} or {"suggestions": [...]}
                for key in ['issues', 'suggestions', 'items']:
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
            
            # If we get here, the format is unexpected
            self.logger.error(f"Unexpected LLM response format: {type(issues)}")
            self.logger.error(f"Response keys: {list(issues.keys()) if isinstance(issues, dict) else 'Not a dict'}")
            if isinstance(issues, dict) and len(issues) < 10:  # Only log if not too verbose
                self.logger.error(f"Full response content: {issues}")
            return []
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse LLM response as JSON: {e}")
            return []
        except Exception as e:
            self.logger.error(f"Failed to parse LLM response: {e}")
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

    def create_issues(self, max_issues: int = 5) -> List[Dict[str, Any]]:
        """Suggest and open new issues in the repo, checking for duplicates."""
        # Get existing issues first
        existing_issues = self._get_existing_issues()
        
        # Suggest new issues
        suggested_issues = self.suggest_issues(max_issues=max_issues)
        if not suggested_issues:
            self.logger.warning("No issues suggested by LLM.")
            return []
        
        # Check for similar issues
        unique_issues, similar_issues_info = self._check_for_similar_issues(suggested_issues, existing_issues)
        
        # Print comparison information
        print(f"Checked {len(suggested_issues)} suggested issues against {len(existing_issues)} existing open issues")
        
        # Print similarity information
        if similar_issues_info:
            print(f"\nSkipping {len(similar_issues_info)} similar issue(s):")
            for info in similar_issues_info:
                print(f"  - '{info['suggested_title']}' is too similar to existing issue #{info['existing_number']}: '{info['existing_title']}' (similarity: {info['similarity_score']:.3f})")
                print(f"    Existing issue: {info['existing_url']} [{info['existing_state']}]")
        
        if not unique_issues:
            print("\nAll suggested issues are too similar to existing ones. No new issues will be created.")
            return []
        
        if len(unique_issues) < len(suggested_issues):
            print(f"\nCreating {len(unique_issues)} unique issue(s) out of {len(suggested_issues)} suggested:")
        
        # Create the unique issues
        return self.open_issues(unique_issues)
