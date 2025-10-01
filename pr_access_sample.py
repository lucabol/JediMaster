#!/usr/bin/env python3
"""Quick sanity check for GitHub token access to pull request diffs.

This script tries to retrieve pull request diffs from specified repositories
using the GitHub token defined in the environment. It exercises both the
PyGithub `get_files()` API and the raw diff URL as a fallback so you can see
precisely where access breaks down.

Usage:
    python pr_access_sample.py [--pr PR_NUMBER] [--repo REPO_NAME]

Optional arguments:
    --pr PR_NUMBER     Specific PR number to fetch (default: first open PR)
    --repo REPO_NAME   Repository to fetch from (default: lucabol/JediTestRepoV2)

Ensure that `.env` contains a `GITHUB_TOKEN` entry before running.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from github import Github
from github.GithubException import GithubException, UnknownObjectException

# Default repository if none specified
DEFAULT_REPO = "lucabol/JediTestRepoV2"

# Limit the amount of diff we display so logs stay readable
DIFF_PREVIEW_CHARS = 400


def preview(text: str, limit: int = DIFF_PREVIEW_CHARS) -> str:
    """Return a compact preview of the provided text for logging."""
    clean = text.replace("\r", "")
    return clean if len(clean) <= limit else f"{clean[:limit]}..."  # Keep ASCII only


def describe_token(token: str) -> str:
    """Return a safe-to-log representation of the token."""
    if len(token) <= 10:
        return token
    return f"{token[:6]}...{token[-4:]}"


def fetch_pr_patch(pr) -> Optional[str]:
    """Try to gather a diff snippet via PyGithub's file patch data."""
    print(f"    [PyGithub] Attempting to fetch files for PR #{pr.number}...")
    print(f"    [PyGithub] PR state: {pr.state}, draft: {getattr(pr, 'draft', 'unknown')}")
    print(f"    [PyGithub] PR mergeable: {getattr(pr, 'mergeable', 'unknown')}")
    
    try:
        files = list(pr.get_files())
        print(f"    [PyGithub] Found {len(files)} files in PR")
        
        if not files:
            print(f"    [PyGithub] No files found in PR #{pr.number}")
            return None
            
        for i, file in enumerate(files):
            print(f"    [PyGithub] File {i+1}: {file.filename} (status: {file.status})")
            patch = getattr(file, "patch", None)
            if patch:
                print(f"    [PyGithub] Found patch data for {file.filename} ({len(patch)} chars)")
                return patch
            else:
                print(f"    [PyGithub] No patch data for {file.filename}")
                
        print(f"    [PyGithub] No patch data found in any of the {len(files)} files")
    except GithubException as exc:  # pragma: no cover - PyGithub raises at runtime
        print(f"    [PyGithub] GithubException: {type(exc).__name__}: {exc}")
        print(f"    [PyGithub] Exception status: {getattr(exc, 'status', 'unknown')}")
        print(f"    [PyGithub] Exception data: {getattr(exc, 'data', 'unknown')}")
    except Exception as exc:
        print(f"    [PyGithub] Unexpected exception: {type(exc).__name__}: {exc}")
    return None


def fetch_pr_content_via_commits(pr, token: str) -> Optional[str]:
    """Alternative method: fetch PR content by getting commit diffs."""
    print(f"    [COMMITS] Attempting to fetch PR content via commits API...")
    
    try:
        commits = list(pr.get_commits())
        print(f"    [COMMITS] Found {len(commits)} commits in PR")
        
        if not commits:
            print(f"    [COMMITS] No commits found in PR")
            return None
            
        # Try to get files from the latest commit
        latest_commit = commits[-1]
        print(f"    [COMMITS] Latest commit: {latest_commit.sha[:8]} - {latest_commit.commit.message.strip()[:50]}")
        
        files = list(latest_commit.files)
        print(f"    [COMMITS] Found {len(files)} files in latest commit")
        
        for file in files:
            patch = getattr(file, "patch", None)
            if patch:
                print(f"    [COMMITS] Found patch data for {file.filename} ({len(patch)} chars)")
                return patch
                
        print(f"    [COMMITS] No patch data found in commit files")
        
    except Exception as exc:
        print(f"    [COMMITS] Failed to fetch via commits: {type(exc).__name__}: {exc}")
    
    return None


