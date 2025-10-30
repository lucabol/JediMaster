# Logging Simplification Summary

## Overview
Simplified logging to show only actionable information about what the program did to each PR and issue, removing verbose debug/info messages.

## Changes Made

### PR Processing Output
Each PR now prints a single line showing:
- PR number
- Title (truncated to 60 chars)
- Action taken

**Examples:**
```
Processing 12 open PRs:
  PR #123: Fix authentication bug in login module → Merged (closed issues: [45, 67])
  PR #124: Add new feature for user profiles → Changes requested
  PR #125: Update documentation → Copilot working
  PR #126: Refactor database queries → Skipped (Copilot not assigned)
  PR #127: Fix typo in README → Needs human review
```

### Issue Processing Output
Each issue now prints a single line showing:
- Issue number
- Title (truncated to 60 chars)
- Decision/action

**Examples:**
```
Processing 8 unprocessed issues:
  Issue #45: Add support for OAuth2 authentication → Assigned to Copilot
  Issue #46: Bug: Login fails on mobile → Assigned to Copilot
  Issue #47: Documentation typo in README → Not suitable for Copilot
  Issue #48: Update dependencies → Labeled (suitable for Copilot)
  Issue #49: Invalid feature request → Not suitable for Copilot
```

### Workflow Summary
The simplified workflow now shows:
```
================================================================================
Starting workflow for owner/repo
Max concurrent Copilot assignments: 10
================================================================================

Step 1/2: Processing pull requests...
Processing 12 open PRs:
  [PR output lines...]

Copilot actively working on 3/10 PRs

Step 2/2: Processing issues (up to 7 assignments available)...
Processing 8 unprocessed issues:
  [Issue output lines...]

================================================================================
Workflow complete:
  - 12 PRs processed
  - 5 issues assigned to Copilot
  - Duration: 45.3s
================================================================================
```

### Verbose Mode
When `--verbose` flag is used:
- All original detailed logging is preserved
- Includes timestamps, file paths, line numbers
- Shows full error stack traces
- Displays detailed API interactions

Without `--verbose`:
- Only essential action summaries are shown
- Errors show simplified messages
- No debug information

## Modified Functions

### jedimaster.py
1. **`_process_pr_state_machine()`** - Added single-line PR status output
2. **`_review_and_act_on_pr()`** - Added action result output
3. **`_merge_pr()`** - Added merge confirmation output
4. **`process_issue()`** - Added single-line issue decision output
5. **`manage_pull_requests()`** - Added header for PR processing
6. **`fetch_issues()`** - Added header for issue processing
7. **`run_simplified_workflow()`** - Replaced all logger calls with print statements
8. **`_assign_issue_via_graphql()`** - Made verbose-only
9. **`_get_copilot_work_status()`** - Removed debug logging

### Logging Strategy
- **Default (non-verbose)**: Only show what was done (actions taken)
- **Verbose**: Show everything including internal decisions and errors
- **User-facing output**: Use `print()` for essential information
- **Debug output**: Use `self.logger.*()` guarded by `if self.verbose`

## Benefits
1. **Cleaner output** - Easy to scan what happened
2. **Action-focused** - Shows decisions and outcomes, not process
3. **Scalable** - Works for 1 or 100 PRs/issues
4. **Debuggable** - Verbose mode still available when needed
5. **User-friendly** - No jargon or internal state info in default output

## Testing
To see the new simplified output:
```bash
# Default simplified output
python example.py --orchestrate

# Verbose detailed output
python example.py --orchestrate --verbose
```
