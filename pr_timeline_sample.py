#!/usr/bin/env python3
"""
Show the full timeline of events for a pull request.

Usage:
    python pr_timeline_sample.py --pr PR_NUMBER [--repo REPO_NAME]

Requires .env with GITHUB_TOKEN.
"""

import argparse
import os
from pathlib import Path
from dotenv import load_dotenv
from github import Github
from github.GithubException import GithubException, UnknownObjectException

DEFAULT_REPO = "lucabol/JediTestRepoV2"

def main():
    parser = argparse.ArgumentParser(description="Show timeline events for a PR")
    parser.add_argument("--pr", type=int, required=True, help="PR number")
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"Repository (default: {DEFAULT_REPO})")
    args = parser.parse_args()

    dotenv_path = Path(".env")
    if dotenv_path.exists():
        load_dotenv(dotenv_path, override=True)

    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN not found in environment. Update .env or set the variable and retry.")
        return 1

    gh = Github(token)
    try:
        repo = gh.get_repo(args.repo)
        pr = repo.get_pull(args.pr)
    except UnknownObjectException:
        print("Repository or PR not found or inaccessible.")
        return 1
    except GithubException as exc:
        print(f"Failed to load PR: {exc}")
        return 1

    print(f"Timeline for PR #{pr.number}: {pr.title}\n")

    # Use the timeline API via as_issue().get_timeline()
    try:
        timeline = list(pr.as_issue().get_timeline())
    except Exception as exc:
        print(f"Failed to fetch timeline: {exc}")
        return 1

    if not timeline:
        print("No timeline events found.")
        return 0

    for event in timeline:
        event_type = getattr(event, 'event', event.__class__.__name__)
        created_at = getattr(event, 'created_at', None)
        actor = getattr(event, 'actor', None)
        user = getattr(event, 'user', None)
        who = actor.login if actor else (user.login if user else "?")
        summary = ''
        if hasattr(event, 'body') and event.body:
            summary = f" {event.body[:120].replace('\n',' ')}"
        elif hasattr(event, 'state'):
            summary = f" state={event.state}"
        elif hasattr(event, 'label'):
            summary = f" label={getattr(event.label, 'name', '')}"
        elif hasattr(event, 'requested_reviewer'):
            summary = f" requested={getattr(event.requested_reviewer, 'login', '')}"
        elif hasattr(event, 'commit_id'):
            if event.commit_id:
                summary = f" commit={event.commit_id[:8]}"
            else:
                summary = " commit=None"
        print(f"- [{created_at}] {event_type} by {who}{summary}")

    return 0

if __name__ == "__main__":
    import sys
    sys.exit(main())
