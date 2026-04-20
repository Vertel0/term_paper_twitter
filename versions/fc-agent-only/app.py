import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

from llm_function_calling_agent import run_account_verification_fc, run_verification_fc

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH, override=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("FLASK_SECRET_KEY", "dev-secret")

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_EXECUTOR = ThreadPoolExecutor(max_workers=2)


def _required_env() -> tuple[str, str, str, str, str, str]:
    binance_api_key = os.getenv("BINANCE_API_KEY", os.getenv("COINCAP_API_KEY", "")).strip()
    nlp_api_key = os.getenv("AITUNNEL_API_KEY", "").strip()
    nlp_model = os.getenv("AITUNNEL_MODEL", "deepseek-v3.2").strip() or "deepseek-v3.2"
    nlp_base_url = os.getenv("AITUNNEL_BASE_URL", "https://api.aitunnel.ru/v1/").strip() or "https://api.aitunnel.ru/v1/"
    fc_model = os.getenv("AITUNNEL_FC_MODEL", os.getenv("AGENT_FC_MODEL", "gpt-5.4-nano")).strip() or "gpt-5.4-nano"
    fc_web_model = os.getenv("AGENT_WEB_MODEL", "sonar").strip() or "sonar"
    return binance_api_key, nlp_api_key, nlp_model, nlp_base_url, fc_model, fc_web_model


def _parse_account_count(raw_value: str, default: int = 30) -> int:
    try:
        v = int((raw_value or "").strip())
    except Exception:
        return default
    return max(1, min(v, 200))


def _set_job(job_id: str, **updates) -> None:
    with _JOBS_LOCK:
        if job_id not in _JOBS:
            _JOBS[job_id] = {}
        _JOBS[job_id].update(updates)


def _cleanup_jobs(max_age_sec: int = 3600) -> None:
    now = time.time()
    with _JOBS_LOCK:
        stale = [jid for jid, job in _JOBS.items() if now - float(job.get("created_at", now)) > max_age_sec]
        for jid in stale:
            _JOBS.pop(jid, None)


def _run_job(job_id: str, mode: str, tweet_id: str, username: str, account_count: int) -> None:
    binance_api_key, nlp_api_key, nlp_model, nlp_base_url, fc_model, _fc_web_model = _required_env()

    def cb(progress: int, message: str) -> None:
        _set_job(job_id, progress=max(0, min(int(progress), 100)), message=message)

    try:
        if mode == "account_fc":
            if not username:
                raise RuntimeError("Введите username")
            _set_job(job_id, progress=2, message="Запуск FC-проверки аккаунта")
            account_result = run_account_verification_fc(
                username=username,
                api_key=binance_api_key or "public",
                count=account_count,
                nlp_api_key=nlp_api_key,
                nlp_model=fc_model,
                nlp_base_url=nlp_base_url,
                progress_callback=cb,
            )
            _set_job(job_id, done=True, progress=100, message="Готово", account_result=account_result)
        else:
            if not tweet_id:
                raise RuntimeError("Введите tweet_id")
            _set_job(job_id, progress=2, message="Запуск FC-проверки поста")
            result = run_verification_fc(
                tweet_id=tweet_id,
                api_key=binance_api_key or "public",
                tweet_qid=os.getenv("TW_QID_TWEET", "").strip(),
                nlp_api_key=nlp_api_key,
                nlp_model=fc_model,
                nlp_base_url=nlp_base_url,
                tweet_out="tweet_output.json",
                output="verification_output.json",
                runs_table=os.getenv("RUNS_TABLE", "verification_runs.csv"),
                progress_callback=cb,
            )
            _set_job(job_id, done=True, progress=100, message="Готово", result=result)
    except Exception as exc:  # noqa: BLE001
        _set_job(job_id, done=True, progress=100, message="Ошибка", error=str(exc))


