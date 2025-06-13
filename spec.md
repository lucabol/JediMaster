# JediMaster - Specification
A python script that retrieves all the issues from a list of GitHub repositories, decides which issues are a good fit for Github Copilot and assigns them to Github Copilot. To make this decision, it invokes and agent ('Decider') that uses a large language model (LLM) to evaluate the issues.

## Algorithm Overview
1. Fetch all issues from the provided GitHub repositories.each issue, including its title, body, labels, and comments.
2. For each issue,
   - Use the 'Decider' agent to evaluate if the issue is a good fit for GitHub Copilot.
   - If the issue is a good fit, assign it to GitHub Copilot.
3. Generate a report summarizing the issues that were assigned to GitHub Copilot, issues that were not assigned, issues that were already assigned, and errors encountered during the process.

## Input
- A list of GitHub repositories (e.g., `["repo1", "repo2"]`).

## Output
- A report of the issues processed, including their status (assigned, not assigned, already assigned) and any errors encountered.

## Output of the Decider Agent
A json object wtih the following structure:
```json
 {
    "decision": "yes|no",
    "reasoning": "The reasoning behind the decision"
  }
