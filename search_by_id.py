#!/usr/bin/env python
# -*- coding: utf-8 -*-
import csv
import html
import json
import os
import re
from datetime import datetime, timezone
from typing import Callable, Optional
from dateutil import parser

def clean_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()
from crypto_tweet_gate import classify_tweet_text
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

# Output paths

AITUNNEL_BASE_URL = os.getenv("AITUNNEL_BASE_URL", "https://api.aitunnel.ru/v1/")
AITUNNEL_MODEL = os.getenv("AITUNNEL_MODEL", "deepseek-v3.2")

PROXIES = ["http://03hGHq:8dUFC8@95.164.202.193:9233"]

from claim_executor import (
    claim_to_legacy_payload,
    compact_coincap_result,
    execute_claim_with_coincap,
    judge_claim_results,
)
from claim_planner import coincap_capabilities_text, plan_claims_llm
from account_analysis import (
    build_account_stats_comment_llm,
    build_account_summary_llm,
    extract_account_profile_from_timeline,
)
from twitter_client import (
    build_author_info,
    build_session,
    extract_features_from_user,
    extract_user_result,
    fetch_tweet_by_id,
    find_tweet_node,
    find_tweet_node_by_id,
    load_cookies,
    load_tweet_qid,
    parse_tweet_json,
)


def is_crypto_tweet_llm(
    tweet_text: str,
    nlp_api_key: str,
    nlp_model: str = AITUNNEL_MODEL,
    nlp_base_url: str = AITUNNEL_BASE_URL,
) -> tuple[bool, str]:
    if not nlp_api_key or OpenAI is None:
        cashtags = re.findall(r"\$[A-Za-z]{2,10}\b", tweet_text or "")
        return bool(cashtags), "heuristic_cashtag"

    client = OpenAI(api_key=nlp_api_key, base_url=nlp_base_url)
    prompt = (
        "Classify whether the tweet is about crypto market/investment claims that are potentially verifiable with CoinCap data. "
        "Return JSON only: {\"is_crypto\": true/false, \"reason\": \"short\"}.\n"
        f"Tweet:\n{tweet_text}"
    )
    response = client.chat.completions.create(
        model=nlp_model,
        temperature=0,
        messages=[
            {"role": "system", "content": "You are a strict classifier. Return JSON only."},
            {"role": "user", "content": prompt},
        ],
    )
    raw = response.choices[0].message.content or ""
    parsed = _extract_json_object_from_text(raw)
    return bool(parsed.get("is_crypto", False)), str(parsed.get("reason", ""))


