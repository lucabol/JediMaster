import azure.functions as func
import logging, os, json, traceback
from datetime import datetime

from jedimaster import JediMaster
from creator import CreatorAgent
from reset_utils import reset_repository

app = func.FunctionApp()

# Environment variable controls (all optional except tokens):
# GITHUB_TOKEN (required)
# OPENAI_API_KEY (required for LLM issue creation / decision logic)
# AUTOMATION_REPOS: comma-separated list owner/repo (required)
# SCHEDULE_CRON: optional cron expression (default: every 6 hours)
# CREATE_ISSUES: if '1' or 'true', create issues
# CREATE_ISSUES_COUNT: number of issues per repo (default 3 when CREATE_ISSUES enabled)
# SIMILARITY_THRESHOLD: similarity threshold for duplicate detection (default 0.9)
# PROCESS_PRS: if '1' or 'true', run PR review logic
# AUTO_MERGE: if '1' or 'true', attempt auto merge of approved PRs
# JUST_LABEL: if '1' or 'true', only label issues (do not assign)
# USE_FILE_FILTER: if '1' use .coding_agent file instead of topic filter

DEFAULT_CRON = "0 0 */6 * * *"  # every 6 hours

# We dynamically register the timer based on env so deployment does not require code change for schedule
schedule_expr = os.getenv("SCHEDULE_CRON", DEFAULT_CRON)

@app.timer_trigger(schedule=schedule_expr, arg_name="automationTimer", run_on_startup=False, use_monitor=False)
def AutomateRepos(automationTimer: func.TimerRequest) -> None:
    start_ts = datetime.utcnow().isoformat()
    logging.info(f"[AutomateRepos] Invocation start {start_ts} schedule={schedule_expr}")
    if automationTimer.past_due:
        logging.warning("[AutomateRepos] Timer is past due")

    github_token = os.getenv('GITHUB_TOKEN')
    openai_api_key = os.getenv('OPENAI_API_KEY')
    repos_env = os.getenv('AUTOMATION_REPOS')

    if not github_token:
        logging.error("[AutomateRepos] Missing GITHUB_TOKEN – aborting")
        return
    if not openai_api_key:
        logging.error("[AutomateRepos] Missing OPENAI_API_KEY – aborting")
        return
    if not repos_env:
        logging.error("[AutomateRepos] AUTOMATION_REPOS not set – aborting")
        return

    repo_names = [r.strip() for r in repos_env.split(',') if r.strip()]
    if not repo_names:
        logging.error("[AutomateRepos] AUTOMATION_REPOS parsed empty – aborting")
        return

    create_issues_flag = os.getenv('CREATE_ISSUES', '0').lower() in ('1', 'true', 'yes')
    create_count_raw = os.getenv('CREATE_ISSUES_COUNT')
    create_count = None
    if create_issues_flag:
        try:
            create_count = int(create_count_raw) if create_count_raw else 3
        except ValueError:
            create_count = 3
    
    # Parse similarity threshold
    similarity_threshold_raw = os.getenv('SIMILARITY_THRESHOLD')
    similarity_threshold = 0.9  # default
    if similarity_threshold_raw:
        try:
            similarity_threshold = float(similarity_threshold_raw)
            if not (0.0 <= similarity_threshold <= 1.0):
                logging.warning(f"[AutomateRepos] Invalid SIMILARITY_THRESHOLD {similarity_threshold}, using default 0.9")
                similarity_threshold = 0.9
        except ValueError:
            logging.warning(f"[AutomateRepos] Invalid SIMILARITY_THRESHOLD format {similarity_threshold_raw}, using default 0.9")
            similarity_threshold = 0.9
    
    process_prs_flag = os.getenv('PROCESS_PRS', '1').lower() in ('1', 'true', 'yes')
    auto_merge_flag = os.getenv('AUTO_MERGE', '1').lower() in ('1', 'true', 'yes')
    just_label_flag = os.getenv('JUST_LABEL', '1').lower() in ('1', 'true', 'yes')
    use_file_filter = os.getenv('USE_FILE_FILTER', '0').lower() in ('1', 'true', 'yes')

    logging.info(
        "[AutomateRepos] Config: repos=%s create_issues=%s count=%s similarity_threshold=%s process_prs=%s auto_merge=%s just_label=%s use_file_filter=%s",
        repo_names, create_issues_flag, create_count, similarity_threshold, process_prs_flag, auto_merge_flag, just_label_flag, use_file_filter
    )

    summary = {
        'start': start_ts,
        'repos_processed': 0,
        'issue_reports': [],  # per repo issue stats
        'issue_creation': [],  # per repo created issues
        'pr_merge': [],        # per repo merge results
        'errors': []
    }

    # Instantiate core orchestrator once; we'll reuse for all repos. We will toggle PR mode per path.
    jedi = JediMaster(
        github_token,
        openai_api_key,
        just_label=just_label_flag,
        use_topic_filter=not use_file_filter,
        process_prs=False,  # we'll call PR / merge flows explicitly
        auto_merge_reviewed=False
    )

    for repo_full in repo_names:
        logging.info(f"[AutomateRepos] Processing repository {repo_full}")
        repo_block = {'repo': repo_full}
        try:
            # 1. Optional issue creation
            if create_issues_flag:
                try:
                    creator = CreatorAgent(github_token, openai_api_key, repo_full, similarity_threshold=similarity_threshold)
                    created = creator.create_issues(max_issues=create_count or 3)
                    repo_block['created_issues'] = created
                    summary['issue_creation'].append({'repo': repo_full, 'created': created})
                    logging.info(f"[AutomateRepos] Created {len(created)} issues in {repo_full}")
                except Exception as e:
                    logging.error(f"[AutomateRepos] Issue creation failed for {repo_full}: {e}")
                    summary['errors'].append({'repo': repo_full, 'stage': 'create_issues', 'error': str(e)})
            # 2. Issue labeling / assigning (non-PR run)
            try:
                report = jedi.process_repositories([repo_full])
                repo_block['issue_report'] = {
                    'total': report.total_issues,
                    'assigned': report.assigned,
                    'labeled': report.labeled,
                    'not_assigned': report.not_assigned,
                    'already_assigned': report.already_assigned,
                    'errors': report.errors
                }
                summary['issue_reports'].append({'repo': repo_full, **repo_block['issue_report']})
                logging.info(f"[AutomateRepos] Issue processing summary {repo_full}: {repo_block['issue_report']}")
            except Exception as e:
                logging.error(f"[AutomateRepos] Issue processing failed for {repo_full}: {e}")
                summary['errors'].append({'repo': repo_full, 'stage': 'issues', 'error': str(e)})
            # 3. PR review & optional auto merge
            if process_prs_flag:
                try:
                    pr_results = jedi.process_pull_requests(repo_full)
                    repo_block['pr_reviews'] = pr_results
                    logging.info(f"[AutomateRepos] PR review results count={len(pr_results)} repo={repo_full}")
                except Exception as e:
                    logging.error(f"[AutomateRepos] PR review failed for {repo_full}: {e}")
                    summary['errors'].append({'repo': repo_full, 'stage': 'pr_review', 'error': str(e)})
                if auto_merge_flag:
                    try:
                        merge_results = jedi.merge_reviewed_pull_requests(repo_full)
                        repo_block['merge'] = merge_results
                        summary['pr_merge'].append({'repo': repo_full, 'results': merge_results})
                        logging.info(f"[AutomateRepos] Merge attempt results count={len(merge_results)} repo={repo_full}")
                    except Exception as e:
                        logging.error(f"[AutomateRepos] Auto-merge failed for {repo_full}: {e}")
                        summary['errors'].append({'repo': repo_full, 'stage': 'auto_merge', 'error': str(e)})
            summary['repos_processed'] += 1
        except Exception as e:
            logging.error(f"[AutomateRepos] Unexpected failure for {repo_full}: {e}\n{traceback.format_exc()}")
            summary['errors'].append({'repo': repo_full, 'stage': 'generic', 'error': str(e)})
        # Optionally log per repo block debug
        logging.debug(f"[AutomateRepos] Repo block detail: {json.dumps(repo_block)[:800]}")

    end_ts = datetime.utcnow().isoformat()
    summary['end'] = end_ts
    logging.info(f"[AutomateRepos] Completed run. Repos={summary['repos_processed']} errors={len(summary['errors'])}")
    # Compact summary log
    try:
        logging.info("[AutomateRepos] Summary JSON: %s", json.dumps(summary)[:4000])
    except Exception:
        pass

