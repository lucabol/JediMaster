# Merge Conflict Diff Improvement

## Summary
Improved the merge conflict handling in JediMaster to provide Copilot with structured, comprehensive diff information when a PR fails to merge.

## Changes Made

### 1. Enhanced `_fetch_pr_diff_with_base_versions()` Function

**Location:** `jedimaster.py` lines 2170-2258

**Improvements:**
- Creates a structured, easy-to-read diff format for each modified file
- Includes file metadata (name, status, additions/deletions count)
- Provides the standard unified diff (patch) showing exact changes
- Includes base branch context (first 150 lines) for reference
- Handles binary files and new files gracefully
- Better error handling for missing base files

**Format Example:**
```
================================================================================
File: tools/code/common/Api.cs
Status: modified (+13 -2)
================================================================================

Changes in this PR:
```diff
@@ -639,9 +639,20 @@ private static async ValueTask PutNonSoapApi(...)
         // Old line
-        await pipeline.PutContent(uriWithImport, dto, cancellationToken);
+        // New line
+        await pipeline.PutContent(uriWithImport, modelForImport, cancellationToken);
```

Base branch (main) version (first 150 of 766 lines):
```
using Azure;
using Azure.Core;
...
```
```

### 2. Updated `_merge_pr()` Function

**Location:** `jedimaster.py` lines 1064-1076

**Changes:**
- Simplified to use only `diff_content` (base versions now included)
- Increased size limit from 3000 to 10000 chars to accommodate structured format
- Cleaner error handling

### 3. Updated Comment Generation

**Location:** `jedimaster.py` lines 1088-1099 (escalation) and 1139-1152 (retry)

**Improvements:**
- Cleaner comment format with structured diff
- Removed redundant base_versions field (now in diff)
- Better labeling: "Merge conflict details (including base branch context)"
- More actionable instructions for Copilot

## Benefits

1. **Better Context:** Copilot gets both the changes and the base branch version in one structured format
2. **Clearer Conflicts:** The unified diff format clearly shows what changed
3. **Easier Resolution:** Having base branch context helps Copilot understand the merge conflict better
4. **Consistent Format:** All merge conflict information is presented consistently
5. **Size Optimization:** Smart truncation prevents overly large comments while preserving critical information

## Testing

Tested with PR #1981 from gim-home/JediTestRepoV3 which had merge conflicts.
The output shows:
- 2 files modified
- Clear diffs for each file
- Base branch context included
- Total size: 14,038 characters (well within GitHub comment limits)

## Example Output

See `merge_diff_output.txt` for a complete example of the structured diff format.
The test script `test_merge_diff.py` can be run to verify the implementation.

## Backward Compatibility

The function signature remains the same (returns tuple of 3 values), but the second value (`base_versions`) is now always `None` since base context is included in `diff_content`. All calling code has been updated to handle this.
