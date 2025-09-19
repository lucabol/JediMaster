
# JediMaster 🤖

A Python tool and Azure Function for AI-powered evaluation and assignment of GitHub issues to GitHub Copilot.

---


## Features

- **AI Issue Evaluation**: Uses Azure AI Foundry models to analyze GitHub issues for Copilot suitability.
- **Automated Assignment**: Assigns suitable issues to Copilot with labels and comments.
- **Automated PR Review**: Reviews open pull requests using AI (PRDeciderAgent) and can comment or mark PRs as ready to merge.
- **Multi-Repo & User Support**: Process issues and PRs for multiple repositories or all repos for a user.
- **Flexible Deployment**: Run as a standalone Python script or as Azure Functions (HTTP & Timer triggers).
- **Library API & Helper Script**: Import and orchestrate directly in Python or use `example.py` for advanced scenarios (auto-merge, repo reset, seeding demo issues, CreatorAgent flows).
- **Comprehensive Reporting**: Generates detailed JSON reports.
- **Robust Error Handling**: Handles API, network, and data errors gracefully.

### Reset Endpoint (Azure Function)

An HTTP endpoint `/api/reset` is available (auth level `function`) that resets every repository listed in the `AUTOMATION_REPOS` environment variable. It performs the same destructive baseline reset logic as `example.py --reset-repo`:

Steps per repository:
- Close all open issues (excluding PRs)
- Close all open pull requests
- Delete all branches except `main`
- Restore baseline `hello.c` and `README.md`
- Delete other root-level files except: `hello.c`, `.gitignore`, `README.md`, and the `.github` directory

Example local invocation (PowerShell / curl):
```bash
curl -X POST -H "x-functions-key: <FUNCTION_KEY>" http://localhost:7071/api/reset
```
Response JSON returns a per-repository summary. Use cautiously; this is irreversible.

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
   - `AZURE_AI_FOUNDRY_ENDPOINT`: Your Azure AI Foundry project endpoint

   Optional Azure AI Foundry configuration:
   - `AZURE_AI_MODEL`: Azure AI Foundry model to use (default: `model-router`)
     - Examples: `model-router`, `gpt-4`, `gpt-4o`

   **Authentication**: The application uses managed authentication (DefaultAzureCredential) to authenticate with Azure AI Foundry. This supports:
   - Managed Identity (when running in Azure)
   - Azure CLI authentication (for local development)
   - Environment variables (AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, AZURE_TENANT_ID)
   - Visual Studio authentication
   - And other Azure credential sources

   Optional environment variables:
   - `ISSUE_ACTION` (optional): Set to 'assign' to assign issues to Copilot, or 'label' to only add labels (default: 'label')
   - `JUST_LABEL` (Functions Timer mode): Alternative flag used in Azure Function automation (`JUST_LABEL=1` means only labeling). Takes precedence over `ISSUE_ACTION` inside the automation Function.
   - `TIMER_USERNAME` (optional): Used by timer-triggered Azure Functions to specify the GitHub username whose repositories should be processed on schedule.
     - Example: `TIMER_USERNAME=github-username`
     - Only needed for timer-based automation; not required for manual/scripted runs.
   - `TIMER_REPOS` (optional): Comma-separated list of repositories for timer-triggered Azure Functions
     - Example: `TIMER_REPOS=owner/repo1,owner/repo2`
   - Automation-specific (see Azure Functions section): `AUTOMATION_REPOS`, `CREATE_ISSUES`, `CREATE_ISSUES_COUNT`, `PROCESS_PRS`, `AUTO_MERGE`, `USE_FILE_FILTER`, `SCHEDULE_CRON`.

   You can use a `.env` file or set them in your shell:
   ```bash
   # .env file (recommended for development)
   GITHUB_TOKEN=your_github_token
   AZURE_AI_FOUNDRY_ENDPOINT=https://your-project.cognitiveservices.azure.com/openai/deployments/model-router/chat/completions?api-version=2025-01-01-preview
   AZURE_AI_FOUNDRY_API_KEY=your_azure_ai_foundry_api_key
   AZURE_AI_MODEL=model-router  # Optional: defaults to model-router

   ISSUE_ACTION=assign   # or label (default: label)
   TIMER_USERNAME=github-username
   TIMER_REPOS=owner/repo1,owner/repo2

   # Or set in your shell
   export GITHUB_TOKEN=your_github_token
   export AZURE_AI_FOUNDRY_ENDPOINT=https://your-project.cognitiveservices.azure.com/openai/deployments/model-router/chat/completions?api-version=2025-01-01-preview
   export AZURE_AI_FOUNDRY_API_KEY=your_azure_ai_foundry_api_key
   export AZURE_AI_MODEL=model-router
   ```

