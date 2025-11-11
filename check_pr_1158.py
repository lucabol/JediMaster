import os
import requests
from dotenv import load_dotenv
import json

load_dotenv()
token = os.getenv('GITHUB_TOKEN')
headers = {'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}

# Get PR details
pr_url = 'https://api.github.com/repos/gim-home/JediTestRepoV3/pulls/1158'
pr_response = requests.get(pr_url, headers=headers)
print(f'PR Status Code: {pr_response.status_code}')
if pr_response.status_code == 200:
    pr_data = pr_response.json()
    print(f'PR number: {pr_data["number"]}, Title: {pr_data["title"]}')
    print(f'State: {pr_data["state"]}')
    print(f'Created: {pr_data["created_at"]}')
    print(f'Updated: {pr_data["updated_at"]}')
    print(f'Mergeable: {pr_data.get("mergeable")}')
    print(f'Draft: {pr_data.get("draft")}')
    print()
    
    # Get reviews
    reviews_url = 'https://api.github.com/repos/gim-home/JediTestRepoV3/pulls/1158/reviews'
    reviews_response = requests.get(reviews_url, headers=headers)
    if reviews_response.status_code == 200:
        reviews = reviews_response.json()
        print(f'\nReviews: {len(reviews)}')
        for review in reviews:
            user_login = review.get('user', {}).get('login', 'unknown')
            state = review.get('state', 'unknown')
            submitted = review.get('submitted_at', 'N/A')
            body = review.get('body', '')
            print(f'\n{"="*80}')
            print(f'Review by: {user_login}')
            print(f'State: {state}')
            print(f'Submitted: {submitted}')
            print(f'Body:\n{body}')
    else:
        print(f'Reviews Status Code: {reviews_response.status_code}')
else:
    print(f'Error: {pr_response.text}')