def run_account_verification(
    username: str,
    api_key: str,
    *,
    count: int = 30,
    nlp_api_key: str = "",
    nlp_model: str = AITUNNEL_MODEL,
    nlp_base_url: str = AITUNNEL_BASE_URL,
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> dict:
    from twitter_scraper import (
        build_session as ts_build_session,
        extract_entries,
        fetch_user_tweets,
        load_cookies as ts_load_cookies,
        load_query_ids,
        tweets_to_rows,
        user_by_screen_name,
    )

    if not api_key:
        raise RuntimeError("Pass CoinCap API key")
    uname = (username or "").strip().lstrip("@")
    if not uname:
        raise RuntimeError("Username is empty")

    cookies = ts_load_cookies()
    if progress_callback:
        progress_callback(5, "Загружаем профиль и последние посты")
    qids = load_query_ids()
    session = ts_build_session(cookies)
    user_id = user_by_screen_name(session, uname, qids["user_by_screen_name"])
    # Берем максимально возможный пул, затем фильтруем по давности >= 30 дней,
    # чтобы с высокой вероятностью получить именно requested `count` постов.
    fetch_pool = 200
    timeline_json = fetch_user_tweets(session, user_id, qids["user_tweets"], count=fetch_pool)
    tweet_legacy_list = extract_entries(timeline_json)
    rows = tweets_to_rows(tweet_legacy_list)

    # Стратегия отбора:
    # 1) сначала берем твиты давностью >= 30 дней;
    # 2) если их меньше requested count, добираем самыми старыми из оставшихся.
    now_utc = datetime.now(timezone.utc)
    parsed_rows = []
    for r in rows:
        created_at = r.get("created_at")
        try:
            dt = parser.parse(created_at) if created_at else None
            if dt is None:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_days = (now_utc - dt).days
            rr = dict(r)
            rr["_age_days"] = age_days
            rr["_dt"] = dt
            parsed_rows.append(rr)
        except Exception:
            continue

    aged_rows = [r for r in parsed_rows if int(r.get("_age_days", 0)) >= 30]
    fresh_rows = [r for r in parsed_rows if int(r.get("_age_days", 0)) < 30]

    # aged: оставляем исходный порядок (обычно от новых к старым)
    selected_rows = list(aged_rows)
    if len(selected_rows) < count:
        need = count - len(selected_rows)
        # добираем наиболее давними из свежих
        fresh_rows.sort(key=lambda x: x.get("_dt"))  # старые -> новые
        selected_rows.extend(fresh_rows[:need])

    rows = selected_rows
    account_profile = extract_account_profile_from_timeline(timeline_json, uname, user_id)

    analyzed = []
    selected_rows = rows[:count]
    total_rows = max(len(selected_rows), 1)
    for idx_row, row in enumerate(selected_rows, start=1):
        if progress_callback:
            base = 10
            span = 78
            progress_callback(base + int((idx_row - 1) / total_rows * span), f"Обрабатываем пост {idx_row}/{total_rows}")
        tweet_text = row.get("full_text", "")
        tweet_id = str(row.get("tweet_id", "") or "")
        if not tweet_id:
            continue

        cls = classify_tweet_text(tweet_text)
        if not cls.get("is_crypto", False):
            analyzed.append(
                {
                    "tweet_id": tweet_id,
                    "tweet_text": tweet_text,
                    "created_at": row.get("created_at"),
                    "included": False,
                    "skip_reason": "non_crypto_by_local_classifier",
                    "classifier": cls,
                }
            )
            continue

        try:
            one = run_verification(
                tweet_id=tweet_id,
                api_key=api_key,
                tweet_qid=os.getenv("TW_QID_TWEET", "").strip(),
                nlp_api_key=nlp_api_key,
                nlp_model=nlp_model,
                nlp_base_url=nlp_base_url,
                tweet_out=None,
                output=None,
                runs_table=os.getenv("RUNS_TABLE", "verification_runs.csv"),
                classifier_result_override=cls,
                progress_callback=None,
                skip_llm_summary=True,
            )
            analyzed.append(
                {
                    "tweet_id": tweet_id,
                    "tweet_text": one.get("tweet", {}).get("tweet_text", tweet_text),
                    "tweet_date": one.get("tweet", {}).get("tweet_date"),
                    "included": True,
                    "verdict": one.get("verdict", {}),
                    "analysis": one.get("analysis", {}),
                    "coincap": one.get("coincap", {}),
                    "executor": one.get("executor", {}),
                    "claim_payload": (one.get("nlp", {}) or {}).get("claim_payload", {}),
                    "classifier": (one.get("classifier") or cls),
                }
            )
        except Exception as exc:  # noqa: BLE001
            analyzed.append(
                {
                    "tweet_id": tweet_id,
                    "tweet_text": tweet_text,
                    "created_at": row.get("created_at"),
                    "included": True,
                    "error": str(exc),
                    "classifier": cls,
                }
            )

    crypto_posts = [x for x in analyzed if x.get("included")]
    true_n = sum(1 for x in crypto_posts if (x.get("verdict") or {}).get("status") == "true")
    false_n = sum(1 for x in crypto_posts if (x.get("verdict") or {}).get("status") == "false")
    unknown_n = sum(1 for x in crypto_posts if (x.get("verdict") or {}).get("status") == "unknown")
    denom = max(true_n + false_n, 1)
    accuracy = true_n / denom
    trust_level = "high" if accuracy >= 0.7 and true_n + false_n >= 5 else ("medium" if accuracy >= 0.45 else "low")
    score_values = []
    for x in crypto_posts:
        sc = (((x.get("verdict") or {}).get("score") or {}).get("confidence_0_100"))
        if isinstance(sc, (int, float)):
            score_values.append(float(sc))
    avg_score = round(sum(score_values) / len(score_values), 2) if score_values else 0.0

    result = {
        "username": uname,
        "user_id": user_id,
        "account_profile": account_profile,
        "coincap_capabilities": coincap_capabilities_text(),
        "totals": {
            "fetched_posts": len(rows[:count]),
            "crypto_posts": len(crypto_posts),
            "skipped_non_crypto": len([x for x in analyzed if not x.get("included")]),
            "true": true_n,
            "false": false_n,
            "unknown": unknown_n,
            "accuracy_on_decidable": round(accuracy, 4),
            "avg_confidence_score": avg_score,
            "trust_level": trust_level,
        },
        "posts": analyzed,
    }

    micro_research = []
    for idx, p in enumerate(analyzed, start=1):
        item = {
            "index": idx,
            "tweet_id": p.get("tweet_id"),
            "tweet_text": p.get("tweet_text"),
            "included": p.get("included", False),
            "classifier": p.get("classifier", {}),
        }
        if not p.get("included", False):
            item["status"] = "skipped_non_crypto"
            item["reason"] = p.get("skip_reason")
        elif p.get("error"):
            item["status"] = "error"
            item["error"] = p.get("error")
        else:
            item["status"] = (p.get("verdict") or {}).get("status", "unknown")
            item["score"] = ((p.get("verdict") or {}).get("score") or {}).get("confidence_0_100")
            item["claim_payload"] = p.get("claim_payload", {})
            item["coincap_request"] = ((p.get("coincap") or {}).get("request") or {})
            item["coincap_response"] = ((p.get("coincap") or {}).get("response") or {})
            item["market_facts"] = build_market_facts_line(p.get("claim_payload", {}), p)
            item["micro_summary"] = ((p.get("analysis") or {}).get("text") or "")
        micro_research.append(item)

    result["micro_research"] = micro_research
    if progress_callback:
        progress_callback(92, "Генерируем сводный вывод по аккаунту")
    src, txt = build_account_summary_llm(uname, result, nlp_api_key, nlp_model, nlp_base_url)
    result["account_assessment"] = {"source": src, "text": txt}
    stats_src, stats_txt = build_account_stats_comment_llm(
        uname,
        account_profile,
        result.get("totals", {}),
        nlp_api_key,
        nlp_model,
        nlp_base_url,
    )
    result["account_profile_comment"] = {"source": stats_src, "text": stats_txt}
    if progress_callback:
        progress_callback(100, "Готово")
    return result

# Feature flags (reuse from working twitter_scraper)
## Twitter client helpers moved to twitter_client.py


def build_verification_summary_heuristic(claim_payload: dict, merged_result: dict) -> str:
    verdict = (merged_result.get("verdict") or {}).get("status", "unknown")
    checks = (merged_result.get("verdict") or {}).get("checks", [])
    score = ((merged_result.get("verdict") or {}).get("score") or {}).get("confidence_0_100")
    ok_count = sum(1 for c in checks if str(c.get("status") or "").lower() == "true")
    all_count = len(checks)

    if verdict == "true":
        outcome = "Предикт в целом сбылся."
        trust = "Автору можно умеренно доверять по этому конкретному прогнозу."
    elif verdict == "false":
        outcome = "Предикт не подтвердился на выбранном горизонте."
        trust = "Доверие к автору по этому прогнозу низкое."
    else:
        outcome = "Недостаточно структурированных данных для уверенной проверки предикта."
        trust = "Недостаточно данных, чтобы делать вывод о надежности автора."

    coin = claim_payload.get("coin")
    horizon = claim_payload.get("relative_days")
    facts_line = build_market_facts_line(claim_payload, merged_result)
    facts_part = f" {facts_line}" if facts_line else ""
    return (
        f"Проверка прогноза по {coin} на горизонте {horizon} дней: {outcome} "
        f"Пройдено проверок: {ok_count}/{all_count}. "
        f"Confidence: {score if score is not None else 'n/a'}/100. {trust}{facts_part}"
    )


def _fmt_money(v) -> str:
    try:
        if v is None:
            return "n/a"
        n = float(v)
        return f"${n:,.2f}".replace(",", " ")
    except Exception:
        return "n/a"


def build_market_facts_line(claim_payload: dict, merged_result: dict) -> str:
    base = (((merged_result.get("coincap") or {}).get("response") or {}).get("base") or {})
    target = (((merged_result.get("coincap") or {}).get("response") or {}).get("target") or {})
    claim = (claim_payload or {}).get("claim") or {}

    base_price = base.get("price_usd")
    base_date = base.get("target_date") or (base.get("requested") or {}).get("tweet_date")
    target_price = target.get("price_usd")
    target_date = target.get("target_date") or (target.get("requested") or {}).get("tweet_date")
    claimed_price = claim.get("target_price_usd")

    parts = []
    if base_date or target_date or base_price is not None or target_price is not None:
        parts.append(
            "CoinCap: "
            f"старт {base_date or 'n/a'} = {_fmt_money(base_price)}, "
            f"заявлено = {_fmt_money(claimed_price)}, "
            f"факт {target_date or 'n/a'} = {_fmt_money(target_price)}"
        )

    claim_results = (((merged_result.get("executor") or {}).get("claim_results")) or [])
    extra_stats = []
    for r in claim_results:
        ctype = str(r.get("claim_type") or "")
        evidence = r.get("evidence") or {}
        if ctype == "price_target":
            tw = evidence.get("target_window") or {}
            hits = tw.get("hits_count")
            closest = tw.get("closest") or {}
            if isinstance(hits, int):
                extra_stats.append(
                    f"попаданий в окне: {hits}, closest={_fmt_money(closest.get('price_usd'))} ({closest.get('date') or 'n/a'})"
                )
        elif ctype == "relative_performance":
            main_ret = (r.get("checks") or [{}])[0].get("main_return")
            cmp_ret = (r.get("checks") or [{}])[0].get("comparison_return")
            cmp_coin = (r.get("checks") or [{}])[0].get("comparison_coin")
            if isinstance(main_ret, (int, float)) and isinstance(cmp_ret, (int, float)):
                extra_stats.append(
                    f"relative perf: {main_ret * 100:.2f}% vs {cmp_coin or 'bench'} {cmp_ret * 100:.2f}%"
                )
        elif ctype == "volatility_risk":
            vol = evidence.get("volatility_pct_daily")
            if isinstance(vol, (int, float)):
                extra_stats.append(f"волатильность(daily stdev): {vol:.2f}%")
        elif ctype == "market_cap":
            mcap = evidence.get("market_cap_usd")
            if isinstance(mcap, (int, float)):
                extra_stats.append(f"market cap: {_fmt_money(mcap)}")
        elif ctype == "timeframe_only":
            ch = (r.get("checks") or [{}])[0]
            if ch:
                extra_stats.append(f"checkpoints: {ch.get('count', 0)}/{ch.get('requested', 0)}")

    if extra_stats:
        parts.append("Доп.метрики: " + "; ".join(extra_stats[:2]))

    return ". ".join(parts)


def build_verification_summary_llm(
    task_description: str,
    claim_payload: dict,
    merged_result: dict,
    nlp_api_key: str,
    nlp_model: str = AITUNNEL_MODEL,
    nlp_base_url: str = AITUNNEL_BASE_URL,
) -> tuple[str, str, str]:
    if not nlp_api_key or OpenAI is None:
        return "heuristic", build_verification_summary_heuristic(claim_payload, merged_result), ""

    client = OpenAI(api_key=nlp_api_key, base_url=nlp_base_url)
    system_prompt = (
        "Ты аналитик проверки предсказаний в твитах. "
        "На основе задачи и JSON-результатов дай краткий вывод на русском языке: "
        "1) насколько сбылся предикт, 2) можно ли доверять автору по этому кейсу. "
        "Приоритет: сначала факты проверки claim'ов и числовые метрики verdict/score. "
        "Метрики аккаунта используй только как слабый вторичный сигнал, не как основание вывода. "
        "Пиши 3-5 предложений, без markdown."
    )
    user_prompt = (
        f"Изначальная задача:\n{task_description}\n\n"
        f"Claim JSON:\n{json.dumps(claim_payload, ensure_ascii=False, indent=2)}\n\n"
        f"Result JSON:\n{json.dumps(merged_result, ensure_ascii=False, indent=2)}"
    )

    response = client.chat.completions.create(
        model=nlp_model,
        temperature=0.1,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )
    text = (response.choices[0].message.content or "").strip()
    if not text:
        text = build_verification_summary_heuristic(claim_payload, merged_result)
        return "heuristic_fallback", text, ""
    return "llm", text, text


## Author parsing helpers moved to twitter_client.py


def extract_features(tweet_text: str) -> dict:
    text = tweet_text or ""
    low = text.lower()

    cashtags = [m.upper() for m in re.findall(r"\$([A-Za-z]{2,10})\b", text)]
    hashtags = re.findall(r"#([A-Za-z0-9_]{2,50})\b", text)

    rel_days = None
    m_days = re.search(r"in\s+(\d{1,3})\s+day", low)
    m_days_ru = re.search(r"через\s+(\d{1,3})\s+д", low)
    if m_days:
        rel_days = int(m_days.group(1))
    elif m_days_ru:
        rel_days = int(m_days_ru.group(1))
    elif "tomorrow" in low or "завтра" in low:
        rel_days = 1
    elif "next week" in low or "через неделю" in low:
        rel_days = 7
    elif "next month" in low or "через месяц" in low:
        rel_days = 30

    m_price = re.search(r"\$\s*(\d{1,3}(?:[\s,]\d{3})*(?:\.\d+)?)([kKmM]?)", text)
    approx_target_price = None
    if m_price:
        val = float(m_price.group(1).replace(" ", "").replace(",", ""))
        mul = m_price.group(2).lower()
        if mul == "k":
            val *= 1000
        elif mul == "m":
            val *= 1_000_000
        approx_target_price = val

    up_words = ["moon", "pump", "bull", "long", "up", "grow", "рост", "выраст"]
    down_words = ["dump", "bear", "short", "down", "fall", "drop", "пад", "упад"]
    up = any(w in low for w in up_words)
    down = any(w in low for w in down_words)
    direction_hint = "up" if up and not down else ("down" if down and not up else None)

    return {
        "cashtags": cashtags,
        "hashtags": hashtags,
        "relative_days_hint": rel_days,
        "target_price_hint_usd": approx_target_price,
        "direction_hint": direction_hint,
        "mentions_usd": "usd" in low or "$" in text,
    }


def extract_claim_struct_heuristic(tweet_text: str, tweet_date: str) -> dict:
    low = tweet_text.lower()
    coin = None
    cash = re.findall(r"\$([a-zA-Z]{2,10})\b", tweet_text)
    if cash:
        coin = cash[0].lower()
    if not coin:
        for k in ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "cardano", "ada", "xrp", "bnb"]:
            if re.search(rf"\b{re.escape(k)}\b", low):
                coin = k
                break
    if not coin:
        raise RuntimeError("Could not detect coin symbol in tweet")

    rel_days = 7
    m_days = re.search(r"in\s+(\d{1,3})\s+day", low)
    m_days_ru = re.search(r"через\s+(\d{1,3})\s+д", low)
    if m_days:
        rel_days = int(m_days.group(1))
    elif m_days_ru:
        rel_days = int(m_days_ru.group(1))
    elif "tomorrow" in low or "завтра" in low:
        rel_days = 1
    elif "next week" in low or "через неделю" in low:
        rel_days = 7
    elif "next month" in low or "через месяц" in low:
        rel_days = 30

    target_price = None
    m_price = re.search(r"\$\s*(\d{1,3}(?:[\s,]\d{3})*(?:\.\d+)?)([kKmM]?)", tweet_text)
    if m_price:
        val = float(m_price.group(1).replace(" ", "").replace(",", ""))
        mul = m_price.group(2).lower()
        if mul == "k":
            val *= 1000
        elif mul == "m":
            val *= 1_000_000
        target_price = val

    direction = None
    up_words = ["moon", "pump", "bull", "long", "up", "grow", "рост", "выраст"]
    down_words = ["dump", "bear", "short", "down", "fall", "drop", "пад", "упад"]
    up = any(w in low for w in up_words)
    down = any(w in low for w in down_words)
    if up and not down:
        direction = "up"
    elif down and not up:
        direction = "down"

    return {
        "coin": coin,
        "tweet_date": tweet_date,
        "relative_days": rel_days,
        "vs_currency": "usd",
        "claim": {
            "target_price_usd": target_price,
            "direction": direction,
            "horizon_days": rel_days,
        },
    }


