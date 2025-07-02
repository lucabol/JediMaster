
# JediMaster 🤖

A Python tool and Azure Function for AI-powered evaluation and assignment of GitHub issues to GitHub Copilot.

---


## Features

- **AI Issue Evaluation**: Uses OpenAI GPT models to analyze GitHub issues for Copilot suitability.
- **Automated Assignment**: Assigns suitable issues to Copilot with labels and comments.
- **Automated PR Review**: Reviews open pull requests using AI (PRDeciderAgent) and can comment or mark PRs as ready to merge.
- **Multi-Repo & User Support**: Process issues and PRs for multiple repositories or all repos for a user.
- **Flexible Deployment**: Run as a standalone Python script or as Azure Functions (HTTP & Timer triggers).
- **Comprehensive Reporting**: Generates detailed JSON reports.
- **Robust Error Handling**: Handles API, network, and data errors gracefully.

---

## Installation

1. **Clone the repository:**
   ```bash
   git clone <repository-url>
   cd JediMaster
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```


   - `TIMER_USERNAME` (optional): Used by timer-triggered Azure Functions to specify the GitHub username whose repositories should be processed on schedule.
     - Example: `TIMER_USERNAME=github-username`
     - Only needed for timer-based automation; not required for manual/scripted runs.

   You can use a `.env` file or set them in your shell:
   ```bash
   # .env file (recommended for development)
   GITHUB_TOKEN=your_github_token
   OPENAI_API_KEY=your_openai_api_key

   ISSUE_ACTION=assign   # or label (default: label)
   TIMER_USERNAME=github-username
   TIMER_REPOS=owner/repo1,owner/repo2

   # Or set in your shell
   export GITHUB_TOKEN=your_github_token
   export OPENAI_API_KEY=your_openai_api_key
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
```

Process open pull requests for one or more repositories:
```bash
python jedimaster.py --process-prs owner/repo1 owner/repo2
```

Process open pull requests for all repositories for a user:
```bash
python jedimaster.py --user github-username --process-prs
```



**Options:**

- `--create-issues`         Use LLM to suggest and open new issues in the specified repositories. Prints the full LLM conversation (prompts and response) for transparency and debugging. Example:

  ```bash
  python jedimaster.py --create-issues owner/repo1 owner/repo2
  ```

- `--process-prs`           Process open pull requests using AI review (PRDeciderAgent)
- `--auto-merge-reviewed`   Automatically merge reviewed PRs with no conflicts
- `--just-label`            Only add labels to issues, do not assign them to Copilot
- `--use-file-filter`       Use .coding_agent file filtering instead of topic filtering (slower but backwards compatible)
- `-o, --output FILENAME`   Output filename for the report (default: auto-generated)
- `-v, --verbose`           Enable verbose logging
- `-h, --help`              Show help message

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
