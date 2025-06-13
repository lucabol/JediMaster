"""
DeciderAgent - Uses LLM to evaluate GitHub issues for GitHub Copilot suitability.
"""

import json
import logging
from typing import Dict, Any
from openai import OpenAI


class DeciderAgent:
    """Agent that uses LLM to decide if an issue is suitable for GitHub Copilot."""
    
    def __init__(self, openai_api_key: str, model: str = "gpt-3.5-turbo"):
        """Initialize the DeciderAgent with OpenAI API key."""
        self.client = OpenAI(api_key=openai_api_key)
        self.model = model
        self.logger = logging.getLogger('jedimaster.decider')
        
        # System prompt for the LLM
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

    def evaluate_issue(self, issue_data: Dict[str, Any]) -> Dict[str, str]:
        """Evaluate an issue and return decision with reasoning."""
        try:
            # Format the issue data for the LLM
            issue_text = self._format_issue_for_llm(issue_data)
              # Create the messages for the chat completion
            messages = [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": f"Please evaluate this GitHub issue:\n\n{issue_text}"}
            ]
            
            # Call the OpenAI API
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,  # type: ignore
                temperature=0.1,  # Low temperature for consistent decisions
                max_tokens=500,
                response_format={"type": "json_object"}
            )
            
            # Parse the response
            result_text = response.choices[0].message.content
            if result_text is None:
                raise ValueError("LLM returned empty response")
            result = json.loads(result_text)
            
            # Validate the response format
            if 'decision' not in result or 'reasoning' not in result:
                raise ValueError("LLM response missing required fields")
            
            # Normalize the decision
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
        """Format issue data into a readable text for the LLM."""
        formatted = f"**Title:** {issue_data['title']}\n\n"
        
        if issue_data.get('body'):
            formatted += f"**Description:**\n{issue_data['body']}\n\n"
        
        if issue_data.get('labels'):
            formatted += f"**Labels:** {', '.join(issue_data['labels'])}\n\n"
        
        if issue_data.get('comments'):
            formatted += "**Recent Comments:**\n"
            for i, comment in enumerate(issue_data['comments'][-3:], 1):  # Last 3 comments
                # Truncate very long comments
                comment_text = comment[:300] + "..." if len(comment) > 300 else comment
                formatted += f"{i}. {comment_text}\n"
        
        return formatted
    
    def batch_evaluate_issues(self, issues_data: list) -> list:
        """Evaluate multiple issues in batch (for future optimization)."""
        results = []
        for issue_data in issues_data:
            result = self.evaluate_issue(issue_data)
            results.append(result)
        return results