def _extract_json_object_from_text(text: str) -> dict:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        raw = raw.strip()
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            raise RuntimeError("LLM did not return JSON")
        return json.loads(m.group(0))


def extract_claim_struct_llm(
    tweet_text: str,
    tweet_date: str,
    nlp_api_key: str,
    nlp_model: str = AITUNNEL_MODEL,
    nlp_base_url: str = AITUNNEL_BASE_URL,
    context_features: Optional[dict] = None,
) -> tuple[dict, str]:
    if OpenAI is None:
        raise RuntimeError("openai package is not installed")

    client = OpenAI(api_key=nlp_api_key, base_url=nlp_base_url)

    schema_description = {
        "coin": "string, lowercase ticker or slug (example: btc, eth, bitcoin)",
        "tweet_date": "string YYYY-MM-DD, must equal provided tweet_date",
        "relative_days": "integer 0..365",
        "vs_currency": "string, always 'usd'",
        "claim": {
            "target_price_usd": "number or null",
            "direction": "'up' | 'down' | null",
            "horizon_days": "integer, must equal relative_days",
            "confidence": "number 0..1",
            "rationale": "short string"
        }
    }

    system_prompt = (
        "You are an information extraction engine for crypto tweet verification. "
        "Convert one tweet into a STRICT JSON object for downstream CoinCap checks. "
        "Return JSON only, no markdown, no comments, no extra text."
    )

    user_prompt = (
        "Extract a claim JSON from tweet text for price verification.\n"
        f"{coincap_capabilities_text()}\n"
        "Use this exact output structure:\n"
        f"{json.dumps(schema_description, ensure_ascii=False, indent=2)}\n\n"
        f"Fixed tweet_date: {tweet_date}\n"
        f"Context features (hints, may be noisy): {json.dumps(context_features or {}, ensure_ascii=False)}\n"
        f"Tweet text:\n{tweet_text}\n\n"
        "Rules:\n"
        "- coin: lowercase\n"
        "- vs_currency: usd\n"
        "- if horizon is missing, use relative_days=7\n"
        "- if target price is absent, use target_price_usd=null\n"
        "- horizon_days must equal relative_days\n"
        "- return ONLY valid JSON object"
    )

    response = client.chat.completions.create(
        model=nlp_model,
        temperature=0,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )

    raw_content = response.choices[0].message.content or ""
    parsed = _extract_json_object_from_text(raw_content)

    coin = str(parsed.get("coin", "")).strip().lower()
    if not coin:
        raise RuntimeError("LLM JSON missing coin")

    rel_days = int(parsed.get("relative_days", 7))
    rel_days = max(0, min(rel_days, 365))

    claim = parsed.get("claim") if isinstance(parsed.get("claim"), dict) else {}
    direction = claim.get("direction")
    if direction not in ("up", "down", None):
        direction = None

    out = {
        "coin": coin,
        "tweet_date": tweet_date,
        "relative_days": rel_days,
        "vs_currency": "usd",
        "claim": {
            "target_price_usd": claim.get("target_price_usd"),
            "direction": direction,
            "horizon_days": rel_days,
            "confidence": claim.get("confidence"),
            "rationale": claim.get("rationale"),
        },
    }
    return out, raw_content


