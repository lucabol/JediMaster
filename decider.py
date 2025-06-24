"""
DeciderAgent - Uses LLM to evaluate GitHub issues for GitHub Copilot suitability.
"""

import json
import logging
from typing import Dict, Any
from openai import OpenAI


class DeciderAgent:
    """Agent that uses LLM to decide if an issue is suitable for GitHub Copilot."""
    # ...existing code...

    def __init__(self, openai_api_key: str, model: str = "gpt-3.5-turbo"):
        # ...existing code...
        self.client = OpenAI(api_key=openai_api_key)
        self.model = model
        self.logger = logging.getLogger('jedimaster.decider')
        # ...existing code...
        self.system_prompt = """You are an expert AI assistant tasked with evaluating GitHub issues to determine if they are suitable for GitHub Copilot assistance. GitHub Copilot excels at:

1. **Code Generation & Implementation**:
   - Writing new functions, classes, or modules
   - Implementing algorithms and data structures
   - Creating boilerplate code and templates
   - Converting pseudocode to actual code

2. **Code Improvement & Refactoring**:
   - Optimizing existing code for performance or readability
   - Refactoring code to follow best practices
   - Adding error handling and validation
   - Updating code to use newer language features

3. **Documentation & Testing**:
   - Writing code comments and documentation
   - Creating unit tests and test cases
   - Generating API documentation
   - Adding type hints and annotations

4. **Bug Fixes & Debugging**:
   - Fixing syntax errors and logical bugs
   - Implementing missing functionality
   - Resolving deprecation warnings
   - Fixing security vulnerabilities

5. **Integration & Configuration**:
   - Integrating APIs and third-party libraries
   - Setting up build scripts and configuration files
   - Creating deployment scripts
   - Writing automation scripts

**Issues that are NOT suitable for GitHub Copilot**:
- Pure discussion or planning issues
- Questions about project direction or architecture decisions
- Issues requiring human judgment about UX/UI design
- Issues that need extensive domain knowledge or business context
- Issues about community management or non-technical topics
- Issues that are primarily about gathering requirements
- Issues that require manual testing or user research

Analyze the provided GitHub issue and respond with a JSON object containing:
- "decision": "yes" if suitable for Copilot, "no" if not suitable
- "reasoning": A clear explanation of why the issue is or isn't suitable for Copilot

Be concise but thorough in your reasoning. Focus on whether the issue involves concrete coding tasks that Copilot can assist with."""
    # ...existing code...

    def evaluate_issue(self, issue_data: Dict[str, Any]) -> Dict[str, str]:
        # ...existing code...
        try:
            issue_text = self._format_issue_for_llm(issue_data)
            messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": f"Please evaluate this GitHub issue:\n\n{issue_text}"}
            ]
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,  # type: ignore
                temperature=0.1,
                max_tokens=500,
                response_format={"type": "json_object"}
            )
            result_text = response.choices[0].message.content
            if result_text is None:
                raise ValueError("LLM returned empty response")
            result = json.loads(result_text)
            if 'decision' not in result or 'reasoning' not in result:
                raise ValueError("LLM response missing required fields")
            decision = result['decision'].lower().strip()
            if decision not in ['yes', 'no']:
                self.logger.warning(f"Unexpected decision value: {decision}, defaulting to 'no'")
                decision = 'no'
            validated_result = {
                'decision': decision,
                'reasoning': result['reasoning']
            }
            self.logger.debug(f"LLM decision: {decision}, reasoning: {result['reasoning'][:100]}...")
            return validated_result
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse LLM response as JSON: {e}")
            return {
                'decision': 'no',
                'reasoning': 'Error: Could not parse LLM response'
            }
        except Exception as e:
            self.logger.error(f"Error calling LLM for issue evaluation: {e}")
            return {
                'decision': 'no',
                'reasoning': f'Error: {str(e)}'
            }

    def _format_issue_for_llm(self, issue_data: Dict[str, Any]) -> str:
        # ...existing code...
        formatted = f"**Title:** {issue_data['title']}\n\n"
        if issue_data.get('body'):
            formatted += f"**Description:**\n{issue_data['body']}\n\n"
        if issue_data.get('labels'):
            formatted += f"**Labels:** {', '.join(issue_data['labels'])}\n\n"
        if issue_data.get('comments'):
            formatted += "**Recent Comments:**\n"
            for i, comment in enumerate(issue_data['comments'][-3:], 1):
                comment_text = comment[:300] + "..." if len(comment) > 300 else comment
                formatted += f"{i}. {comment_text}\n"
        return formatted

    def batch_evaluate_issues(self, issues_data: list) -> list:
        # ...existing code...
        results = []
        for issue_data in issues_data:
            result = self.evaluate_issue(issue_data)
            results.append(result)
        return results

# --- New class for PR Decider Agent ---
class PRDeciderAgent:
    """Agent that uses LLM to decide if a PR can be checked in or needs a comment."""

    def __init__(self, openai_api_key: str, model: str = "gpt-3.5-turbo"):
        self.client = OpenAI(api_key=openai_api_key)
        self.model = model
        self.logger = logging.getLogger('jedimaster.prdecider')
        self.system_prompt = (
    "You are an expert AI assistant tasked with reviewing GitHub pull requests. "
    "Respond with a JSON object containing either:\n"
    "- 'decision': 'accept' if the PR can be merged as-is, or\n"
    "- 'comment': a string with constructive feedback to be inserted as a PR comment if changes are needed.\n"
    "Given the full text of a pull request (including title, description, and code changes):\n"
    "- If the PR has plenty of comments in the code changes, reply with {'decision': 'accept'}.\n"
    "- If the PR title does NOT has plenty of comments in the code changes, reply with {'comment': 'Please add more comments to your code changes.'}.\n"
    "Ensure your response is a valid JSON object with either 'decision' or 'comment'"
)

    def evaluate_pr(self, pr_text: str) -> dict:
        """Evaluate a PR and return either a decision or a comment."""
        try:
            messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": f"Please review this pull request:\n\n{pr_text}"}
            ]
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,  # type: ignore
                temperature=0.5,
                max_tokens=500,
                response_format={"type": "json_object"}
            )
            result_text = response.choices[0].message.content
            
            if result_text is None:
                raise ValueError("LLM returned empty response")
            result = json.loads(result_text)
            if not (("decision" in result and result["decision"] == "accept") or "comment" in result):
                raise ValueError("LLM response missing required fields: must have 'decision' or 'comment'")
            self.logger.debug(f"LLM PR review result: {result}")
            return result
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse LLM response as JSON: {e}")
            return {
                'comment': 'Error: Could not parse LLM response'
            }
        except Exception as e:
            self.logger.error(f"Error calling LLM for PR evaluation: {e}")
            return {
                'comment': f'Error: {str(e)}'
            }