def fetch_pr_content_via_compare(pr, gh: Github) -> Optional[str]:
    """Alternative method: use compare API to get diff between base and head."""
    print(f"    [COMPARE] Attempting to fetch PR content via compare API...")
    
    try:
        repo = pr.base.repo
        base_sha = pr.base.sha
        head_sha = pr.head.sha
        
        print(f"    [COMPARE] Comparing {base_sha[:8]}...{head_sha[:8]}")
        
        comparison = repo.compare(base_sha, head_sha)
        print(f"    [COMPARE] Comparison status: {comparison.status}")
        print(f"    [COMPARE] Files changed: {len(comparison.files)}")
        
        for file in comparison.files:
            patch = getattr(file, "patch", None)
            if patch:
                print(f"    [COMPARE] Found patch data for {file.filename} ({len(patch)} chars)")
                return patch
                
        print(f"    [COMPARE] No patch data found in comparison files")
        
    except Exception as exc:
        print(f"    [COMPARE] Failed to fetch via compare: {type(exc).__name__}: {exc}")
    
    return None


def fetch_pr_content_via_raw_api(pr, token: str) -> Optional[str]:
    """Alternative method: use raw GitHub API with different endpoints."""
    print(f"    [RAW_API] Attempting to fetch PR content via raw GitHub API...")
    
    headers = {
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "jedimaster-pr-access-check",
    }
    
    # Try different API endpoints
    endpoints_to_try = [
        (f"{pr.url}/files", "application/vnd.github.v3+json"),
        (f"{pr.url}.diff", "text/plain"),
        (f"{pr.url}.patch", "text/plain"),
    ]
    
    for endpoint, accept_header in endpoints_to_try:
        print(f"    [RAW_API] Trying endpoint: {endpoint}")
        headers["Accept"] = accept_header
        
        try:
            resp = requests.get(endpoint, headers=headers, timeout=15)
            print(f"    [RAW_API] Response status: {resp.status_code}")
            
            if resp.status_code == 200:
                content = resp.text
                print(f"    [RAW_API] Success! Retrieved {len(content)} characters")
                
                # If it's JSON (files endpoint), try to extract patch from first file
                if accept_header == "application/vnd.github.v3+json":
                    try:
                        files_data = json.loads(content)
                        if files_data and isinstance(files_data, list):
                            for file_data in files_data:
                                if "patch" in file_data and file_data["patch"]:
                                    print(f"    [RAW_API] Found patch in JSON response")
                                    return file_data["patch"]
                    except json.JSONDecodeError:
                        pass
                else:
                    # For .diff or .patch endpoints, return content directly
                    return content
                    
            elif resp.status_code == 403:
                print(f"    [RAW_API] 403 Forbidden - possible rate limit or permissions issue")
            else:
                print(f"    [RAW_API] Failed with status {resp.status_code}")
                
        except Exception as exc:
            print(f"    [RAW_API] Request failed: {type(exc).__name__}: {exc}")
    
    return None


def fetch_pr_diff_via_http(pr, token: str) -> Optional[str]:
    """Alternative method: fetch PR content by getting commit diffs."""
    print(f"    [COMMITS] Attempting to fetch PR content via commits API...")
    
    try:
        commits = list(pr.get_commits())
        print(f"    [COMMITS] Found {len(commits)} commits in PR")
        
        if not commits:
            print(f"    [COMMITS] No commits found in PR")
            return None
            
        # Try to get files from the latest commit
        latest_commit = commits[-1]
        print(f"    [COMMITS] Latest commit: {latest_commit.sha[:8]} - {latest_commit.commit.message.strip()[:50]}")
        
        files = list(latest_commit.files)
        print(f"    [COMMITS] Found {len(files)} files in latest commit")
        
        for file in files:
            patch = getattr(file, "patch", None)
            if patch:
                print(f"    [COMMITS] Found patch data for {file.filename} ({len(patch)} chars)")
                return patch
                
        print(f"    [COMMITS] No patch data found in commit files")
        
    except Exception as exc:
        print(f"    [COMMITS] Failed to fetch via commits: {type(exc).__name__}: {exc}")
    
    return None


