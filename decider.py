"""
DeciderAgent and PRDeciderAgent - Use Azure AI Foundry agents for evaluation.
"""

import asyncio
import json
import logging
import os
from typing import Dict, Any, Optional
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential


class DeciderAgent:
    """Agent that uses Foundry DeciderAgent to decide if an issue is suitable for GitHub Copilot."""

    def __init__(self, azure_foundry_project_endpoint: str, model: str = None, verbose: bool = False):
        self.azure_foundry_project_endpoint = azure_foundry_project_endpoint
        self.verbose = verbose
        self.logger = logging.getLogger('jedimaster.decider')
        self._credential: Optional[DefaultAzureCredential] = None
        self._project_client: Optional[AIProjectClient] = None
        self._openai_client = None
        self._agent = None

    async def __aenter__(self):
        """Async context manager entry."""
        self._credential = DefaultAzureCredential(exclude_cli_credential=True)
        
        # Create project client (synchronous)
        self._project_client = AIProjectClient(
            endpoint=self.azure_foundry_project_endpoint,
            credential=self._credential
        )
        
        # Get the DeciderAgent from Foundry
        self._agent = self._project_client.agents.get(agent_name="DeciderAgent")
        self.logger.info(f"Retrieved DeciderAgent from Foundry: {self._agent.id}")
        
        # Get OpenAI client for invoking the agent
        self._openai_client = self._project_client.get_openai_client()
        
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        # Synchronous SDK doesn't need explicit cleanup
        pass

    async def _run_agent(self, prompt: str) -> str:
        """
        Invoke the Foundry DeciderAgent with the given prompt.
        
        Args:
            prompt: User prompt to send to the agent
            
        Returns:
            Raw text response from the agent
            
        Raises:
            ValueError: If agent returns empty response
        """
        # Log in verbose mode
        if self.verbose:
            self.logger.info(f"[DeciderAgent] Calling Foundry agent: {self._agent.name}")
        
        # Call the Foundry agent (synchronous call wrapped in async)
        loop = asyncio.get_event_loop()
        
        # Define sync function for executor
        def call_foundry_api():
            self.logger.error(f"[DeciderAgent DEBUG] About to call Foundry API")
            result = self._openai_client.responses.create(
                input=[{"role": "user", "content": prompt}],
                extra_body={"agent": {"name": self._agent.name, "type": "agent_reference"}}
            )
            self.logger.error(f"[DeciderAgent DEBUG] API call completed, result type: {type(result)}")
            return result
        
        # Run synchronous Foundry call in executor to avoid blocking
        try:
            response = await loop.run_in_executor(None, call_foundry_api)
            
            # Debug: Check response type and attributes
            self.logger.error(f"[DeciderAgent DEBUG] Response type: {type(response)}")
            self.logger.error(f"[DeciderAgent DEBUG] Response dir: {dir(response)}")
            self.logger.error(f"[DeciderAgent DEBUG] Response repr: {repr(response)}")
            
            # Extract text from response - handle both object and string types
            if isinstance(response, str):
                self.logger.error(f"[DeciderAgent DEBUG] Response is string")
                result_text = response
            elif hasattr(response, 'output_text'):
                self.logger.error(f"[DeciderAgent DEBUG] Response has output_text attribute")
                result_text = response.output_text
            elif hasattr(response, 'text'):
                self.logger.error(f"[DeciderAgent DEBUG] Response has text attribute")
                result_text = response.text
            else:
                # Fallback: try to get text from response object
                self.logger.error(f"[DeciderAgent DEBUG] Using str() fallback")
                result_text = str(response)
                
        except Exception as e:
            self.logger.error(f"[DeciderAgent DEBUG] Exception during API call: {type(e).__name__}: {e}")
            import traceback
            self.logger.error(f"[DeciderAgent DEBUG] Traceback:\n{traceback.format_exc()}")
            raise
        
        if not result_text:
            self.logger.error(f"Agent returned empty response")
            raise ValueError("Agent returned empty response")
        
        self.logger.debug(f"Agent raw response: {result_text[:500]}...")
        return result_text

    async def evaluate_issue(self, issue_data: Dict[str, Any]) -> Dict[str, str]:
        """Evaluate a GitHub issue using the Foundry DeciderAgent."""
        try:
            issue_text = self._format_issue_for_llm(issue_data)
            prompt = f"Please evaluate this GitHub issue:\n\n{issue_text}"
            
            # Use helper method to run agent
            result_text = await self._run_agent(prompt)
            
            # Strip markdown formatting if present
            cleaned_text = self._strip_markdown_json(result_text)
            
            # Parse JSON response
            parsed_result = json.loads(cleaned_text)
            self.logger.debug(f"Parsed agent response: {parsed_result}")
            
            if 'decision' not in parsed_result or 'reasoning' not in parsed_result:
                raise ValueError("Agent response missing required fields")
            
            decision = parsed_result['decision'].lower().strip()
            if decision not in ['yes', 'no']:
                self.logger.warning(f"Unexpected decision value: {decision}, defaulting to 'no'")
                decision = 'no'
            
            validated_result = {
                'decision': decision,
                'reasoning': parsed_result['reasoning']
            }
            
            self.logger.debug(f"Agent decision: {decision}, reasoning: {parsed_result['reasoning'][:100]}...")
            return validated_result
                
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse agent response as JSON: {e}")
            self.logger.error(f"Raw response that failed to parse: {result_text}")
            return {
                'decision': 'error',
                'reasoning': 'Error: Could not parse agent response'
            }
        except Exception as e:
            self.logger.error(f"Error calling agent for issue evaluation: {e}")
            return {
                'decision': 'error',
                'reasoning': f'Error: {str(e)}'
            }

    def _format_issue_for_llm(self, issue_data: Dict[str, Any]) -> str:
        """Format issue data for LLM prompt."""
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

    async def batch_evaluate_issues(self, issues_data: list) -> list:
        """Evaluate multiple issues (sequentially for now, could be parallelized)."""
        results = []
        for issue_data in issues_data:
            result = await self.evaluate_issue(issue_data)
            results.append(result)
        return results


