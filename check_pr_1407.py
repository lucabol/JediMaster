import requests
import json
import os
from dotenv import load_dotenv

load_dotenv()
token = os.getenv('GITHUB_TOKEN')
headers = {'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}

# First get recent PRs to find the right one
prs_response = requests.get('https://api.github.com/repos/gim-home/JediTestRepoV3/pulls?state=all&per_page=20&sort=created&direction=desc', headers=headers)
print("Recent PRs:")
for pr in prs_response.json()[:10]:
    print(f"  PR #{pr['number']}: {pr['title'][:60]} - {pr['state']}")

# Get PR timeline - using a recent PR number
pr_number = 1407
print(f"\n\nChecking PR #{pr_number}...")
response = requests.get(f'https://api.github.com/repos/gim-home/JediTestRepoV3/issues/{pr_number}/timeline', headers=headers)
if response.status_code != 200:
    print(f"Error: {response.status_code}")
    print(response.text)
    exit(1)

timeline = response.json()

print('PR #1407 Timeline Events:')
print('=' * 80)
for i, event in enumerate(timeline):
    if isinstance(event, str):
        print(f'\n{i}. STRING EVENT: {event}')
        continue
        
    event_type = event.get('event', event.get('state', 'unknown'))
    created_at = event.get('created_at', event.get('submitted_at', 'unknown'))
    
    if event_type == 'commented':
        body = event.get('body', '')
        author = event.get('user', {}).get('login', 'unknown')
        print(f'\n{i}. {created_at} - COMMENT by {author}:')
        if len(body) > 300:
            print(f'   Body: {body[:300]}...')
        else:
            print(f'   Body: {body}')
    elif event_type == 'assigned' or event_type == 'unassigned':
        assignee = event.get('assignee', {}).get('login', 'unknown')
        print(f'\n{i}. {created_at} - {event_type.upper()}: {assignee}')
    elif event_type == 'reviewed':
        state = event.get('state', 'unknown')
        author = event.get('user', {}).get('login', 'unknown')
        body = event.get('body', '')
        print(f'\n{i}. {created_at} - REVIEWED by {author}: {state}')
        if body:
            print(f'   Review body: {body[:200]}...' if len(body) > 200 else f'   Review body: {body}')
    else:
        print(f'\n{i}. {created_at} - {event_type.upper()}')
        
    # Check for copilot-related content
    event_str = json.dumps(event).lower()
    if 'copilot' in event_str:
        print(f'   ** Contains copilot reference **')
        if 'error' in event_str or 'fail' in event_str:
            print(f'   ** MAY CONTAIN ERROR **')
