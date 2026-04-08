import json
import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, render_template, request

from search_by_id import run_account_verification, run_verification

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH, override=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-secret")


def _required_env() -> tuple[str, str, str, str]:
    coincap_api_key = os.getenv("COINCAP_API_KEY", "").strip()
    nlp_api_key = os.getenv("AITUNNEL_API_KEY", "").strip()
    nlp_model = os.getenv("AITUNNEL_MODEL", "deepseek-v3.2").strip() or "deepseek-v3.2"
    nlp_base_url = os.getenv("AITUNNEL_BASE_URL", "https://api.aitunnel.ru/v1/").strip() or "https://api.aitunnel.ru/v1/"
    return coincap_api_key, nlp_api_key, nlp_model, nlp_base_url


@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    account_result = None
    error = None
    tweet_id = ""
    username = ""
    tweet_json = ""
    author_json = ""
    coincap_request_json = ""
    coincap_response_json = ""
    account_summary_text = ""
    account_profile_comment_text = ""
    account_totals_json = ""
    account_profile_json = ""
    account_posts_json = ""
    account_micro = []

    if request.method == "POST":
        mode = (request.form.get("mode") or "tweet").strip()
        tweet_id = (request.form.get("tweet_id") or "").strip()
        username = (request.form.get("username") or "").strip()

        coincap_api_key, nlp_api_key, nlp_model, nlp_base_url = _required_env()
        if not coincap_api_key:
            error = "COINCAP_API_KEY не найден в .env"
        elif mode == "account":
            if not username:
                error = "Введите username"
            else:
                try:
                    account_result = run_account_verification(
                        username=username,
                        api_key=coincap_api_key,
                        nlp_api_key=nlp_api_key,
                        nlp_model=nlp_model,
                        nlp_base_url=nlp_base_url,
                    )
                except Exception as exc:  # noqa: BLE001
                    error = str(exc)
        else:
            if not tweet_id:
                error = "Введите tweet_id"
            else:
                try:
                    result = run_verification(
                        tweet_id=tweet_id,
                        api_key=coincap_api_key,
                        tweet_qid=os.getenv("TW_QID_TWEET", "").strip(),
                        nlp_api_key=nlp_api_key,
                        nlp_model=nlp_model,
                        nlp_base_url=nlp_base_url,
                        tweet_out="tweet_output.json",
                        output="verification_output.json",
                        runs_table=os.getenv("RUNS_TABLE", "verification_runs.csv"),
                    )
                except Exception as exc:  # noqa: BLE001
                    error = str(exc)

    if result:
        tweet_json = json.dumps(result.get("tweet", {}), ensure_ascii=False, indent=2)
        author_json = json.dumps(result.get("author", {}), ensure_ascii=False, indent=2)
        coincap_request_json = json.dumps((result.get("coincap", {}) or {}).get("request", {}), ensure_ascii=False, indent=2)
        coincap_response_json = json.dumps((result.get("coincap", {}) or {}).get("response", {}), ensure_ascii=False, indent=2)

    if account_result:
        account_summary_text = ((account_result.get("account_assessment") or {}).get("text") or "")
        account_profile_comment_text = ((account_result.get("account_profile_comment") or {}).get("text") or "")
        account_profile_json = json.dumps(account_result.get("account_profile", {}), ensure_ascii=False, indent=2)
        account_totals_json = json.dumps(account_result.get("totals", {}), ensure_ascii=False, indent=2)
        account_posts_json = json.dumps(account_result.get("posts", []), ensure_ascii=False, indent=2)
        account_micro = account_result.get("micro_research", []) or []

    return render_template(
        "index.html",
        tweet_id=tweet_id,
        username=username,
        result=result,
        account_result=account_result,
        result_json=json.dumps(result, ensure_ascii=False, indent=2) if result else "",
        tweet_json=tweet_json,
        author_json=author_json,
        coincap_request_json=coincap_request_json,
        coincap_response_json=coincap_response_json,
        account_summary_text=account_summary_text,
        account_profile_comment_text=account_profile_comment_text,
        account_totals_json=account_totals_json,
        account_profile_json=account_profile_json,
        account_posts_json=account_posts_json,
        account_micro=account_micro,
        error=error,
    )


if __name__ == "__main__":
    host = os.getenv("FLASK_HOST", "127.0.0.1")
    port = int(os.getenv("FLASK_PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host=host, port=port, debug=debug)
