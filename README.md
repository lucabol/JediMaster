# JediMaster 🤖

A Python tool that automatically evaluates GitHub issues and assigns suitable ones to GitHub Copilot using AI-powered decision making.

## Features

- **Intelligent Issue Evaluation**: Uses OpenAI's GPT models to analyze GitHub issues and determine if they're suitable for GitHub Copilot assistance
- **Automated Assignment**: Automatically assigns suitable issues to GitHub Copilot by adding labels and comments
- **Comprehensive Reporting**: Generates detailed reports of all processed issues with reasoning
- **Multi-Repository Support**: Can process issues from multiple GitHub repositories in a single run
- **Error Handling**: Robust error handling with detailed logging and error reporting

## Installation

1. Clone the repository:
```bash
git clone <repository-url>
cd JediMaster
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Set up environment variables:
```bash
cp .env.example .env
# Edit .env with your API keys
```

## Configuration

You need to set up the following environment variables:

- `GITHUB_TOKEN`: GitHub Personal Access Token with `repo` permissions
- `OPENAI_API_KEY`: OpenAI API key for GPT access

### Environment Variable Setup

You can configure the API keys in two ways:

#### Option 1: Using .env file (Recommended for development)
```bash
cp .env.example .env
# Edit .env with your API keys
```

#### Option 2: System Environment Variables (Recommended for production)
```bash
# Windows (PowerShell)
$env:GITHUB_TOKEN="your_github_token_here"
$env:OPENAI_API_KEY="your_openai_api_key_here"

# Windows (Command Prompt)
set GITHUB_TOKEN=your_github_token_here
set OPENAI_API_KEY=your_openai_api_key_here

# Linux/macOS
export GITHUB_TOKEN=your_github_token_here
export OPENAI_API_KEY=your_openai_api_key_here
```

The tool will automatically check for API keys in both locations, with system environment variables taking precedence over .env file settings.

### GitHub Token Setup

1. Go to GitHub Settings → Developer settings → Personal access tokens → Tokens (classic)
2. Generate a new token with `repo` scope
3. Add the token to your `.env` file or set as system environment variable

### OpenAI API Key Setup

1. Go to [OpenAI API Keys](https://platform.openai.com/api-keys)
2. Create a new API key
3. Add the key to your `.env` file or set as system environment variable

## Usage

### Basic Usage

Process issues from one or more repositories:

```bash
python jedimaster.py owner/repo1 owner/repo2
```

### Command Line Options

```bash
python jedimaster.py [OPTIONS] REPOSITORIES...

Arguments:
  REPOSITORIES    GitHub repositories to process (format: owner/repo)

Options:
  -o, --output FILENAME    Output filename for the report (default: auto-generated)
  -v, --verbose           Enable verbose logging
  -h, --help              Show help message
```

### Examples

```bash
# Process a single repository
python jedimaster.py microsoft/vscode

# Process multiple repositories with custom output
python jedimaster.py microsoft/vscode github/copilot -o my_report.json

# Enable verbose logging
python jedimaster.py microsoft/vscode -v
```

## How It Works

1. **Issue Fetching**: Retrieves all open issues from specified GitHub repositories
2. **AI Evaluation**: Uses an LLM-powered "Decider Agent" to evaluate each issue:
   - Analyzes issue title, description, labels, and recent comments
   - Determines if the issue involves concrete coding tasks suitable for Copilot
   - Provides detailed reasoning for the decision
3. **Assignment**: For suitable issues:
   - Adds a "github-copilot" label
   - Posts a comment indicating AI assignment
   - Skips issues already assigned to Copilot
4. **Reporting**: Generates a comprehensive JSON report with:
   - Summary statistics
   - Individual issue results with reasoning
   - Error details for any failed operations

## Issue Suitability Criteria

The AI evaluates issues based on whether they involve tasks that GitHub Copilot excels at:

### ✅ Suitable Issues
- Code generation and implementation
- Bug fixes and debugging
- Code refactoring and optimization
- Adding tests and documentation
- API integration tasks
- Configuration and setup scripts

### ❌ Not Suitable Issues
- Pure discussion or planning
- UX/UI design decisions
- Architecture and strategy discussions
- Community management
- Requirement gathering
- Non-technical issues

## Output

### Console Output
The tool provides real-time progress updates and a summary report:

```
Processing 2 repositories...
==============================================================
JEDIMASTER PROCESSING SUMMARY
==============================================================
Timestamp: 2025-06-13T10:30:00
Total Issues Processed: 15
Assigned to Copilot: 8
Not Assigned: 5
Already Assigned: 1
Errors: 1
==============================================================
```

### JSON Report
Detailed JSON report saved to file containing:

```json
{
  "total_issues": 15,
  "assigned": 8,
  "not_assigned": 5,
  "already_assigned": 1,
  "errors": 1,
  "results": [
    {
      "repo": "owner/repo",
      "issue_number": 123,
      "title": "Add error handling to API client",
      "url": "https://github.com/owner/repo/issues/123",
      "status": "assigned",
      "reasoning": "This issue involves implementing concrete error handling code, which is perfect for GitHub Copilot assistance..."
    }
  ],
  "timestamp": "2025-06-13T10:30:00.123456"
}
```

## Error Handling

The tool includes comprehensive error handling:

- **API Rate Limits**: Respects GitHub and OpenAI API rate limits
- **Network Issues**: Retries on temporary network failures
- **Authentication Errors**: Clear error messages for invalid tokens
- **Repository Access**: Handles private/inaccessible repositories gracefully
- **Malformed Issues**: Processes issues with missing or invalid data

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## License

[Add your license here]

## Support

For issues and questions:
1. Check the existing GitHub issues
2. Create a new issue with detailed information
3. Include logs with `--verbose` flag when reporting bugs
