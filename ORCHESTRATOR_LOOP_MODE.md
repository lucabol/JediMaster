# Orchestrator Loop Mode

## Feature: `--loop N` Parameter

Added continuous orchestration capability to run the orchestrator in a loop, checking repositories at regular intervals.

**Design Note**: Loop mode is specifically designed for orchestration (`--orchestrate` flag required) because orchestration is the autonomous, strategic mode meant for continuous operation. Other modes (issue processing, PR management) are typically run ad-hoc when needed, not continuously.

## Usage

**Important**: `--loop` requires `--orchestrate` to be specified.

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

### Why Only Orchestration?

Loop mode is **specifically designed for orchestration** because:

**Orchestration is autonomous and strategic**:
- LLM decides what workflows to run based on repository state
- Adapts to changing conditions (PR backlog, Copilot capacity, rate limits)
- Makes intelligent decisions about resource allocation
- Designed for "set it and forget it" continuous operation

**Other modes are more manual/tactical**:
- `--manage-prs`: Process specific PRs (typically run when PRs exist)
- Normal mode: Process issues (typically run when issues are created)
- `--auto-merge-reviewed`: Merge specific PRs (targeted operation)
- These are typically triggered by events, not run continuously

**Continuous operation requires intelligence**:
- Running issue processing in a loop would blindly reassign issues every N minutes
- Running PR management in a loop could conflict with Copilot's work
- Orchestration has the intelligence to skip unnecessary work and avoid conflicts

If you need continuous operation for other modes, you can use cron jobs or systemd timers for more control.

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
- **Missing --orchestrate**: Exits with error if `--loop` used without `--orchestrate`

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

⚠️ **Requires --orchestrate**: `--loop` only works with `--orchestrate` flag (by design - see "Why Only Orchestration?" section)
⚠️ **Long-running process**: May need process management (systemd, Docker)
⚠️ **Single instance**: Don't run multiple loops on same repo (conflicts)
⚠️ **Memory**: Long-running processes may accumulate memory (restart periodically)
⚠️ **Rate limits**: Very short intervals may hit GitHub API rate limits

### Alternative: Looping Other Modes

If you need to run other modes continuously, use cron or systemd timers:

**Cron example** (process issues every 30 minutes):
```bash
# Add to crontab (crontab -e)
*/30 * * * * cd /path/to/JediMaster && python example.py myrepo >> /var/log/jedimaster.log 2>&1
```

**Systemd timer example** (process PRs hourly):
```ini
# /etc/systemd/system/jedimaster.timer
[Unit]
Description=JediMaster PR Processing Timer

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
```

**Why use cron/systemd instead of --loop?**
- Better process management (restart on failure, logging, monitoring)
- More flexible scheduling (different intervals for different modes)
- System integration (notifications, alerts, dependencies)
- Resource isolation (each run is fresh, no memory accumulation)

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

# Error: --loop without --orchestrate
python example.py lucabol/Hello-World --loop
# Output:
# Error: --loop requires --orchestrate flag
# 
# Reason: Loop mode is designed for continuous autonomous orchestration.
# For one-time operations, run the command without --loop.
# 
# Usage: python example.py <repo> --orchestrate --loop [MINUTES]
# 
# Examples:
#   python example.py myrepo --orchestrate --loop        # Loop every 30 min
#   python example.py myrepo --orchestrate --loop 15     # Loop every 15 min
#   python example.py myrepo --orchestrate               # Run once (no loop)
```

## Summary

The `--loop N` parameter enables continuous orchestration with:
- **Flexible intervals** (default 30 minutes)
- **Clean operation** (timestamps, iteration tracking)
- **Graceful shutdown** (Ctrl+C handling)
- **Production-ready** (error resilient, clear feedback)

Perfect for running the orchestrator as a daemon to continuously manage repository health!