class PRDeciderAgent:
    """Agent that uses Foundry PRDeciderAgent to decide if a PR can be checked in or needs a comment."""

    def __init__(self, azure_foundry_project_endpoint: str, model: str = None, verbose: bool = False):
        self.azure_foundry_project_endpoint = azure_foundry_project_endpoint
        self.verbose = verbose
        self.logger = logging.getLogger('jedimaster.prdecider')
        self._credential: Optional[DefaultAzureCredential] = None
        self._project_client: Optional[AIProjectClient] = None
        self._openai_client = None
        self._agent = None

    async def __aenter__(self):
        """Async context manager entry."""
        self._credential = DefaultAzureCredential(exclude_cli_credential=True)
        
        # Create project client (synchronous)
        self._project_client = AIProjectClient(
            endpoint=self.azure_foundry_project_endpoint,
            credential=self._credential
        )
        
        # Get the PRDeciderAgent from Foundry
        self._agent = self._project_client.agents.get(agent_name="PRDeciderAgent")
        self.logger.info(f"Retrieved PRDeciderAgent from Foundry: {self._agent.id}")
        
        # Get OpenAI client for invoking the agent
        self._openai_client = self._project_client.get_openai_client()
        
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        # Synchronous SDK doesn't need explicit cleanup
        pass

    async def _run_agent(self, prompt: str) -> str:
        """
        Invoke the Foundry PRDeciderAgent with the given prompt.
        
        Args:
            prompt: User prompt to send to the agent
            
        Returns:
            Raw text response from the agent
            
        Raises:
            ValueError: If agent returns empty response
        """
        # Log in verbose mode
        if self.verbose:
            self.logger.info(f"[PRDeciderAgent] Calling Foundry agent: {self._agent.name}")
        
        # Call the Foundry agent (synchronous call wrapped in async)
        loop = asyncio.get_event_loop()
        
        # Define sync function for executor
        def call_foundry_api():
            self.logger.error(f"[PRDeciderAgent DEBUG] About to call Foundry API")
            result = self._openai_client.responses.create(
                input=[{"role": "user", "content": prompt}],
                extra_body={"agent": {"name": self._agent.name, "type": "agent_reference"}}
            )
            self.logger.error(f"[PRDeciderAgent DEBUG] API call completed, result type: {type(result)}")
            return result
        
        # Run synchronous Foundry call in executor to avoid blocking
        try:
            response = await loop.run_in_executor(None, call_foundry_api)
            
            # Debug: Check response type and attributes
            self.logger.error(f"[PRDeciderAgent DEBUG] Response type: {type(response)}")
            self.logger.error(f"[PRDeciderAgent DEBUG] Response dir: {dir(response)}")
            self.logger.error(f"[PRDeciderAgent DEBUG] Response repr: {repr(response)}")
            
            # Extract text from response - handle both object and string types
            if isinstance(response, str):
                self.logger.error(f"[PRDeciderAgent DEBUG] Response is string")
                result_text = response
            elif hasattr(response, 'output_text'):
                self.logger.error(f"[PRDeciderAgent DEBUG] Response has output_text attribute")
                result_text = response.output_text
            elif hasattr(response, 'text'):
                self.logger.error(f"[PRDeciderAgent DEBUG] Response has text attribute")
                result_text = response.text
            else:
                # Fallback: try to get text from response object
                self.logger.error(f"[PRDeciderAgent DEBUG] Using str() fallback")
                result_text = str(response)
                
        except Exception as e:
            self.logger.error(f"[PRDeciderAgent DEBUG] Exception during API call: {type(e).__name__}: {e}")
            import traceback
            self.logger.error(f"[PRDeciderAgent DEBUG] Traceback:\n{traceback.format_exc()}")
            raise
        
        if not result_text:
            self.logger.error(f"Agent returned empty response")
            raise ValueError("Agent returned empty response")
        
        self.logger.debug(f"Agent raw response: {result_text[:500]}...")
        return result_text

    def _strip_markdown_json(self, text: str) -> str:
        """Strip markdown code block formatting from JSON response."""
        text = text.strip()
        # Remove ```json or ``` prefix
        if text.startswith('```json'):
            text = text[7:]
        elif text.startswith('```'):
            text = text[3:]
        # Remove ``` suffix
        if text.endswith('```'):
            text = text[:-3]
        return text.strip()

    async def evaluate_pr(self, pr_data: Dict[str, Any]) -> Dict[str, str]:
        """Evaluate a GitHub PR using the Foundry PRDeciderAgent."""
        try:
            pr_text = self._format_pr_for_llm(pr_data)
            prompt = f"Please review this GitHub pull request:\n\n{pr_text}"
            
            # Use helper method to run agent
            result_text = await self._run_agent(prompt)
            
            # Strip markdown formatting if present
            cleaned_text = self._strip_markdown_json(result_text)
            
            # Parse JSON response
            parsed_result = json.loads(cleaned_text)
            self.logger.error(f"[PRDeciderAgent DEBUG] Parsed result type: {type(parsed_result)}")
            self.logger.error(f"[PRDeciderAgent DEBUG] Parsed result: {parsed_result}")
            self.logger.debug(f"Parsed agent response: {parsed_result}")
            
            if 'decision' not in parsed_result or 'comment' not in parsed_result:
                raise ValueError("Agent response missing required fields")
            
            decision = parsed_result['decision'].lower().strip()
            if decision not in ['accept', 'changes_requested']:
                self.logger.warning(f"Unexpected decision value: {decision}, defaulting to 'changes_requested'")
                decision = 'changes_requested'
            
            validated_result = {
                'decision': decision,
                'comment': parsed_result['comment']
            }
            
            self.logger.debug(f"Agent decision: {decision}, comment: {parsed_result['comment'][:100]}...")
            return validated_result
                
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse agent response as JSON: {e}")
            self.logger.error(f"Raw response that failed to parse: {result_text}")
            return {
                'decision': 'error',
                'comment': 'Error: Could not parse agent response'
            }
        except Exception as e:
            self.logger.error(f"Error calling agent for PR evaluation: {e}")
            return {
                'decision': 'error',
                'comment': f'Error: {str(e)}'
            }

    def _format_pr_for_llm(self, pr_data: Dict[str, Any]) -> str:
        """Format PR data for LLM prompt."""
        formatted = f"**Title:** {pr_data['title']}\n\n"
        if pr_data.get('body'):
            formatted += f"**Description:**\n{pr_data['body']}\n\n"
        if pr_data.get('diff'):
            # Limit diff size to avoid token limits
            diff_text = pr_data['diff']
            if len(diff_text) > 10000:
                diff_text = diff_text[:10000] + "\n\n... (diff truncated)"
            formatted += f"**Changes (diff):**\n```diff\n{diff_text}\n```\n\n"
        if pr_data.get('files_changed'):
            formatted += f"**Files Changed:** {pr_data['files_changed']}\n"
        if pr_data.get('additions'):
            formatted += f"**Additions:** +{pr_data['additions']} lines\n"
        if pr_data.get('deletions'):
            formatted += f"**Deletions:** -{pr_data['deletions']} lines\n"
        return formatted
