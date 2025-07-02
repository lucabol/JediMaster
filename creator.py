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
            "Return a JSON array of objects, each with a 'title' and 'body' field. "
            "Each issue should be specific, actionable, and relevant to the code or documentation. "
            "Do not include duplicate or trivial issues."
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
            f"Given the following repository context, suggest up to {max_issues} new GitHub issues. "
            "Return a JSON array of objects, each with 'title' and 'body'.\n\n"
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
            max_tokens=1000,
            response_format={"type": "json_object"}
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
            issues = json.loads(result_text)
            # Accept either a top-level list or a dict with 'suggestions' key
            if isinstance(issues, list):
                return issues[:max_issues]
            elif isinstance(issues, dict) and 'suggestions' in issues and isinstance(issues['suggestions'], list):
                return issues['suggestions'][:max_issues]
            else:
                raise ValueError("LLM did not return a list of issues or a dict with 'suggestions' list")
        except Exception as e:
            self.logger.error(f"Failed to parse LLM response: {e}")
            # Still return empty, but conversation is available for printing
            conv = getattr(self, 'last_conversation', None)
            print("\n--- LLM Conversation ---")
            if conv:
                print("[System Prompt]:\n" + conv.get("system", ""))
                print("\n[User Prompt]:\n" + conv.get("user", ""))
                print("\n[LLM Response]:\n" + str(conv.get("llm_response", "")))
            else:
                print("[No conversation captured]")
            print("--- End Conversation ---\n")
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
