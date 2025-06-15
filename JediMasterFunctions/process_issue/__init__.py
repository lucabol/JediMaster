def main(req: func.HttpRequest) -> func.HttpResponse:
import azure.functions as func
from jedimaster import process_issues_api

def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        data = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON", status_code=400)
    result = process_issues_api(data)
    return func.HttpResponse(str(result), status_code=200)
