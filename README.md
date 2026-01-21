
# JediMaster 🤖

An AI-powered GitHub repository orchestrator that automatically manages issues and pull requests using GitHub Copilot.

---


## Features

- **🔄 Intelligent Orchestration**: Continuously monitors and manages repository health with automated workflows
- **🤖 AI Issue Evaluation**: Uses Azure AI Foundry models to analyze GitHub issues for Copilot suitability
- **✅ Automated Assignment**: Assigns suitable issues to Copilot with labels and comments
- **📝 Smart PR Review**: Reviews open pull requests using AI and automatically merges when appropriate
- **🔁 Continuous Loop Mode**: Runs continuously, checking repositories at regular intervals
- **🎯 Capacity Management**: Intelligently limits concurrent Copilot assignments to prevent overload
- **🏥 Repository Health Tracking**: Monitors stuck PRs, escalates issues, and ensures forward progress
- **📊 Comprehensive Reporting**: Provides detailed visibility into workflow operations
- **🛡️ Robust Error Handling**: Handles API rate limits, network issues, and errors gracefully

---

## Installation

1. **Clone the repository:**
   ```bash
   git clone https://github.com/lucabol/JediMaster.git
   cd JediMaster
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Set up environment variables:**

   Required environment variables:
   - `GITHUB_TOKEN`: Your GitHub personal access token with repo permissions
   - `AZURE_AI_FOUNDRY_ENDPOINT`: Your Azure AI Foundry endpoint for chat completions
   - `AZURE_AI_FOUNDRY_PROJECT_ENDPOINT`: Your Azure AI Foundry project endpoint for agents

   Optional configuration:
   - `MAX_COPILOT_SLOTS`: Maximum concurrent Copilot assignments (default: 10)
   - `MAX_COMMENTS`: Maximum PR comments before escalating to human (default: 35)
   - `CREATE_ISSUES`: Enable AI-powered issue creation (0=disabled, 1=enabled, default: 0)
   - `CREATE_ISSUES_COUNT`: Number of issues to create per repository (default: 3)
   - `SIMILARITY_THRESHOLD`: Duplicate detection threshold when creating issues (0.0-1.0, default: 0.85)
   - `SKIP_PR_REVIEWS`: Skip AI review and merge PRs directly (0=disabled, 1=enabled, default: 0)
   - `ISSUE_ACTION`: How to handle suitable issues - `assign` (assign to Copilot) or `label` (only add labels)
   - `MERGE_MAX_RETRIES`: Maximum merge retry attempts before giving up (default: 5)

   **Authentication**: The application uses **DefaultAzureCredential** for Azure AI Foundry authentication, which supports:
   - Azure CLI authentication (recommended for local development - run `az login`)
   - Managed Identity (for Azure deployments)
   - Environment variables (AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID)
   - Visual Studio authentication
   - And other Azure credential sources

   Create a `.env` file in the project root (see `.env.example` for all options):
   ```bash
   # .env file (recommended)
   GITHUB_TOKEN=your_github_token
   AZURE_AI_FOUNDRY_ENDPOINT=https://your-project.cognitiveservices.azure.com/openai/deployments/model-router/chat/completions?api-version=2025-01-01-preview
   AZURE_AI_FOUNDRY_PROJECT_ENDPOINT=https://your-project.services.ai.azure.com/api/projects/YourProject
   
   # Optional settings
   MAX_COMMENTS=35
   CREATE_ISSUES=0  # Set to 1 to enable AI issue creation
   ```

---

## Usage

### Recommended: Orchestrate Mode with Loop

The best way to run JediMaster is in **orchestrate mode with continuous loop**, which intelligently manages your repositories:

```bash
python example.py --orchestrate --loop 20
```

This will:
- ✅ **Process all pull requests** - review, merge, or escalate as needed
- ✅ **Assign issues to Copilot** - up to the configured capacity limit
- ✅ **Track repository health** - monitor stuck PRs and forward progress
- ✅ **Run continuously** - check every 20 minutes (configurable)
- ✅ **Auto-stop when done** - stops when all work is complete or needs human review

**Command-line options for orchestrate mode:**
```bash
# Run once and exit
python example.py --orchestrate

# Run continuously, checking every N minutes
python example.py --orchestrate --loop 20

# Enable AI-powered issue creation
python example.py --orchestrate --loop 20 --create-issues

# Process specific repositories
python example.py --orchestrate --loop 20 owner/repo1 owner/repo2

