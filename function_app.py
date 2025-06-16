import azure.functions as func
import datetime
import json
import logging

from jedimaster import process_issues_api, process_user_api

app = func.FunctionApp()

@app.route(route="ProcessRepos", auth_level=func.AuthLevel.ANONYMOUS)
def ProcessRepos(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('ProcessRepos HTTP trigger function called.')
    repo_names_param = req.params.get('repo_names')
    input_data = {}
    repo_names = None
    if repo_names_param:
        # Support comma-separated list in query string
        repo_names = [r.strip() for r in repo_names_param.split(',') if r.strip()]
        input_data['repo_names'] = repo_names
    else:
        try:
            input_data = req.get_json()
        except ValueError:
            return func.HttpResponse(
                json.dumps({"error": "Invalid JSON in request body or missing repo_names parameter"}),
                status_code=400,
                mimetype="application/json"
            )
        repo_names = input_data.get('repo_names')
    if not repo_names or not isinstance(repo_names, list) or not repo_names:
        return func.HttpResponse(
            json.dumps({"error": "Missing repo_names in query string or request body (should be a non-empty list or comma-separated string)"}),
            status_code=400,
            mimetype="application/json"
        )
    result = process_issues_api(input_data)
    return func.HttpResponse(
        json.dumps(result),
        status_code=200,
        mimetype="application/json"
    )

@app.route(route="ProcessUser", auth_level=func.AuthLevel.ANONYMOUS)
def ProcessUser(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('ProcessUser HTTP trigger function called.')
    username = req.params.get('username')
    input_data = {}
    if not username:
        try:
            input_data = req.get_json()
        except ValueError:
            return func.HttpResponse(
                json.dumps({"error": "Invalid JSON in request body or missing username parameter"}),
                status_code=400,
                mimetype="application/json"
            )
        username = input_data.get('username')
    else:
        input_data['username'] = username
    if not username:
        return func.HttpResponse(
            json.dumps({"error": "Missing username in query string or request body"}),
            status_code=400,
            mimetype="application/json"
        )
    result = process_user_api(input_data)
    return func.HttpResponse(
        json.dumps(result),
        status_code=200,
        mimetype="application/json"
    )