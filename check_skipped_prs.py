import os
import requests

token = os.environ['GITHUB_TOKEN']
headers = {'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}

for pr_num in [1402, 1400, 1396]:
    print(f'\n=== PR #{pr_num} ===')
    
    # Get PR info
    pr_url = f'https://api.github.com/repos/gim-home/JediTestRepoV3/pulls/{pr_num}'
    pr_resp = requests.get(pr_url, headers=headers)
    pr = pr_resp.json()
    
    print(f'Title: {pr.get("title")}')
    print(f'State: {pr.get("state")}')
    print(f'Draft: {pr.get("draft", False)}')
    print(f'Mergeable: {pr.get("mergeable")}')
    
    # Get timeline
    timeline_url = f'https://api.github.com/repos/gim-home/JediTestRepoV3/issues/{pr_num}/timeline'
    timeline_headers = headers.copy()
    timeline_headers['Accept'] = 'application/vnd.github.mockingbird-preview+json'
    timeline_resp = requests.get(timeline_url, headers=timeline_headers)
    events = timeline_resp.json()
    
    print(f'\nLast 3 timeline events:')
    for event in events[-3:]:
        event_type = event.get('event')
        created_at = event.get('created_at')
        print(f'  - {event_type}: {created_at}')
        if 'actor' in event and event['actor']:
            print(f'    Actor: {event.get("actor", {}).get("login")}')
        if event_type == 'assigned' and 'assignee' in event:
            print(f'    Assignee: {event.get("assignee", {}).get("login")}')
