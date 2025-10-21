# Orchestrator Loop Mode

## Feature: `--loop N` Parameter

Added continuous orchestration capability to run the orchestrator in a loop, checking repositories at regular intervals.

## Usage

```bash
# Run orchestrator in a loop with default 30-minute interval
python example.py <repo> --orchestrate --loop

# Run orchestrator every 15 minutes
python example.py <repo> --orchestrate --loop 15

# Run orchestrator every 60 minutes (1 hour)
python example.py <repo> --orchestrate --loop 60

# Run orchestrator on multiple repos every 45 minutes
python example.py repo1/name repo2/name --orchestrate --loop 45

# Enable issue creation in loop mode
python example.py <repo> --orchestrate --loop 30 --enable-issue-creation
```

## Behavior

### Default Interval
- If `--loop` is specified without a value: **30 minutes**
- Minimum interval: **1 minute**

### Loop Operation

**Each iteration:**
1. Displays iteration number and current UTC timestamp
2. Runs orchestrated workflow on all specified repositories
3. Prints orchestration report for each repository
4. Calculates next run time
5. Displays sleep message with next run time
6. Sleeps for the specified interval
7. Repeats indefinitely

**Stopping:**
- Press `Ctrl+C` to gracefully stop the loop
- Shows total iterations completed

### Output Format

**Loop Mode:**
```
[Orchestrator] Running in LOOP mode: checking every 30 minutes
[Orchestrator] Press Ctrl+C to stop
[Orchestrator] Issue creation DISABLED (use --enable-issue-creation to enable)

================================================================================
[Orchestrator] Iteration #1 at 2025-10-21 15:57:00 UTC
================================================================================

--- Orchestrating: owner/repo ---
<orchestration report>

================================================================================
[Orchestrator] Iteration #1 complete
[Orchestrator] Next run at: 2025-10-21 16:27:00 UTC
[Orchestrator] Sleeping for 30 minutes... (Ctrl+C to stop)
================================================================================

<30 minutes later>

================================================================================
[Orchestrator] Iteration #2 at 2025-10-21 16:27:00 UTC
================================================================================
...
```

**Single Run (no loop):**
```
[Orchestrator] Running intelligent orchestration on: ['owner/repo']
[Orchestrator] Issue creation DISABLED (use --enable-issue-creation to enable)

================================================================================
Orchestrating: owner/repo
================================================================================
<orchestration report>
```

## Use Cases

### 1. Continuous Monitoring
Run the orchestrator continuously to monitor and manage repository health:

```bash
# Check every 30 minutes (default)
python example.py myorg/myrepo --orchestrate --loop
```

### 2. Frequent Updates
For active repositories that need frequent attention:

```bash
# Check every 10 minutes
python example.py myorg/active-repo --orchestrate --loop 10
```

### 3. Batch Processing
Process multiple repositories at regular intervals:

```bash
# Check 5 repos every hour
python example.py \
  org/repo1 \
  org/repo2 \
  org/repo3 \
  org/repo4 \
  org/repo5 \
  --orchestrate --loop 60
```

### 4. Production Daemon
Run as a long-lived process (consider using systemd, supervisor, or Docker):

```bash
# Run indefinitely with 30-minute checks
nohup python example.py org/production-repo --orchestrate --loop 30 > orchestrator.log 2>&1 &
```

### 5. Issue Creation Loop
Continuously create and manage issues:

```bash
# Check every 2 hours and allow issue creation
python example.py org/repo --orchestrate --loop 120 --enable-issue-creation
```

## Implementation Details

### Timing
- Uses `asyncio.sleep()` for non-blocking delays
- Sleep duration: `N * 60` seconds (converts minutes to seconds)
- Next run time calculated and displayed before sleeping

### Timestamp Handling
- All timestamps shown in **UTC**
- Format: `YYYY-MM-DD HH:MM:SS UTC`
- Next run time normalized to start of minute (seconds=0, microseconds=0)

