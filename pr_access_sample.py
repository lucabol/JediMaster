#!/usr/bin/env python3
"""Quick sanity check for GitHub token access to pull request diffs.

This script tries to retrieve the first open pull request from the
`lucabol/HelloWorld` (public) and `lucabol/JediMasterV2` (private) repositories
using the GitHub token defined in the environment. It exercises both the
PyGithub `get_files()` API and the raw diff URL as a fallback so you can see
precisely where access breaks down.

Usage:
    python pr_access_sample.py

Ensure that `.env` contains a `GITHUB_TOKEN` entry before running.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from github import Github
from github.GithubException import GithubException, UnknownObjectException

# Repositories we want to probe (public first, then private)
TARGET_REPOS = [
    ("lucabol/Hello-World", "public"),
    ("lucabol/JediTestRepoV2", "private"),
]

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
    try:
        for file in pr.get_files():
            patch = getattr(file, "patch", None)
            if patch:
                return patch
    except GithubException as exc:  # pragma: no cover - PyGithub raises at runtime
        print(f"    [PyGithub] Unable to enumerate files: {exc}")
    return None


def fetch_pr_diff_via_http(pr, token: str) -> Optional[str]:
    """Fallback to the raw diff URL using the provided token."""
    headers = {
        "Accept": "application/vnd.github.v3.diff",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "jedimaster-pr-access-check",
    }
    try:
        resp = requests.get(pr.diff_url, headers=headers, timeout=15)
    except requests.RequestException as exc:
        print(f"    [HTTP] Request to {pr.diff_url} failed: {exc}")
        return None

    if resp.status_code == 200:
        return resp.text

    if resp.status_code == 401:
        print("    [HTTP] 401 Unauthorized: token rejected (check scopes / expiration).")
    elif resp.status_code == 403:
        print("    [HTTP] 403 Forbidden: token lacks required repo permissions.")
    elif resp.status_code == 404:
        print("    [HTTP] 404 Not Found: repository or PR inaccessible with this token.")
    else:
        print(f"    [HTTP] {resp.status_code}: {preview(resp.text)}")
    return None


def inspect_repository(gh: Github, repo_name: str, visibility: str, token: str) -> None:
    """Attempt to fetch the most recent open PR for the given repository."""
    print(f"\n=== Checking {repo_name} ({visibility}) ===")
    try:
        repo = gh.get_repo(repo_name)
    except UnknownObjectException:
        print("  Repository not found or inaccessible with this token.")
        return
    except GithubException as exc:
        print(f"  Failed to load repository: {exc}")
        return

    pulls = repo.get_pulls(state="open", sort="created", direction="desc")
    try:
        pr = pulls[0]
    except IndexError:
        print("  No open pull requests to test.")
        return

    print(f"  Using PR #{pr.number}: {pr.title}")

    patch = fetch_pr_patch(pr)
    if patch:
        first_line = patch.strip().splitlines()[0] if patch.strip() else ""
        print(f"  CONTENT: {first_line}")
        return

    print("  PyGithub patch data unavailable (likely permission-related).")

    diff_text = fetch_pr_diff_via_http(pr, token)
    if diff_text:
        first_line = diff_text.strip().splitlines()[0] if diff_text.strip() else ""
        print(f"  CONTENT: {first_line}")
    else:
        print("  Unable to retrieve diff via raw HTTP request.")


def main() -> int:
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

    for repo_name, visibility in TARGET_REPOS:
        inspect_repository(gh, repo_name, visibility, token)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