def extract_claim_struct(
    tweet_text: str,
    tweet_date: str,
    nlp_api_key: str = "",
    nlp_model: str = AITUNNEL_MODEL,
    nlp_base_url: str = AITUNNEL_BASE_URL,
    context_features: Optional[dict] = None,
) -> tuple[dict, str, str]:
    if nlp_api_key:
        try:
            claim, raw = extract_claim_struct_llm(
                tweet_text,
                tweet_date,
                nlp_api_key,
                nlp_model,
                nlp_base_url,
                context_features=context_features,
            )
            return claim, "llm", raw
        except Exception as e:
            fallback = extract_claim_struct_heuristic(tweet_text, tweet_date)
            return fallback, "heuristic_fallback", f"LLM_ERROR: {e}"
    fallback = extract_claim_struct_heuristic(tweet_text, tweet_date)
    return fallback, "heuristic", ""


def append_run_table(
    csv_path: str,
    tweet_info: dict,
    claim_payload: dict,
    merged_result: dict,
) -> None:
    headers = [
        "run_utc",
        "tweet_id",
        "tweet_date",
        "tweet_text",
        "request_json",
        "result_json",
    ]

    write_header = (not os.path.exists(csv_path)) or os.path.getsize(csv_path) == 0
    row = {
        "run_utc": datetime.now(timezone.utc).isoformat(),
        "tweet_id": tweet_info.get("tweet_id", ""),
        "tweet_date": tweet_info.get("tweet_date", ""),
        "tweet_text": tweet_info.get("tweet_text", ""),
        "request_json": json.dumps(claim_payload, ensure_ascii=False, separators=(",", ":")),
        "result_json": json.dumps(merged_result, ensure_ascii=False, separators=(",", ":")),
    }

    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def run_verification(
    tweet_id: str,
    api_key: str,
    *,
    tweet_qid: str = "",
    nlp_api_key: str = "",
    nlp_model: str = AITUNNEL_MODEL,
    nlp_base_url: str = AITUNNEL_BASE_URL,
    tweet_out: Optional[str] = "tweet_output.json",
    output: Optional[str] = "verification_output.json",
    runs_table: Optional[str] = "verification_runs.csv",
    classifier_result_override: Optional[dict] = None,
    progress_callback: Optional[Callable[[int, str], None]] = None,
    skip_llm_summary: bool = False,
) -> dict:
    if not api_key:
        raise RuntimeError("Pass CoinCap API key")

    if progress_callback:
        progress_callback(3, "Инициализация")

    cookies = load_cookies()
    session = build_session(cookies)
    if tweet_qid:
        os.environ["TW_QID_TWEET"] = tweet_qid
    loaded_tweet_qid = load_tweet_qid()

    raw_payload = None
    last_err = None
    if progress_callback:
        progress_callback(10, "Получаем твит из X")
    for proxy in PROXIES:
        try:
            raw_payload = fetch_tweet_by_id(session, loaded_tweet_qid, tweet_id, {"http": proxy, "https": proxy})
            break
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
            continue

    if raw_payload is None:
        try:
            raw_payload = fetch_tweet_by_id(session, loaded_tweet_qid, tweet_id, None)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"Tweet fetch failed. proxy_err={last_err}; direct_err={e}")

    tweet_info = parse_tweet_json(raw_payload, tweet_id)
    if progress_callback:
        progress_callback(22, "Извлекаем твит и профиль автора")
    if tweet_out:
        with open(tweet_out, "w", encoding="utf-8") as f:
            json.dump(tweet_info, f, ensure_ascii=False, indent=2)

    tweet_node = find_tweet_node_by_id(raw_payload, tweet_id) or find_tweet_node(raw_payload) or {}
    text_features = extract_features(tweet_info["tweet_text"])
    user_data = extract_user_result(tweet_node)
    tweet_legacy = tweet_node.get("legacy", {}) if isinstance(tweet_node, dict) else {}
    meta_features = extract_features_from_user(user_data, tweet_legacy, tweet_node) if tweet_node else {}

    classifier_result = classifier_result_override or classify_tweet_text(tweet_info["tweet_text"])

    if not classifier_result.get("is_crypto", False):
        merged = {
            "tweet": tweet_info,
            "author": build_author_info(user_data, meta_features),
            "classifier": classifier_result,
            "nlp": {
                "source": "skipped_non_crypto",
                "context_features": {
                    "text_features": text_features,
                    "meta_features": meta_features,
                },
                "claim_payload": {},
                "raw_model_output": "",
            },
            "coincap": {
                "request": {},
                "response": {},
                "base": {},
                "target": {},
            },
            "verdict": {
                "status": "skipped_non_crypto",
                "reason": "Tweet classified as non-crypto by local classifier",
            },
            "analysis": {
                "source": "classifier",
                "text": (
                    "Твит отнесен локальным классификатором к non-crypto, "
                    f"вероятность crypto={classifier_result.get('confidence_crypto')}"
                ),
                "raw_model_output": "",
            },
        }
        if output:
            with open(output, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)
        if progress_callback:
            progress_callback(100, "Готово")
        return merged

    features = {
        "text_features": text_features,
        "meta_features": meta_features,
    }

    planner_json, planner_source, planner_raw = plan_claims_llm(
        tweet_text=tweet_info["tweet_text"],
        tweet_date=tweet_info["tweet_date"],
        context_features=features,
        nlp_api_key=nlp_api_key,
        nlp_model=nlp_model,
        nlp_base_url=nlp_base_url,
    )
    if progress_callback:
        progress_callback(45, "Планируем проверяемые claim'ы")

    claims = planner_json.get("claims") or []
    claim_results = []
    total_claims = max(len(claims), 1)
    for idx_claim, c in enumerate(claims, start=1):
        if progress_callback:
            progress_callback(50 + int((idx_claim - 1) / total_claims * 25), f"Выполняем CoinCap проверку {idx_claim}/{total_claims}")
        claim_results.append(execute_claim_with_coincap(c, tweet_info["tweet_date"], api_key))
    judge = judge_claim_results(claim_results)
    if progress_callback:
        progress_callback(78, "Сводим вердикт")

    # Backward compatibility for existing UI blocks: derive legacy base/target from first price/direction-like claim
    first_verifiable = None
    for c in (planner_json.get("claims") or []):
        if c.get("claim_type") in ("price_target", "direction", "relative_performance", "market_cap", "volatility_risk") and c.get("coin"):
            first_verifiable = c
            break
    if first_verifiable is None and (planner_json.get("claims") or []):
        first_verifiable = (planner_json.get("claims") or [])[0]

    claim_payload = claim_to_legacy_payload(first_verifiable or {}, tweet_info["tweet_date"]) if first_verifiable else {}
    first_result = claim_results[0] if claim_results else {}
    base_result = ((first_result.get("evidence") or {}).get("base") or {}) if isinstance(first_result, dict) else {}
    target_result = ((first_result.get("evidence") or {}).get("target") or {}) if isinstance(first_result, dict) else {}

    verdict = {
        "status": judge.get("status", "unknown"),
        "checks": judge.get("checks", []),
        "counts": judge.get("counts", {}),
        "score": judge.get("score", {}),
    }

    merged = {
        "tweet": tweet_info,
        "author": build_author_info(user_data, meta_features),
        "classifier": classifier_result,
        "planner": {
            "source": planner_source,
            "claims": planner_json.get("claims", []),
            "raw_model_output": planner_raw,
        },
        "executor": {
            "claim_results": claim_results,
        },
        "judge": judge,
        "nlp": {
            "source": planner_source,
            "context_features": features,
            "claim_payload": claim_payload,
            "raw_model_output": planner_raw,
        },
        "coincap": {
            "request": {
                "base": ({**claim_payload, "relative_days": 0} if claim_payload else {}),
                "target": claim_payload,
            },
            "response": {
                "base": base_result,
                "target": target_result,
            },
            "base": compact_coincap_result(base_result),
            "target": compact_coincap_result(target_result),
        },
        "verdict": verdict,
    }

    task_description = (
        "Проверить твит-предсказание цены криптоактива: извлечь claim, "
        "сравнить с историческими данными CoinCap и оценить, сбылся ли прогноз."
    )
    if skip_llm_summary:
        summary_source = "heuristic_batch_mode"
        summary_text = build_verification_summary_heuristic(claim_payload, merged)
        summary_raw = ""
    else:
        summary_source, summary_text, summary_raw = build_verification_summary_llm(
            task_description=task_description,
            claim_payload=claim_payload,
            merged_result=merged,
            nlp_api_key=nlp_api_key,
            nlp_model=nlp_model,
            nlp_base_url=nlp_base_url,
        )
    if progress_callback:
        progress_callback(90, "Генерируем итоговый комментарий")
    merged["analysis"] = {
        "source": summary_source,
        "text": summary_text,
        "raw_model_output": summary_raw,
    }

    author_username = (merged.get("author") or {}).get("username") or (meta_features.get("username") or "author")
    author_profile = {
        "user_id": "",
        "username": author_username,
        "display_name": (merged.get("author") or {}).get("display_name") or "",
        "description": (merged.get("author") or {}).get("description") or "",
        "verified": bool((merged.get("author") or {}).get("verified")),
        "protected": False,
        "followers_count": int(meta_features.get("followers_count") or (merged.get("author") or {}).get("followers_count") or 0),
        "friends_count": int(meta_features.get("friends_count") or (merged.get("author") or {}).get("friends_count") or 0),
        "followers_friends_ratio": round(
            float(meta_features.get("followers_count") or (merged.get("author") or {}).get("followers_count") or 0)
            / max(float(meta_features.get("friends_count") or (merged.get("author") or {}).get("friends_count") or 0), 1.0),
            4,
        ),
        "statuses_count": int(meta_features.get("statuses_count") or 0),
        "favourites_count": int(meta_features.get("favourites_count") or 0),
        "listed_count": int(meta_features.get("listed_count") or 0),
        "media_count": int(meta_features.get("media_count") or 0),
        "account_age_days": int(meta_features.get("account_age_days") or (merged.get("author") or {}).get("account_age_days") or 0),
        "tweets_per_day": float(meta_features.get("tweets_per_day") or (merged.get("author") or {}).get("tweets_per_day") or 0),
    }
    stats_src, stats_txt = build_account_stats_comment_llm(
        author_username,
        author_profile,
        {"from_mode": "single_tweet"},
        nlp_api_key,
        nlp_model,
        nlp_base_url,
    )
    merged["author_profile"] = author_profile
    merged["author_profile_comment"] = {"source": stats_src, "text": stats_txt}

    if output:
        with open(output, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
    if runs_table:
        append_run_table(runs_table, tweet_info, claim_payload, merged)
    if progress_callback:
        progress_callback(100, "Готово")
    return merged


def unified_main() -> None:
    import argparse

    p = argparse.ArgumentParser(description="Fetch tweet by id, extract claim, verify via CoinCap")
    p.add_argument("tweet_id", help="Tweet ID")
    p.add_argument("--tweet-qid", default="", help="GraphQL queryId for TweetResultByRestId")
    p.add_argument("--api-key", default=os.getenv("COINCAP_API_KEY", ""), help="CoinCap API key")
    p.add_argument("--nlp-api-key", default=os.getenv("AITUNNEL_API_KEY", ""), help="AITunnel API key for DeepSeek")
    p.add_argument("--nlp-model", default=AITUNNEL_MODEL, help="LLM model name")
    p.add_argument("--nlp-base-url", default=AITUNNEL_BASE_URL, help="LLM base URL")
    p.add_argument("--tweet-out", default="tweet_output.json", help="Output json with tweet text/date")
    p.add_argument("--output", default="verification_output.json", help="Merged output json")
    p.add_argument(
        "--runs-table",
        default="verification_runs.csv",
        help="Append-only CSV with tweet, request JSON, and result JSON",
    )
    args = p.parse_args()

    if not args.api_key:
        raise SystemExit("Pass --api-key or set COINCAP_API_KEY")

    run_verification(
        args.tweet_id,
        args.api_key,
        tweet_qid=args.tweet_qid,
        nlp_api_key=args.nlp_api_key,
        nlp_model=args.nlp_model,
        nlp_base_url=args.nlp_base_url,
        tweet_out=args.tweet_out,
        output=args.output,
        runs_table=args.runs_table,
    )

    print(f"Saved tweet JSON: {args.tweet_out}")
    print(f"Saved merged JSON: {args.output}")
    print(f"Appended run row: {args.runs_table}")


if __name__ == "__main__":
    unified_main()
