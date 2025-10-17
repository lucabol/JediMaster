"""
DeciderAgent - Uses LLM to evaluate GitHub issues for GitHub Copilot suitability.
"""

import json
import logging
import os
from typing import Dict, Any, Optional
from agent_framework.azure import AzureAIAgentClient
from azure.identity.aio import DefaultAzureCredential


class DeciderAgent:
    """Agent that uses LLM to decide if an issue is suitable for GitHub Copilot."""

    def __init__(self, azure_foundry_endpoint: str, model: str = None):
        # Load configuration from environment variables
        self.model = model or os.getenv('AZURE_AI_MODEL', 'model-router')
        self.azure_foundry_endpoint = azure_foundry_endpoint
        self.logger = logging.getLogger('jedimaster.decider')
        self._credential: Optional[DefaultAzureCredential] = None
        self._client: Optional[AzureAIAgentClient] = None
        
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

    async def __aenter__(self):
        """Async context manager entry."""
        self._credential = DefaultAzureCredential()
        await self._credential.__aenter__()
        self._client = AzureAIAgentClient(async_credential=self._credential)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self._credential:
            await self._credential.__aexit__(exc_type, exc_val, exc_tb)

    async def _run_agent(self, agent_name: str, prompt: str) -> str:
        """
        Helper method to create and run an agent with the system prompt.
        Uses the credential and client initialized in __aenter__.
        Includes retry logic for transient service errors.
        
        Args:
            agent_name: Name for the agent instance
            prompt: User prompt to send to the agent
            
        Returns:
            Raw text response from the agent
            
        Raises:
            ValueError: If agent returns empty response
        """
        import asyncio
        import traceback
        from agent_framework import ChatAgent
        from agent_framework.exceptions import ServiceResponseException
        
        # Retry logic for transient errors
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Use the EXISTING credential and client from __aenter__
                # Only create a new agent (which is lightweight)
                async with ChatAgent(
                    chat_client=self._client,
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
            except ServiceResponseException as e:
                # Log the service error
                self.logger.warning(f"ServiceResponseException on attempt {attempt + 1}/{max_retries}: {e}")
                
                if attempt < max_retries - 1:
                    # Exponential backoff: 2, 4, 8 seconds
                    wait_time = 2 ** (attempt + 1)
                    self.logger.info(f"Retrying in {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                else:
                    # Last attempt failed, log and re-raise
                    self.logger.error(f"All {max_retries} attempts failed for ServiceResponseException")
                    raise
            except Exception as e:
                # Log full exception details for other errors (no retry)
                self.logger.error(f"Non-retryable exception in _run_agent: {type(e).__name__}: {e}")
                self.logger.error(f"Full traceback:\n{traceback.format_exc()}")
                raise

    async def evaluate_issue(self, issue_data: Dict[str, Any]) -> Dict[str, str]:
        """Evaluate a GitHub issue using the Agent Framework."""
        try:
            issue_text = self._format_issue_for_llm(issue_data)
            prompt = f"Please evaluate this GitHub issue:\n\n{issue_text}"
            
            # Use helper method to run agent
            result_text = await self._run_agent("IssueDeciderAgent", prompt)
            
            # Parse JSON response
            parsed_result = json.loads(result_text)
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
    """Agent that uses LLM to decide if a PR can be checked in or needs a comment."""

    def __init__(self, azure_foundry_endpoint: str, model: str = None):
        # Load configuration from environment variables
        self.model = model or os.getenv('AZURE_AI_MODEL', 'model-router')
        self.azure_foundry_endpoint = azure_foundry_endpoint
        self.logger = logging.getLogger('jedimaster.prdecider')
        self._credential: Optional[DefaultAzureCredential] = None
        self._client: Optional[AzureAIAgentClient] = None
        
        self.system_prompt = (
            "You are an expert AI assistant tasked with reviewing GitHub pull requests. "
            "You must make a binary decision for each PR:\n\n"
            "Respond with a JSON object containing either:\n"
            "- {'decision': 'accept'} if the PR can be merged as-is\n"
            "- {'comment': 'detailed feedback'} if changes are needed\n\n"
            "When you provide a comment, the PR will get a formal CHANGES_REQUESTED review state.\n"
            "When you accept, the PR will get an APPROVED review state.\n\n"
            "Guidelines:\n"
            "- Accept PRs that are well-written, properly tested, and ready to merge\n"
            "- Request changes for PRs that need improvements, missing tests, unclear code, etc.\n"
            "- Provide specific, actionable feedback in your comments\n"
            "- Consider code quality, completeness, and adherence to best practices"
        )

    async def __aenter__(self):
        """Async context manager entry."""
        self._credential = DefaultAzureCredential()
        await self._credential.__aenter__()
        self._client = AzureAIAgentClient(async_credential=self._credential)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self._credential:
            await self._credential.__aexit__(exc_type, exc_val, exc_tb)

    async def _run_agent(self, agent_name: str, prompt: str) -> str:
        """
        Helper method to create and run an agent with the system prompt.
        Uses the credential and client initialized in __aenter__.
        Includes retry logic for transient service errors.
        
        Args:
            agent_name: Name for the agent instance
            prompt: User prompt to send to the agent
            
        Returns:
            Raw text response from the agent
            
        Raises:
            ValueError: If agent returns empty response
        """
        import asyncio
        import traceback
        from agent_framework import ChatAgent
        from agent_framework.exceptions import ServiceResponseException
        
        # Retry logic for transient errors
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Use the EXISTING credential and client from __aenter__
                # Only create a new agent (which is lightweight)
                async with ChatAgent(
                    chat_client=self._client,
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
            except ServiceResponseException as e:
                # Log the service error
                self.logger.warning(f"ServiceResponseException on attempt {attempt + 1}/{max_retries}: {e}")
                
                if attempt < max_retries - 1:
                    # Exponential backoff: 2, 4, 8 seconds
                    wait_time = 2 ** (attempt + 1)
                    self.logger.info(f"Retrying in {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                else:
                    # Last attempt failed, log and re-raise
                    self.logger.error(f"All {max_retries} attempts failed for ServiceResponseException")
                    raise
            except Exception as e:
                # Log full exception details for other errors (no retry)
                self.logger.error(f"Non-retryable exception in _run_agent: {type(e).__name__}: {e}")
                self.logger.error(f"Full traceback:\n{traceback.format_exc()}")
                raise

    async def evaluate_pr(self, pr_text: str) -> dict:
        """Evaluate a PR and return either a decision or a comment."""
        try:
            prompt = f"Please review this pull request:\n\n{pr_text}"
            
            # Use helper method to run agent
            result_text = await self._run_agent("PRDeciderAgent", prompt)
            
            parsed_result = json.loads(result_text)
            self.logger.debug(f"Parsed agent response: {parsed_result}")
            
            if not (("decision" in parsed_result and parsed_result["decision"] == "accept") or "comment" in parsed_result):
                raise ValueError("Agent response missing required fields: must have 'decision' or 'comment'")
            
            self.logger.debug(f"Agent PR review result: {parsed_result}")
            return parsed_result
                
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse agent response as JSON: {e}")
            self.logger.error(f"Raw response that failed to parse: {result_text}")
            return {
                'comment': 'Error: Could not parse agent response'
            }
        except Exception as e:
            self.logger.error(f"Error calling agent for PR evaluation: {e}")
            return {
                'comment': f'Error: {str(e)}'
            }