### Error Handling
- **Ctrl+C (KeyboardInterrupt)**: Gracefully stops loop and shows iteration count
- **Repository errors**: Logged but don't stop the loop (continues to next iteration)
- **Invalid interval**: Exits with error message if `N < 1`

### Iteration Tracking
- Counter starts at 1
- Increments before each run
- Displayed in reports and exit message

## Code Structure

**Added to `example.py`:**

1. **Import**: `from datetime import datetime, timezone`
2. **Argument**: `--loop` with optional integer value (default: 30)
3. **Loop logic**: Wraps orchestrate section in `while True` loop
4. **Sleep mechanism**: `await asyncio.sleep(loop_minutes * 60)`
5. **Graceful exit**: `KeyboardInterrupt` handler

## Examples

### Quick Test (1 minute interval)
```bash
# Test loop functionality
python example.py lucabol/Hello-World --orchestrate --loop 1

# Output:
[Orchestrator] Running in LOOP mode: checking every 1 minutes
[Orchestrator] Press Ctrl+C to stop
...
[Orchestrator] Sleeping for 1 minutes... (Ctrl+C to stop)
# <wait 1 minute>
# <runs again>
# Press Ctrl+C
[Orchestrator] Loop stopped by user (Ctrl+C)
[Orchestrator] Completed 2 iteration(s)
```

### Production Usage (30 minutes)
```bash
# Recommended for production
python example.py myorg/production-repo --orchestrate --loop 30

# Runs continuously:
# - Iteration every 30 minutes
# - Manages PRs and issues
# - Strategic workflow selection
# - Rate limit aware
```

### Development Monitoring (5 minutes)
```bash
# Fast feedback during development
python example.py myorg/dev-repo --orchestrate --loop 5

# Frequent checks:
# - Quick PR review cycles
# - Fast issue assignment
# - Responsive to changes
```

## Advantages

✅ **Continuous Operation**: Set it and forget it
✅ **Configurable Interval**: Adapt to repository activity level
✅ **Graceful Shutdown**: Clean exit with Ctrl+C
✅ **Multiple Repos**: Process several repositories per iteration
✅ **Clear Feedback**: Timestamps and iteration tracking
✅ **Error Resilient**: Continues on repository-level errors
✅ **Resource Efficient**: Async sleep doesn't block

## Limitations

⚠️ **Long-running process**: May need process management (systemd, Docker)
⚠️ **Single instance**: Don't run multiple loops on same repo (conflicts)
⚠️ **Memory**: Long-running processes may accumulate memory (restart periodically)
⚠️ **Rate limits**: Very short intervals may hit GitHub API rate limits

## Best Practices

1. **Choose appropriate interval**:
   - Active repos: 10-15 minutes
   - Normal repos: 30 minutes (default)
   - Low-activity repos: 60-120 minutes

2. **Use process management**:
   ```bash
   # systemd service
   # supervisor config
   # Docker container with restart policy
   ```

3. **Monitor logs**:
   ```bash
   # Redirect output
   python example.py repo --orchestrate --loop 30 > orchestrator.log 2>&1
   
   # Use journald with systemd
   # Use log rotation
   ```

4. **Consider rate limits**:
   - GitHub API: 5,000 requests/hour (authenticated)
   - LLM rate limits (Copilot/OpenAI)
   - Adjust interval based on repository count and activity

5. **Graceful restarts**:
   ```bash
   # Send Ctrl+C to gracefully stop
   # Then restart with updated code/config
   ```

## Testing

```bash
# Test with 1-minute interval
python example.py lucabol/Hello-World --orchestrate --loop 1

# Expected output:
# - Immediate first run
# - Sleep message with next run time
# - Runs again after 1 minute
# - Ctrl+C to stop
```

## Summary

The `--loop N` parameter enables continuous orchestration with:
- **Flexible intervals** (default 30 minutes)
- **Clean operation** (timestamps, iteration tracking)
- **Graceful shutdown** (Ctrl+C handling)
- **Production-ready** (error resilient, clear feedback)

Perfect for running the orchestrator as a daemon to continuously manage repository health!