@app.post("/api/start")
def api_start():
    _cleanup_jobs()
    mode = (request.form.get("mode") or "tweet_fc").strip()
    tweet_id = (request.form.get("tweet_id") or "").strip()
    username = (request.form.get("username") or "").strip()
    account_count = _parse_account_count(request.form.get("account_count") or "30")

    job_id = uuid.uuid4().hex
    _set_job(
        job_id,
        created_at=time.time(),
        mode=mode,
        tweet_id=tweet_id,
        username=username,
        account_count=account_count,
        progress=1,
        message="Поставлено в очередь",
        done=False,
        error="",
        result=None,
        account_result=None,
    )
    _EXECUTOR.submit(_run_job, job_id, mode, tweet_id, username, account_count)
    return jsonify({"ok": True, "job_id": job_id})


@app.get("/api/status/<job_id>")
def api_status(job_id: str):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "job_not_found"}), 404
        return jsonify(
            {
                "ok": True,
                "done": bool(job.get("done", False)),
                "progress": int(job.get("progress", 0)),
                "message": job.get("message", ""),
                "error": job.get("error", ""),
                "mode": job.get("mode", "tweet_fc"),
            }
        )


@app.route("/", methods=["GET", "POST"])
def index():
    result = None
    account_result = None
    error = None
    tweet_id = ""
    username = ""
    account_count = 30
    tweet_json = ""
    author_json = ""
    author_profile_json = ""
    author_profile_comment_text = ""
    binance_request_json = ""
    binance_response_json = ""
    account_summary_text = ""
    account_profile_comment_text = ""
    account_totals_json = ""
    account_profile_json = ""
    account_posts_json = ""
    account_micro = []

    job_id = (request.args.get("job_id") or "").strip()
    if job_id:
        with _JOBS_LOCK:
            job = _JOBS.get(job_id) or {}
        if job.get("error"):
            error = job.get("error")
        result = job.get("result")
        account_result = job.get("account_result")
        tweet_id = job.get("tweet_id", "")
        username = job.get("username", "")
        account_count = int(job.get("account_count", 30) or 30)

    if request.method == "POST" and not result and not account_result:
        mode = (request.form.get("mode") or "tweet_fc").strip()
        tweet_id = (request.form.get("tweet_id") or "").strip()
        username = (request.form.get("username") or "").strip()
        account_count = _parse_account_count(request.form.get("account_count") or "30")

        binance_api_key, nlp_api_key, nlp_model, nlp_base_url, fc_model, _fc_web_model = _required_env()
        if mode == "account_fc":
            if not username:
                error = "Введите username"
            else:
                try:
                    account_result = run_account_verification_fc(
                        username=username,
                        api_key=binance_api_key or "public",
                        count=account_count,
                        nlp_api_key=nlp_api_key,
                        nlp_model=fc_model,
                        nlp_base_url=nlp_base_url,
                    )
                except Exception as exc:  # noqa: BLE001
                    error = str(exc)
        else:
            if not tweet_id:
                error = "Введите tweet_id"
            else:
                try:
                    result = run_verification_fc(
                        tweet_id=tweet_id,
                        api_key=binance_api_key or "public",
                        tweet_qid=os.getenv("TW_QID_TWEET", "").strip(),
                        nlp_api_key=nlp_api_key,
                        nlp_model=fc_model,
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
        author_profile_json = json.dumps(result.get("author_profile", {}), ensure_ascii=False, indent=2)
        author_profile_comment_text = ((result.get("author_profile_comment") or {}).get("text") or "")
        binance_request_json = json.dumps((result.get("binance", {}) or {}).get("request", {}), ensure_ascii=False, indent=2)
        binance_response_json = json.dumps((result.get("binance", {}) or {}).get("response", {}), ensure_ascii=False, indent=2)

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
        account_count=account_count,
        result=result,
        account_result=account_result,
        result_json=json.dumps(result, ensure_ascii=False, indent=2) if result else "",
        tweet_json=tweet_json,
        author_json=author_json,
        author_profile_json=author_profile_json,
        author_profile_comment_text=author_profile_comment_text,
        binance_request_json=binance_request_json,
        binance_response_json=binance_response_json,
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
