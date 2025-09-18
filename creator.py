"""
CreatorAgent - Uses LLM to suggest and open new GitHub issues based on repository context.
"""

import os
import logging
import json
import numpy as np
from typing import List, Dict, Any, Optional
from openai import OpenAI
from github import Github

class CreatorAgent:
    """Agent that uses LLM to suggest and open new GitHub issues."""
    def __init__(self, github_token: str, openai_api_key: str, repo_full_name: str, model: str = "gpt-3.5-turbo", similarity_threshold: float = 0.9):
        self.github_token = github_token
        self.openai_api_key = openai_api_key
        self.repo_full_name = repo_full_name
        self.model = model
        self.similarity_threshold = similarity_threshold
        self.client = OpenAI(api_key=openai_api_key)
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
        """Fetch all existing issues (open and closed) from the repository."""
        try:
            repo = self.github.get_repo(self.repo_full_name)
            
            # Get both open and closed issues
            open_issues = list(repo.get_issues(state='open'))
            closed_issues = list(repo.get_issues(state='closed'))
            
            all_issues = open_issues + closed_issues
            
            existing_issues = []
            for issue in all_issues:
                # Skip pull requests (they also show up in issues)
                if issue.pull_request:
                    continue
                    
                existing_issues.append({
                    'number': issue.number,
                    'title': issue.title,
                    'state': issue.state,
                    'url': issue.html_url
                })
            
            self.logger.info(f"Found {len(existing_issues)} existing issues in {self.repo_full_name}")
            return existing_issues
            
        except Exception as e:
            self.logger.error(f"Failed to fetch existing issues: {e}")
            return []

    def _get_embeddings(self, texts: List[str]) -> List[List[float]]:
        """Get embeddings for a list of texts using OpenAI's text-embedding-ada-002 model."""
        try:
            response = self.client.embeddings.create(
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

        # Get titles for embedding
        suggested_titles = [issue['title'] for issue in suggested_issues]
        existing_titles = [issue['title'] for issue in existing_issues]
        
        # Get embeddings for all titles
        all_titles = suggested_titles + existing_titles
        embeddings = self._get_embeddings(all_titles)
        
        if not embeddings or len(embeddings) != len(all_titles):
            self.logger.warning("Failed to get embeddings, skipping similarity check")
            return suggested_issues, []
        
        # Split embeddings back
        suggested_embeddings = embeddings[:len(suggested_titles)]
        existing_embeddings = embeddings[len(suggested_titles):]
        
        unique_issues = []
        similar_issues_info = []
        
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
            max_tokens=1000
        )
        result_text = response.choices[0].message.content
        self.last_conversation = {
            "system": self.system_prompt,
            "user": user_prompt,
            "llm_response": result_text
        }
        if not result_text:
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
            
            # Expect a JSON array of issues
            if isinstance(issues, list):
                return issues[:max_issues]
            elif isinstance(issues, dict):
                # Handle wrapper objects like {"issues": [...]} or {"suggestions": [...]}
                for key in ['issues', 'suggestions', 'items']:
                    if key in issues and isinstance(issues[key], list):
                        return issues[key][:max_issues]
                # Handle single issue dict with 'title' and 'body'
                if 'title' in issues and 'body' in issues:
                    return [issues]
            
            # If we get here, the format is unexpected
            self.logger.error(f"Unexpected LLM response format: {type(issues)}")
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
