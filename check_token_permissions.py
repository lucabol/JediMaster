#!/usr/bin/env python3
"""Diagnose whether the GitHub token in `.env` has the repo permissions JediMaster needs.

The script loads the GITHUB_TOKEN from the local `.env` file (overriding any
shell environment), then performs a series of read operations against each
repository listed in the `AUTOMATION_REPOS` environment variable. These checks
mirror what JediMaster requires when processing pull requests:

* Fetch repository metadata
* Read repository contents (root listing)
* Enumerate files in the newest open PR and obtain patch data

The script reports failures with actionable messages so you can adjust the
fine-grained token configuration accordingly.

Usage:
    python check_token_permissions.py

Environment:
    GITHUB_TOKEN       - GitHub (fine-grained) token to validate.
    AUTOMATION_REPOS   - Comma-separated list of `owner/name` repositories.
                          Defaults to `lucabol/Hello-World,lucabol/JediTestRepoV2`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

import requests
from dotenv import load_dotenv
from github import Github
from github.GithubException import GithubException, UnknownObjectException

DEFAULT_REPOS = ["lucabol/Hello-World", "lucabol/JediTestRepoV2"]


def describe_token(token: str) -> str:
    """Return a safe-to-log preview of the token."""
    if len(token) <= 10:
        return token
    return f"{token[:6]}...{token[-4:]}"


def read_target_repos(env_value: Optional[str]) -> Sequence[str]:
    if not env_value:
        return DEFAULT_REPOS
    repos = [repo.strip() for repo in env_value.split(",")]
    return [repo for repo in repos if repo]


def fetch_scopes(token: str) -> str:
    """Return the X-OAuth-Scopes header (classic tokens) or a note for fine-grained ones."""
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "jedimaster-token-check",
        "Accept": "application/vnd.github.v3+json",
    }
    resp = requests.get("https://api.github.com/user", headers=headers, timeout=10)
    scopes = resp.headers.get("X-OAuth-Scopes", "")
    if scopes:
        return scopes
    if resp.headers.get("X-GitHub-SSO", ""):
        return "(enterprise SSO enforced; scopes hidden)"
    # Fine-grained tokens often return an empty scopes header; provide a hint.
    return "(fine-grained tokens do not expose scopes in headers)"


def check_repo_permissions(gh: Github, repo_name: str) -> None:
    print(f"\n=== {repo_name} ===")

    try:
        repo = gh.get_repo(repo_name)
    except UnknownObjectException:
        print("  ❌ Unable to access repository metadata (missing Metadata permission or repo not granted).")
        return
    except GithubException as exc:
        print(f"  ❌ Failed to load repository: {exc}")
        return

    print("  ✅ Repository metadata accessible.")

    # Contents permission check (list root directory)
    try:
        repo.get_contents("")
    except GithubException as exc:
        print(f"  ❌ Cannot list repository contents (requires Contents: Read). Details: {exc}")
    else:
        print("  ✅ Repository contents readable (Contents: Read).")

    # Pull request / diff access check
    pulls = repo.get_pulls(state="open", sort="created", direction="desc")
    try:
        pr = pulls[0]
    except IndexError:
        print("  ⚠️ No open pull requests to verify PR access.")
        return

    print(f"  Using PR #{pr.number}: {pr.title}")

    try:
        files = pr.get_files()
        first_patch = None
        for file in files:
            if getattr(file, "patch", None):
                first_patch = file.patch
                break
    except GithubException as exc:
        print(f"  ❌ Cannot enumerate PR files (requires Pull requests: Read). Details: {exc}")
        return

    if not first_patch:
        print("  ⚠️ PR files accessible, but no patch data returned (possible binary-only changes).")
    else:
        first_line = first_patch.strip().splitlines()[0] if first_patch.strip() else ""
        print(f"  ✅ Pull request file patches accessible. CONTENT: {first_line}")


def main() -> int:
    dotenv_path = Path(".env")
    if dotenv_path.exists():
        load_dotenv(dotenv_path, override=True)

    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN not found. Add it to .env before running this script.")
        return 1

    repos_env = os.getenv("AUTOMATION_REPOS")
    target_repos = read_target_repos(repos_env)
    if not target_repos:
        print("No repositories configured via AUTOMATION_REPOS or default list.")
        return 1

    print(f"Using GITHUB_TOKEN: {describe_token(token)}")
    print(f"Token header scopes: {fetch_scopes(token)}")
    print(f"Repositories to check: {', '.join(target_repos)}")

    gh = Github(token)

    for repo_name in target_repos:
        check_repo_permissions(gh, repo_name)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