def fetch_pr_content_via_compare(pr, gh: Github) -> Optional[str]:
    """Alternative method: use compare API to get diff between base and head."""
    print(f"    [COMPARE] Attempting to fetch PR content via compare API...")
    
    try:
        repo = pr.base.repo
        base_sha = pr.base.sha
        head_sha = pr.head.sha
        
        print(f"    [COMPARE] Comparing {base_sha[:8]}...{head_sha[:8]}")
        
        comparison = repo.compare(base_sha, head_sha)
        print(f"    [COMPARE] Comparison status: {comparison.status}")
        print(f"    [COMPARE] Files changed: {len(comparison.files)}")
        
        for file in comparison.files:
            patch = getattr(file, "patch", None)
            if patch:
                print(f"    [COMPARE] Found patch data for {file.filename} ({len(patch)} chars)")
                return patch
                
        print(f"    [COMPARE] No patch data found in comparison files")
        
    except Exception as exc:
        print(f"    [COMPARE] Failed to fetch via compare: {type(exc).__name__}: {exc}")
    
    return None


def fetch_pr_content_via_raw_api(pr, token: str) -> Optional[str]:
    """Alternative method: use raw GitHub API with different endpoints."""
    print(f"    [RAW_API] Attempting to fetch PR content via raw GitHub API...")
    
    headers = {
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "jedimaster-pr-access-check",
    }
    
    # Try different API endpoints
    endpoints_to_try = [
        (f"{pr.url}/files", "application/vnd.github.v3+json"),
        (f"{pr.url}.diff", "text/plain"),
        (f"{pr.url}.patch", "text/plain"),
    ]
    
    for endpoint, accept_header in endpoints_to_try:
        print(f"    [RAW_API] Trying endpoint: {endpoint}")
        headers["Accept"] = accept_header
        
        try:
            resp = requests.get(endpoint, headers=headers, timeout=15)
            print(f"    [RAW_API] Response status: {resp.status_code}")
            
            if resp.status_code == 200:
                content = resp.text
                print(f"    [RAW_API] Success! Retrieved {len(content)} characters")
                
                # If it's JSON (files endpoint), try to extract patch from first file
                if accept_header == "application/vnd.github.v3+json":
                    try:
                        import json
                        files_data = json.loads(content)
                        if files_data and isinstance(files_data, list):
                            for file_data in files_data:
                                if "patch" in file_data and file_data["patch"]:
                                    print(f"    [RAW_API] Found patch in JSON response")
                                    return file_data["patch"]
                    except json.JSONDecodeError:
                        pass
                else:
                    # For .diff or .patch endpoints, return content directly
                    return content
                    
            elif resp.status_code == 403:
                print(f"    [RAW_API] 403 Forbidden - possible rate limit or permissions issue")
            else:
                print(f"    [RAW_API] Failed with status {resp.status_code}")
                
        except Exception as exc:
            print(f"    [RAW_API] Request failed: {type(exc).__name__}: {exc}")
    
    return None
    """Fallback to the raw diff URL using the provided token."""
    print(f"    [HTTP] Attempting to fetch diff via HTTP for PR #{pr.number}")
    print(f"    [HTTP] Diff URL: {pr.diff_url}")
    
    headers = {
        "Accept": "application/vnd.github.v3.diff",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "jedimaster-pr-access-check",
    }
    
    try:
        print(f"    [HTTP] Making request with headers: {dict(headers)}")
        resp = requests.get(pr.diff_url, headers=headers, timeout=15)
        print(f"    [HTTP] Response status: {resp.status_code}")
        print(f"    [HTTP] Response headers: {dict(resp.headers)}")
    except requests.RequestException as exc:
        print(f"    [HTTP] Request to {pr.diff_url} failed: {type(exc).__name__}: {exc}")
        return None

    if resp.status_code == 200:
        content_length = len(resp.text)
        print(f"    [HTTP] Success! Retrieved {content_length} characters of diff data")
        return resp.text

    # Enhanced error reporting
    print(f"    [HTTP] Request failed with status {resp.status_code}")
    if resp.status_code == 401:
        print("    [HTTP] 401 Unauthorized: token rejected (check scopes / expiration).")
    elif resp.status_code == 403:
        print("    [HTTP] 403 Forbidden: token lacks required repo permissions.")
        print(f"    [HTTP] Response body: {preview(resp.text)}")
    elif resp.status_code == 404:
        print("    [HTTP] 404 Not Found: repository or PR inaccessible with this token.")
    else:
        print(f"    [HTTP] {resp.status_code}: {preview(resp.text)}")
    
    return None