# Process all repos for a user (with topic "managed-by-coding-agent")
python example.py --orchestrate --loop 20 --user github-username
```

The orchestrator will:
1. **Process PRs first** (higher priority):
   - Skip PRs already needing human review
   - Skip PRs where Copilot is actively working
   - Review PRs and either merge or request changes
   - Track Copilot capacity to avoid overload
   
2. **Assign issues to Copilot** (with remaining capacity):
   - Only assigns issues up to `MAX_COPILOT_SLOTS` limit
   - Prioritizes PRs over new issue assignments
   
3. **Monitor health and auto-stop**:
   - Stops when all PRs need human review and no issues to assign
   - Provides detailed summary of each iteration

---

### Alternative: Manual Modes

You can also run specific workflows manually:

#### Process issues for repositories:
```bash
python jedimaster.py owner/repo1 owner/repo2
```

#### Process all repositories for a user:
```bash
python jedimaster.py --user github-username
```

#### Process pull requests:
```bash
python jedimaster.py --manage-prs owner/repo1 owner/repo2
```


**Available command-line options:**

- `--orchestrate`           Run intelligent orchestration workflow (recommended)
- `--loop MINUTES`          Run continuously, checking every N minutes
- `--create-issues`         Enable AI-powered issue creation
- `--create-issues-count N` Number of issues to create per repo (default: 3)
- `--similarity-threshold`  Duplicate detection threshold (0.0-1.0, enables OpenAI embeddings)
- `--user, -u USERNAME`     Process repos for a GitHub user (with topic "managed-by-coding-agent")
- `--verbose, -v`           Enable verbose logging
- `--output, -o FILENAME`   Output filename for the report
- `--save-report`           Save detailed report to JSON file
- `--use-file-filter`       Use .coding_agent file filtering instead of topic filtering

**Legacy options (for manual workflows):**
- `--manage-prs`            Process open pull requests through state machine
- `--just-label`            Only add labels to issues, do not assign them
- `--assign`                Assign issues to Copilot (overrides --just-label)
- `--populate-issues`       Seed a demo repo with test issues
- `--reset-repo`            Reset a demo repo (closes all issues/PRs, deletes branches)

---

### Example Workflows

**Start continuous orchestration:**
```bash
# Check every 30 minutes
python example.py --orchestrate --loop 30

# With AI issue creation enabled
python example.py --orchestrate --loop 30 --create-issues

# For specific repos
python example.py --orchestrate --loop 30 owner/repo1 owner/repo2

# For all user repos with topic "managed-by-coding-agent"
python example.py --orchestrate --loop 30 --user myusername
```

**One-time operations:**
```bash
# Single orchestration pass (run once)
python example.py --orchestrate owner/repo1

# Create AI-suggested issues
python example.py --create-issues owner/repo1

# Process only PRs (no issue assignment)
python example.py --manage-prs owner/repo1
```

**Demo/test operations:**
```bash
# Populate demo repo with test issues
python example.py --populate-issues lucabol/Hello-World

# Reset demo repo to baseline
python example.py --reset-repo lucabol/Hello-World
```

---

### Using as a Library

You can also import and use JediMaster programmatically:

```python
import asyncio
from jedimaster import JediMaster

async def main():
    async with JediMaster(
        github_token="<token>",
        azure_foundry_endpoint="<chat_endpoint>",
        azure_foundry_project_endpoint="<project_endpoint>",
        # Uses DefaultAzureCredential (no API key needed)
    ) as jm:
        # Run orchestration workflow
        report = await jm.run_simplified_workflow("owner/repo1")
        
        print(f"Success: {report['success']}")
        print(f"PRs processed: {report['prs_processed']}")
        print(f"Issues assigned: {report['issues_assigned']}")