---

## Usage


### 1. As a Python Script

Process issues for one or more repositories:
```bash
python jedimaster.py owner/repo1 owner/repo2
```

Process all repositories for a user:
```bash
python jedimaster.py --user github-username
# or using short form:
python jedimaster.py -u github-username
```

Process open pull requests for one or more repositories:
```bash
python jedimaster.py --process-prs owner/repo1 owner/repo2
```

Process open pull requests for all repositories for a user:
```bash
python jedimaster.py --user github-username --process-prs
# or using short form:
python jedimaster.py -u github-username --process-prs
```



**Options:**

- `--user, -u USERNAME`     GitHub username to process (will process repos with topic "managed-by-coding-agent" or .coding_agent file)
- `--output, -o FILENAME`   Output filename for the report (default: auto-generated)
- `--verbose, -v`           Enable verbose logging
- `--just-label`            Only add labels to issues, do not assign them to Copilot
- `--use-file-filter`       Use .coding_agent file filtering instead of topic filtering (slower but backwards compatible)
- `--process-prs`           Process open pull requests with PRDeciderAgent (add comments or log check-in readiness)
- `--auto-merge-reviewed`   Automatically merge reviewed PRs with no conflicts
- `--create-issues`         Use CreatorAgent to suggest and open new issues in the specified repositories

**CreatorAgent Example:**

Create new issues using AI suggestions:
```bash
python jedimaster.py --create-issues owner/repo1 owner/repo2
```

This option uses LLM to suggest and open new issues in the specified repositories. It prints the full LLM conversation (prompts and response) for transparency and debugging.

---

### 1b. Using the Library Programmatically

You can import and drive `JediMaster` directly:

```python
from jedimaster import JediMaster

jm = JediMaster(
  github_token="<token>",
  azure_foundry_endpoint="<azure_endpoint>",
  # No API key needed - uses managed authentication
  just_label=True,             # only label instead of assign
  use_topic_filter=True,       # or False to use .coding_agent file
  process_prs=False,
  auto_merge_reviewed=False,
)

report = jm.process_repositories(["owner/repo1", "owner/repo2"])
jm.print_summary(report)
jm.save_report(report)
```

---

### 1c. `example.py` Helper Script

The `example.py` script showcases extended workflows and test/demo utilities.

Key additional flags beyond `jedimaster.py`:

| Flag | Purpose |
|------|---------|
| `--assign` | Force assignment (overrides `--just-label`) |
| `--populate-issues` | Seed a demo repo with curated suitable/unsuitable issues |
| `--reset-repo` | Aggressively reset a demo repo: close issues/PRs, delete branches, restore baseline files |
| `--create-issues` | Invoke `CreatorAgent` to suggest & open issues |
| `--auto-merge-reviewed` | Only perform reviewed PR auto-merge pass |

Examples:

Populate a demo repo with synthetic issues:
```bash
python example.py --populate-issues lucabol/Hello-World
```

Reset the demo repo baseline:
```bash
python example.py --reset-repo lucabol/Hello-World
```

Run normal labeling (topic filter) on seeded repo:
```bash
python example.py lucabol/Hello-World --verbose
```

Auto-merge approved PRs only (skip issue processing):
```bash
python example.py --auto-merge-reviewed lucabol/Hello-World
```

Generate new issues via CreatorAgent:
```bash
python example.py --create-issues lucabol/Hello-World
```

NOTE: Some helper actions (reset/delete) assume you control the target repo and can modify branches & files.

---

### 2. As Azure Functions

#### HTTP Triggers

- **ProcessUser**:  
  `POST /api/ProcessUser`  
  Body: `{ "username": "github-username" }`  
  Or: `GET /api/ProcessUser?username=github-username`

- **ProcessRepos**:  
  `POST /api/ProcessRepos`  
  Body: `{ "repo_names": ["owner/repo1", "owner/repo2"] }`  
  Or: `GET /api/ProcessRepos?repo_names=owner/repo1,owner/repo2`

