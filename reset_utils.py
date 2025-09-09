import os
import base64
import requests
import logging
from typing import Dict, Any

def _gh_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}

def close_all_open_issues(token: str, owner: str, repo: str, logger: logging.Logger) -> int:
    url = f"https://api.github.com/repos/{owner}/{repo}/issues?state=open&per_page=100"
    resp = requests.get(url, headers=_gh_headers(token))
    if resp.status_code != 200:
        logger.warning(f"Failed to fetch issues: {resp.status_code} {resp.text}")
        return 0
    count = 0
    for issue in resp.json():
        if 'pull_request' in issue:
            continue
        issue_number = issue['number']
        close_url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}"
        close_resp = requests.patch(close_url, headers=_gh_headers(token), json={"state": "closed"})
        if close_resp.status_code == 200:
            count += 1
        else:
            logger.warning(f"Failed to close issue #{issue_number}: {close_resp.status_code}")
    return count

def close_all_open_prs(token: str, owner: str, repo: str, logger: logging.Logger) -> int:
    pr_url = f"https://api.github.com/repos/{owner}/{repo}/pulls?state=open&per_page=100"
    pr_resp = requests.get(pr_url, headers=_gh_headers(token))
    if pr_resp.status_code != 200:
        logger.warning(f"Failed to fetch open PRs: {pr_resp.status_code} {pr_resp.text}")
        return 0
    count = 0
    for pr in pr_resp.json():
        pr_number = pr['number']
        close_pr_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
        patch_resp = requests.patch(close_pr_url, headers=_gh_headers(token), json={"state": "closed"})
        if patch_resp.status_code == 200:
            count += 1
        else:
            logging.warning(f"Failed to close PR #{pr_number}: {patch_resp.status_code} {patch_resp.text}")
    return count

def delete_all_branches_except_main(token: str, owner: str, repo: str, logger: logging.Logger) -> int:
    url = f"https://api.github.com/repos/{owner}/{repo}/branches"
    resp = requests.get(url, headers=_gh_headers(token))
    if resp.status_code != 200:
        logger.warning(f"Failed to fetch branches: {resp.status_code} {resp.text}")
        return 0
    deleted = 0
    for branch in resp.json():
        name = branch['name']
        if name == 'main':
            continue
        del_url = f"https://api.github.com/repos/{owner}/{repo}/git/refs/heads/{name}"
        del_resp = requests.delete(del_url, headers=_gh_headers(token))
        if del_resp.status_code == 204:
            deleted += 1
        else:
            logger.info(f"Could not delete branch {name}: status {del_resp.status_code}")
    return deleted

def update_github_file(token: str, owner: str, repo: str, path: str, new_content: str, commit_message: str, logger: logging.Logger) -> bool:
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    headers = _gh_headers(token)
    response = requests.get(url, headers=headers)
    sha = response.json().get('sha') if response.status_code == 200 else None
    encoded = base64.b64encode(new_content.encode('utf-8')).decode('utf-8')
    data = {"message": commit_message, "content": encoded}
    if sha:
        data['sha'] = sha
    put_resp = requests.put(url, headers=headers, json=data)
    if put_resp.status_code not in (200, 201):
        logger.warning(f"Failed to update {path}: {put_resp.status_code} {put_resp.text}")
        return False
    return True

def prune_files(token: str, owner: str, repo: str, logger: logging.Logger) -> Dict[str, Any]:
    url = f"https://api.github.com/repos/{owner}/{repo}/contents"
    resp = requests.get(url, headers=_gh_headers(token))
    deleted = []
    skipped_dirs = []
    if resp.status_code != 200:
        logger.warning(f"Failed to list repo contents: {resp.status_code} {resp.text}")
        return {"deleted": deleted, "skipped_dirs": skipped_dirs}
    allowed = {"hello.c", ".gitignore", "README.md"}
    for item in resp.json():
        name = item['name']
        path = item['path']
        if name in allowed or (name == '.github' and item['type'] == 'dir'):
            continue
        if item['type'] == 'file':
            del_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
            del_resp = requests.delete(del_url, headers=_gh_headers(token), json={"message": f"Remove {name} for repo reset", "sha": item['sha']})
            if del_resp.status_code in (200, 204):
                deleted.append(name)
            else:
                logger.warning(f"Failed to delete file {name}: {del_resp.status_code}")
        elif item['type'] == 'dir' and name != '.github':
            skipped_dirs.append(name)
    return {"deleted": deleted, "skipped_dirs": skipped_dirs}

def reset_repository(token: str, full_name: str, logger: logging.Logger) -> Dict[str, Any]:
    owner, repo = full_name.split('/')
    result: Dict[str, Any] = {"repository": full_name}
    result['closed_issues'] = close_all_open_issues(token, owner, repo, logger)
    result['closed_prs'] = close_all_open_prs(token, owner, repo, logger)
    result['deleted_branches'] = delete_all_branches_except_main(token, owner, repo, logger)
    baseline_hello = ('# include <stdio.h>\n\n' 'int main(){\n' '    printf("Hello world!");\n' '}\n')
    result['hello_updated'] = update_github_file(token, owner, repo, 'hello.c', baseline_hello, 'Reset baseline hello.c for repo reset', logger)
    baseline_readme = '# Hello World\n Test repo for JediMaster'
    result['readme_updated'] = update_github_file(token, owner, repo, 'README.md', baseline_readme, 'Reset baseline README.md for repo reset', logger)
    prune = prune_files(token, owner, repo, logger)
    result['deleted_files'] = prune['deleted']
    result['skipped_dirs'] = prune['skipped_dirs']
    return result
