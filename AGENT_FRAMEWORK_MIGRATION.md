# Migration to Microsoft Agent Framework

## Overview
This document describes the conversion of JediMaster from direct Azure OpenAI client usage to the Microsoft Agent Framework with full async/await patterns.

## Key Changes

### 1. Agent Classes (decider.py)

**Before:**
```python
class DeciderAgent:
    def __init__(self, azure_foundry_endpoint: str, model: str = None):
        self.project_client = create_azure_ai_foundry_client(azure_foundry_endpoint)
        self.client = get_chat_client(self.project_client)
    
    def evaluate_issue(self, issue_data: Dict[str, Any]) -> Dict[str, str]:
        response = self.client.chat.completions.create(...)
```

**After:**
```python
class DeciderAgent:
    def __init__(self, azure_foundry_endpoint: str, model: str = None):
        self._credential = None
        self._client = None
    
    async def __aenter__(self):
        self._credential = DefaultAzureCredential()
        self._client = AzureAIAgentClient(async_credential=self._credential)
        return self
    
    async def evaluate_issue(self, issue_data: Dict[str, Any]) -> Dict[str, str]:
        async with self._client.create_agent(...) as agent:
            result = await agent.run(prompt)
```

### 2. JediMaster Class (jedimaster.py)

**Before:**
```python
class JediMaster:
    def __init__(self, ...):
        self.decider = DeciderAgent(azure_foundry_endpoint)
        self.pr_decider = PRDeciderAgent(azure_foundry_endpoint)
    
    def process_repositories(self, repo_names: List[str]) -> ProcessingReport:
        result = self.decider.evaluate_issue(issue_data)
```

**After:**
```python
class JediMaster:
    def __init__(self, ...):
        self._decider = None
        self._pr_decider = None
    
    async def __aenter__(self):
        self._decider = DeciderAgent(self.azure_foundry_endpoint)
        self._pr_decider = PRDeciderAgent(self.azure_foundry_endpoint)
        await self._decider.__aenter__()
        await self._pr_decider.__aenter__()
        return self
    
    async def process_repositories(self, repo_names: List[str]) -> ProcessingReport:
        result = await self.decider.evaluate_issue(issue_data)
```

### 3. CreatorAgent (creator.py)

**Similar pattern:**
- Added async context manager support
- Made `suggest_issues()` and `create_issues()` async
- Kept OpenAI client for embeddings (Agent Framework doesn't provide embeddings yet)

### 4. CLI Usage (jedimaster.py main())

**Before:**
```python
def main():
    jedimaster = JediMaster(...)
    report = jedimaster.process_repositories(repo_names)

if __name__ == '__main__':
    exit(main())
```

**After:**
```python
async def main():
    async with JediMaster(...) as jedimaster:
        report = await jedimaster.process_repositories(repo_names)

if __name__ == '__main__':
    import asyncio
    exit(asyncio.run(main()))
```

### 5. Azure Functions (function_app.py)

**Before:**
```python
@app.timer_trigger(...)
def AutomateRepos(automationTimer: func.TimerRequest) -> None:
    jedi = JediMaster(...)
    report = jedi.process_repositories([repo_full])
    pr_results = pr_jedi.manage_pull_requests(repo_full)
```

**After:**
```python
@app.timer_trigger(...)
async def AutomateRepos(automationTimer: func.TimerRequest) -> None:
    async with JediMaster(...) as jedi:
        report = await jedi.process_repositories([repo_full])
    
    async with JediMaster(...) as pr_jedi:
        pr_results = await pr_jedi.manage_pull_requests(repo_full)
```

## Benefits

1. **Native Framework Integration**: Uses Microsoft Agent Framework's ChatAgent for cleaner abstraction
2. **Proper Async**: Full async/await throughout for better I/O handling
3. **Resource Management**: Context managers ensure proper cleanup of credentials and clients
4. **Better Performance**: Potential for parallel processing of multiple repositories/issues
5. **Future-Proof**: Aligned with Microsoft's AI agent strategy

## Dependencies

### Added:
- `agent-framework` - Microsoft Agent Framework

### Retained:
- `openai>=1.0.0` - Still needed for embeddings (not yet in Agent Framework)
- `azure-identity>=1.15.0` - For DefaultAzureCredential (async version)

### Removed:
- Custom `azure_ai_foundry_utils.py` module

## Breaking Changes

### For Library Users:
```python
# Old way
jm = JediMaster(token, endpoint)
report = jm.process_repositories(repos)

# New way
async with JediMaster(token, endpoint) as jm:
    report = await jm.process_repositories(repos)
```

### For Azure Functions:
- All timer/HTTP triggered functions must be `async def`
- All calls to JediMaster methods must use `await`
- JediMaster must be used with `async with` context manager

## Testing

To test the migration:

```bash
# Install new dependencies
pip install -r requirements.txt

# Run CLI
python jedimaster.py --user <username>

# Test issue creation
python jedimaster.py --create-issues <owner/repo>
```

## Authentication

Authentication remains unchanged - uses Azure DefaultAzureCredential which supports:
- Managed Identity (for Azure deployments)
- Azure CLI (`az login` for local development)
- Environment variables
- Visual Studio authentication

No API keys required.

## Future Enhancements

1. **Parallel Processing**: Now that everything is async, could process multiple repos simultaneously
2. **Streaming**: Agent Framework supports streaming responses - could show real-time progress
3. **Agent Tools**: Could add custom tools/functions to agents for more sophisticated workflows
4. **Embeddings Migration**: Once Agent Framework supports embeddings, remove OpenAI client dependency