@app.function_name(name="ResetRepositories")
@app.route(route="reset", methods=[func.HttpMethod.POST], auth_level=func.AuthLevel.FUNCTION)
def ResetRepositories(req: func.HttpRequest) -> func.HttpResponse:
    """HTTP endpoint to reset all repositories listed in AUTOMATION_REPOS.
    Reuses the logic from example reset process (close issues/PRs, delete branches except main, restore baseline files, prune others).
    """
    github_token = os.getenv('GITHUB_TOKEN')
    repos_env = os.getenv('AUTOMATION_REPOS')
    if not github_token or not repos_env:
        return func.HttpResponse(
            json.dumps({"error": "Missing GITHUB_TOKEN or AUTOMATION_REPOS"}),
            status_code=400,
            mimetype="application/json"
        )
    repo_names = [r.strip() for r in repos_env.split(',') if r.strip()]
    summaries = []
    errors = []
    for full in repo_names:
        try:
            logging.info(f"[ResetRepositories] Resetting {full}")
            summaries.append(reset_repository(github_token, full, logging.getLogger('jedimaster')))
        except Exception as e:
            logging.error(f"[ResetRepositories] Failed to reset {full}: {e}")
            errors.append({"repo": full, "error": str(e)})
    body = {"repositories": repo_names, "results": summaries, "errors": errors}
    return func.HttpResponse(json.dumps(body), status_code=200, mimetype="application/json")