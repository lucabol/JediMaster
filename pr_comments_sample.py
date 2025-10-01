#!/usr/bin/env python3
"""
Show the latest comments for a pull request.

Usage:
    python pr_comments_sample.py --pr PR_NUMBER [--repo REPO_NAME]

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
    parser = argparse.ArgumentParser(description="Show latest comments for a PR")
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

    print(f"Comments for PR #{pr.number}: {pr.title}\n")

    # Collect all comments (issue, review, and review comments) with timestamps
    comments = []

    # Issue comments
    for c in pr.get_issue_comments():
        comments.append({
            'type': 'issue',
            'user': c.user.login,
            'created_at': c.created_at,
            'body': c.body,
        })

    # Review comments (inline)
    for c in pr.get_review_comments():
        comments.append({
            'type': 'review_comment',
            'user': c.user.login,
            'created_at': c.created_at,
            'body': c.body,
        })

    # PR reviews (summary reviews)
    for r in pr.get_reviews():
        comments.append({
            'type': f'review_{r.state.lower()}',
            'user': r.user.login,
            'created_at': r.submitted_at,
            'body': r.body,
        })

    # Sort all comments by created_at descending (most recent first)
    comments.sort(key=lambda x: x['created_at'], reverse=True)

    print(f"\nLast {min(10, len(comments))} comments (most recent first):\n")
    for c in comments[:10]:
        print(f"- [{c['type']}] {c['user']} at {c['created_at']}: {c['body'][:200]}")

    return 0

if __name__ == "__main__":
    import sys
    sys.exit(main())
