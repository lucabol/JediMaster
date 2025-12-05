"""
DeciderAgent - Uses LLM to evaluate GitHub issues for GitHub Copilot suitability.
"""

import json
import logging
import os
from typing import Dict, Any, Optional
from agent_framework.azure import AzureAIAgentClient
from azure.identity.aio import DefaultAzureCredential, AzureCliCredential


class DeciderAgent:
    """Agent that uses LLM to decide if an issue is suitable for GitHub Copilot."""

    def __init__(self, azure_foundry_endpoint: str, model: str = None, verbose: bool = False):
        # Load configuration from environment variables
        self.model = model or os.getenv('AZURE_AI_MODEL', 'model-router')
        self.azure_foundry_endpoint = azure_foundry_endpoint
        self.verbose = verbose
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
        self._credential = DefaultAzureCredential(exclude_cli_credential=True)
        await self._credential.__aenter__()
        # Don't create shared client - will create per agent call
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self._credential:
            await self._credential.__aexit__(exc_type, exc_val, exc_tb)

    async def _run_agent(self, agent_name: str, prompt: str) -> str:
        """
        Helper method to create and run an agent with the system prompt.
        Creates a new client for each agent (which ChatAgent will manage).
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
        
        # Log endpoint and model in verbose mode
        if self.verbose:
            self.logger.info(f"[Agent] Calling Azure AI Foundry - Endpoint: {self.azure_foundry_endpoint}")
        if self.verbose:
            self.logger.info(f"[Agent] Model: {self.model}")
        
        # Retry logic for transient errors
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Create a NEW client for each ChatAgent - the ChatAgent will manage its lifecycle
                # This is the pattern from Microsoft's docs
                async with ChatAgent(
                    chat_client=AzureAIAgentClient(async_credential=self._credential),
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

    def __init__(self, azure_foundry_endpoint: str, model: str = None, verbose: bool = False):
        # Load configuration from environment variables
        self.model = model or os.getenv('AZURE_AI_MODEL', 'model-router')
        self.azure_foundry_endpoint = azure_foundry_endpoint
        self.verbose = verbose
        self.logger = logging.getLogger('jedimaster.prdecider')
        self._credential: Optional[DefaultAzureCredential] = None
        self._client: Optional[AzureAIAgentClient] = None
        
        self.system_prompt = (
            "You are an expert AI assistant reviewing GitHub pull requests created by GitHub Copilot. "
            "Your role is to ensure the code works correctly and is safe to merge.\n\n"
            "**IMPORTANT: Be pragmatic and supportive of Copilot's work. Accept PRs that solve the problem correctly, "
            "even if they could be slightly improved. Only request changes for serious issues.**\n\n"
            "Respond with ONLY a JSON object in this EXACT format:\n\n"
            '{\n'
            '  "decision": "accept" or "changes_requested",\n'
            '  "comment": "Your detailed feedback here (can be empty string if accepting)"\n'
            '}\n\n'
            "EXAMPLES:\n\n"
            "Example 1 - Accepting a working PR:\n"
            '{"decision": "accept", "comment": "LGTM! The implementation solves the issue correctly."}\n\n'
            "Example 2 - Accepting with minor suggestions:\n"
            '{"decision": "accept", "comment": "Works well! Minor optimization could be done in a future PR if needed."}\n\n'
            "Example 3 - Requesting changes for serious issue:\n"
            '{"decision": "changes_requested", "comment": "The null pointer on line 42 will cause crashes. Please add a null check before accessing the object."}\n\n'
            "CRITICAL REQUIREMENTS:\n"
            "- You MUST return BOTH fields: decision and comment\n"
            "- decision MUST be exactly \"accept\" or \"changes_requested\" (lowercase)\n"
            "- comment can be an empty string \"\" if accepting, but the field must be present\n"
            "- If requesting changes, comment MUST contain specific, actionable feedback about serious issues only\n"
            "- ALWAYS return valid JSON with both fields\n\n"
            "When decision is \"accept\", the PR will get an APPROVED review and may be merged.\n"
            "When decision is \"changes_requested\", the PR will get a CHANGES_REQUESTED review with your comment.\n\n"
            "**Review Guidelines - ACCEPT if:**\n"
            "- The code solves the issue/implements the feature correctly\n"
            "- No bugs, security vulnerabilities, or crashes are present\n"
            "- Tests pass (if applicable)\n"
            "- Code is readable and maintainable\n"
            "→ Minor style issues, small optimizations, or documentation improvements are NOT reasons to reject\n\n"
            "**Request Changes ONLY if:**\n"
            "- There are bugs that will cause the code to fail\n"
            "- Security vulnerabilities are present\n"
            "- The implementation doesn't actually solve the issue\n"
            "- Breaking changes are introduced without proper handling\n"
            "- Critical error handling is missing (e.g., null checks that will crash)\n\n"
            "**Default to ACCEPT** - If you're unsure or the issue is minor, choose accept. "
            "Small improvements can be done in future PRs. Trust Copilot's work when it's correct.\n\n"
            "JSON formatting rules:\n"
            "- Return ONLY valid JSON (no markdown, no explanation text)\n"
            "- Escape all backslashes: use \\\\ instead of \\\n"
            "- Escape all quotes inside strings: use \\\" for quotes\n"
            "- Escape control characters: \\n for newlines, \\t for tabs\n"
            "- Do NOT wrap JSON in ```json blocks\n"
            "- Do NOT add any text before or after the JSON object"
        )

    async def __aenter__(self):
        """Async context manager entry."""
        self._credential = DefaultAzureCredential(exclude_cli_credential=True)
        await self._credential.__aenter__()
        # Don't create shared client - will create per agent call
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self._credential:
            await self._credential.__aexit__(exc_type, exc_val, exc_tb)

    async def _run_agent(self, agent_name: str, prompt: str) -> str:
        """
        Helper method to create and run an agent with the system prompt.
        Creates a new client for each agent (which ChatAgent will manage).
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
        
        # Log endpoint and model in verbose mode
        if self.verbose:
            self.logger.info(f"[Agent] Calling Azure AI Foundry - Endpoint: {self.azure_foundry_endpoint}")
        if self.verbose:
            self.logger.info(f"[Agent] Model: {self.model}")
        
        # Retry logic for transient errors
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # Create a NEW client for each ChatAgent - the ChatAgent will manage its lifecycle
                # This is the pattern from Microsoft's docs
                async with ChatAgent(
                    chat_client=AzureAIAgentClient(async_credential=self._credential),
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
            
            # Try to parse as-is first
            try:
                parsed_result = json.loads(result_text)
            except json.JSONDecodeError as e:
                # If parsing fails, try to extract JSON from response
                # Sometimes LLM wraps JSON in markdown code blocks
                self.logger.warning(f"Initial JSON parse failed: {e}. Attempting to extract JSON...")
                
                # Try to find JSON object in the response
                import re
                json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
                if json_match:
                    result_text = json_match.group(0)
                    parsed_result = json.loads(result_text)
                else:
                    raise
            
            self.logger.debug(f"Parsed agent response: {parsed_result}")
            
            # Validate: must have both 'decision' and 'comment' fields
            if "decision" not in parsed_result:
                raise ValueError(
                    f"Agent response missing 'decision' field. Got: {parsed_result}"
                )
            if "comment" not in parsed_result:
                raise ValueError(
                    f"Agent response missing 'comment' field. Got: {parsed_result}"
                )
            
            decision = parsed_result["decision"]
            if decision not in ["accept", "changes_requested"]:
                raise ValueError(
                    f"Agent decision must be 'accept' or 'changes_requested'. Got: {decision}"
                )
            
            self.logger.debug(f"Agent PR review result: decision={decision}, comment_length={len(parsed_result['comment'])}")
            return parsed_result
                
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse agent response as JSON: {e}")
            self.logger.error(f"Raw response (truncated to 500 chars): {result_text[:500]}")
            return {
                'comment': 'Error: Could not parse agent response. Please review manually.'
            }
        except Exception as e:
            self.logger.error(f"Error calling agent for PR evaluation: {e}")
            return {
                'comment': f'Error: {str(e)}'
            }

