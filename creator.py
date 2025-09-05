"""
CreatorAgent - Uses LLM to suggest and open new GitHub issues based on repository context.
"""

import os
import logging
import json
from typing import List, Dict, Any, Optional
from openai import OpenAI
from github import Github

class CreatorAgent:
    """Agent that uses LLM to suggest and open new GitHub issues."""
    def __init__(self, github_token: str, openai_api_key: str, repo_full_name: str, model: str = "gpt-3.5-turbo"):
        self.github_token = github_token
        self.openai_api_key = openai_api_key
        self.repo_full_name = repo_full_name
        self.model = model
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
        """Suggest and open new issues in the repo."""
        issues = self.suggest_issues(max_issues=max_issues)
        if not issues:
            self.logger.warning("No issues suggested by LLM.")
            return []
        return self.open_issues(issues)