#### Timer Triggers

- **TimerProcessUser**:  
  Runs on schedule, processes all repos for the user specified in the `TIMER_USERNAME` environment variable.

- **TimerProcessRepos**:  
  Runs on schedule, processes repos listed in the `TIMER_REPOS` environment variable (comma-separated).

**Environment variables for timers:**
```bash
TIMER_USERNAME=github-username
TIMER_REPOS=owner/repo1,owner/repo2
```

#### Local Development

1. Install Azure Functions Core Tools and dependencies.
2. Start the function app:
   ```bash
   func start
   ```
3. Use tools like Postman, curl, or PowerShell to call the HTTP endpoints.

**Example PowerShell call:**
```powershell
$body = @{ username = "your-github-username" } | ConvertTo-Json
Invoke-RestMethod -Uri "http://localhost:7071/api/ProcessUser" -Method Post -Body $body -ContentType "application/json"
```

---

#### 2a. Automated Orchestration Function (`function_app.py` root)

The repository also includes a timer-driven automation Function (`AutomateRepos`) that performs a full cycle (optional issue creation, labeling/assignment, PR review, auto-merge) across a static list of repositories.

Configure via environment variables (e.g. in Azure portal or `local.settings.json` for local test):

| Variable | Description | Default |
|----------|-------------|---------|
| `AUTOMATION_REPOS` | Comma-separated `owner/repo` list to process | (required) |
| `CREATE_ISSUES` | `1`/`true` to enable `CreatorAgent` | `0` |
| `CREATE_ISSUES_COUNT` | Issues per repo when creation enabled | `3` |
| `PROCESS_PRS` | Enable PR review agent | `1` |
| `AUTO_MERGE` | Attempt merge of approved PRs | `1` |
| `JUST_LABEL` | Only label, do not assign | `1` (label-only) |
| `USE_FILE_FILTER` | Use `.coding_agent` file instead of topic | `0` |
| `SCHEDULE_CRON` | NCRONTAB expression for timer | every 6h |

Invocation log summary JSON is emitted at end of each run (Application Insights recommended).

---

#### 2b. Deployment Guidance (Azure)

Recommendations (Python Azure Functions best practices):

- Use Python Functions v2 programming model (this project uses `func.FunctionApp()` style) and host version v4.
- Deploy to Linux plan (Flex Consumption (FC1) preferred; fallback to Elastic Premium if needed).
- Keep secrets in Azure Function App settings or Key Vault references (avoid committing `.env`).
- Enable Application Insights for monitoring.
- Secure HTTP endpoints (default level `function`; add an API key or front door / APIM if exposed externally).
- For scheduled automation, adjust `SCHEDULE_CRON` without code changes.

Basic deployment steps (one-off manual path):

```bash
# Log in
az login

# (Optional) create resource group
az group create -n rg-jedimaster -l westus2

# Create a storage account (required for Functions)
az storage account create -g rg-jedimaster -n <uniqueStorageName> -l westus2 --sku Standard_LRS

# Create a Linux Flex Consumption Function App (preview naming may vary)
az functionapp plan create -g rg-jedimaster -n plan-jedimaster --flex-consumption --location westus2
az functionapp create -g rg-jedimaster -n jedimaster-func \
  --storage-account <uniqueStorageName> \
  --plan plan-jedimaster \
  --runtime python \
  --functions-version 4

# Configure required settings
az functionapp config appsettings set -g rg-jedimaster -n jedimaster-func --settings \
  GITHUB_TOKEN=*** OPENAI_API_KEY=*** AUT0MATION_REPOS=owner/repo1,owner/repo2 JUST_LABEL=1 PROCESS_PRS=1 AUTO_MERGE=1

# Deploy source (from repo root)
func azure functionapp publish jedimaster-func
```

For infrastructure-as-code, consider Azure Developer CLI (azd) with a Bicep template referencing a Flex Consumption plan + Application Insights + Storage.

Local test of automation timer (simulate run):
```bash
set AUT0MATION_REPOS=owner/repo1
func start
```

---


## Output

- **Console**: Real-time progress and summary.
- **JSON Report**: Detailed results, including assignment and PR review status, reasoning, and actions taken.

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