asyncio.run(main())
```

---

## How It Works

JediMaster uses a sophisticated workflow to manage repositories:

### Orchestration Flow

1. **PR Processing (Priority 1)**:
   - Fetches all open PRs
   - Skips PRs already escalated to humans (`copilot-human-review` label)
   - Skips PRs where Copilot is actively working
   - For remaining PRs:
     - Reviews using PRDeciderAgent (AI-powered review)
     - If approved and mergeable → merges automatically
     - If approved but merge conflicts → asks Copilot to fix (with detailed diff)
     - If changes needed → requests changes and reassigns to Copilot
     - If too many comments (>MAX_COMMENTS) → escalates to human review

2. **Issue Assignment (Priority 2)**:
   - Evaluates unprocessed issues using IssueDeciderAgent
   - Assigns suitable issues to Copilot
   - Labels unsuitable issues

3. **Capacity Management**:
   - Tracks active Copilot assignments across PRs and issues
   - Limits concurrent work to prevent overload (hardcoded to 10 slots)
   - Prioritizes PR review over new issue assignments

4. **Auto-Stop Criteria**:
   - All PRs need human review AND
   - No issues available to assign
   - Prevents unnecessary API calls

### Key Agents

- **PRDeciderAgent**: Reviews PRs and decides whether to approve or request changes
- **IssueDeciderAgent**: Evaluates issues for Copilot suitability
- **CreatorAgent**: Suggests new issues based on repository analysis (when enabled)

---

## Azure Functions Deployment

JediMaster can be deployed as an Azure Function for automated, scheduled repository management.

### Prerequisites

- Azure CLI (`az`) installed and logged in
- Azure Functions Core Tools (`func`) installed
- An existing Azure Function App (Python)
- Azure AI Foundry resource with managed identity access

### Deployment

1. **Configure `.env`** with deployment settings:
   ```bash
   # Azure deployment configuration (REQUIRED)
   RESOURCE_GROUP=your-resource-group
   FUNCTION_APP_NAME=your-function-app-name
   
   # AI resource for managed identity role assignment
   AI_RESOURCE_GROUP=your-ai-resource-group
   
   # Timer schedule (Azure Functions CRON format)
   SCHEDULE_CRON=0 */30 * * * *  # Every 30 minutes
   
   # Repositories to process
   AUTOMATION_REPOS=owner/repo1,owner/repo2
   
   # Processing flags
   PROCESS_PRS=1
   AUTO_MERGE=1
   JUST_LABEL=0
   CREATE_ISSUES=0
   ```

2. **Deploy using the script**:
   ```powershell
   pwsh ./deploy_existing.ps1
   ```

   The script will:
   - Enable system-assigned managed identity
   - Configure Cognitive Services User role for AI access
   - Apply all settings from `.env`
   - Deploy the function code

### Azure Function Environment Variables

Additional variables for Azure Functions (see `.env.example` for complete list):

| Variable | Description | Default |
|----------|-------------|---------|
| `SCHEDULE_CRON` | Timer trigger schedule (CRON format) | `0 */30 * * * *` |
| `AUTOMATION_REPOS` | Comma-separated list of repos | - |
| `PROCESS_PRS` | Enable PR processing | `1` |
| `AUTO_MERGE` | Enable auto-merge of approved PRs | `1` |
| `JUST_LABEL` | Only label issues, don't assign | `0` |
| `USE_FILE_FILTER` | Use .coding_agent file filtering | `0` |
| `BATCH_SIZE` | Items to process per batch | `5` |
| `RATE_LIMIT_DELAY` | Delay between API calls (seconds) | `2.0` |

---

## Output

- **Console**: Real-time progress with summary statistics
- **Continuous monitoring**: Shows iteration number, timestamp, and next check time
- **Detailed logging**: Per-PR and per-issue status with reasons
- **JSON Report**: Available with `--save-report` flag

Example output:
```
================================================================================
[SimplifiedWorkflow] Iteration #5 at 2025-11-07 10:30:00 UTC
================================================================================

--- Processing: owner/repo1 ---

Step 1/2: Processing pull requests...

Found 15 open PRs (3 need human review, 12 processed):
  PR #123: Fix authentication bug -> Merged
  PR #124: Add new feature -> Changes requested
  PR #125: Update documentation -> Copilot working

Copilot actively working on 3/10 PRs

Step 2/2: Processing issues (up to 7 assignments available)...

Processing 5 unprocessed issues:
  Issue #45: Add error handling -> Assigned to Copilot
  Issue #46: Fix typo -> Assigned to Copilot
  Issue #47: Complex refactoring -> Not suitable for Copilot

================================================================================
Workflow complete:
  - 15 PRs processed
  - 2 issues assigned to Copilot
  - Duration: 45.2s
================================================================================

[SimplifiedWorkflow] Next run at: 2025-11-07 10:50:00 UTC
```

---

## Error Handling

- Handles API rate limits, network issues, authentication errors, and invalid data.
- Logs errors and provides clear messages for troubleshooting.

---

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes and add tests
4. Submit a pull request

---


## License

This project is licensed under the [MIT License](LICENSE).

---

## Support

- Check existing GitHub issues
- Create a new issue with details and logs (use `--verbose` for more info)

---