def inspect_repository(gh: Github, repo_name: str, pr_number: Optional[int], token: str) -> None:
    """Attempt to fetch the specified PR or the most recent open PR for the given repository."""
    print(f"\n=== Checking {repo_name} ===")
    try:
        repo = gh.get_repo(repo_name)
    except UnknownObjectException:
        print("  Repository not found or inaccessible with this token.")
        return
    except GithubException as exc:
        print(f"  Failed to load repository: {exc}")
        return

    if pr_number:
        # Fetch specific PR
        try:
            pr = repo.get_pull(pr_number)
            print(f"  Using specified PR #{pr.number}: {pr.title}")
        except UnknownObjectException:
            print(f"  PR #{pr_number} not found.")
            return
        except GithubException as exc:
            print(f"  Failed to load PR #{pr_number}: {type(exc).__name__}: {exc}")
            return
    else:
        # Fetch first open PR
        pulls = repo.get_pulls(state="open", sort="created", direction="desc")
        try:
            pr = pulls[0]
            print(f"  Using most recent open PR #{pr.number}: {pr.title}")
        except IndexError:
            print("  No open pull requests to test.")
            return
    
    # Print detailed PR information
    print(f"  PR Details:")
    print(f"    - State: {pr.state}")
    print(f"    - Draft: {getattr(pr, 'draft', 'unknown')}")
    print(f"    - Mergeable: {getattr(pr, 'mergeable', 'unknown')}")
    print(f"    - Merged: {getattr(pr, 'merged', 'unknown')}")
    print(f"    - Base: {pr.base.ref}")
    print(f"    - Head: {pr.head.ref}")
    print(f"    - Changed files: {getattr(pr, 'changed_files', 'unknown')}")
    print(f"    - Additions: {getattr(pr, 'additions', 'unknown')}")
    print(f"    - Deletions: {getattr(pr, 'deletions', 'unknown')}")
    print(f"    - Commits: {getattr(pr, 'commits', 'unknown')}")

    patch = fetch_pr_patch(pr)
    if patch:
        first_line = patch.strip().splitlines()[0] if patch.strip() else ""
        print(f"  SUCCESS via PyGithub: {first_line}")
        return

    print(f"  PyGithub patch data unavailable, trying alternative methods...")

    # Try commits API approach
    patch = fetch_pr_content_via_commits(pr, token)
    if patch:
        first_line = patch.strip().splitlines()[0] if patch.strip() else ""
        print(f"  SUCCESS via Commits API: {first_line}")
        return

    # Try compare API approach  
    patch = fetch_pr_content_via_compare(pr, gh)
    if patch:
        first_line = patch.strip().splitlines()[0] if patch.strip() else ""
        print(f"  SUCCESS via Compare API: {first_line}")
        return

    # Try raw API endpoints
    patch = fetch_pr_content_via_raw_api(pr, token)
    if patch:
        first_line = patch.strip().splitlines()[0] if patch.strip() else ""
        print(f"  SUCCESS via Raw API: {first_line}")
        return

    # Finally try the original HTTP diff method
    diff_text = fetch_pr_diff_via_http(pr, token)
    if diff_text:
        first_line = diff_text.strip().splitlines()[0] if diff_text.strip() else ""
        print(f"  SUCCESS via HTTP Diff: {first_line}")
    else:
        print(f"  FAILED: Unable to retrieve PR content via any method.")


def main() -> int:
    # Parse command line arguments
    parser = argparse.ArgumentParser(
        description="Check GitHub token access to pull request diffs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Ensure that .env contains a GITHUB_TOKEN entry before running."
    )
    parser.add_argument(
        "--pr",
        type=int,
        help="Specific PR number to fetch (default: first open PR)"
    )
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help=f"Repository to fetch from (default: {DEFAULT_REPO})"
    )
    
    args = parser.parse_args()

    # Load environment variables from .env if present
    dotenv_path = Path(".env")
    if dotenv_path.exists():
        # Override any shell-provided token so the .env file takes precedence
        load_dotenv(dotenv_path, override=True)

    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN not found in environment. Update .env or set the variable and retry.")
        return 1

    print(f"Using GITHUB_TOKEN: {describe_token(token)}")

    gh = Github(token)

    inspect_repository(gh, args.repo, args.pr, token)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
