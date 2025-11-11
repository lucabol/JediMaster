import os
from dotenv import load_dotenv
from github import Github

load_dotenv()
token = os.getenv('GITHUB_TOKEN')
g = Github(token)
repo = g.get_repo('gim-home/JediTestRepoV3')
issue = repo.get_issue(1164)

timeline = list(issue.get_timeline())
print(f'PR #1164: Total {len(timeline)} timeline events\n')

for i, event in enumerate(timeline):
    created = event.created_at if hasattr(event, 'created_at') else 'N/A'
    print(f'{i}. Event: {event.event}, Time: {created}')
    
    if event.event == 'assigned' and hasattr(event, 'raw_data'):
        data = event.raw_data.get('assignee', {})
        if data:
            print(f'   -> Assignee: {data.get("login", "unknown")}')
    elif event.event == 'reviewed':
        print(f'   -> State: {event.state}, By: {event.user.login}')
