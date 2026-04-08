#!/usr/bin/env python
# -*- coding: utf-8 -*-
import csv
import html
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from dateutil import parser

def clean_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()
import requests
from crypto_tweet_gate import classify_tweet_text
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

# Output paths

AITUNNEL_BASE_URL = os.getenv("AITUNNEL_BASE_URL", "https://api.aitunnel.ru/v1/")
AITUNNEL_MODEL = os.getenv("AITUNNEL_MODEL", "deepseek-v3.2")
COOKIES_FILE = os.getenv("TW_COOKIE_FILE", "cookies.json")
QIDS_FILE = os.getenv("TW_QIDS_FILE", "query_ids.json")

# Authorization bearer
DEFAULT_AUTHORIZATION = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

PROXIES = ["http://03hGHq:8dUFC8@95.164.202.193:9233"]


def coincap_capabilities_text() -> str:
    return (
        "CoinCap API capabilities summary for verification planning: "
        "Price endpoints: /price/bysymbol/{symbol} for up to 100 symbols, /price/byaddress?tokenAddress&network for token contract price. "
        "Assets: /assets (search/filter/list), /assets/{slug} (single asset metrics), /assets/{slug}/history (interval m1..d1 with start/end), /assets/{slug}/markets (venue-level liquidity), "
        "/assets/{slug}/marketcap-history and /assets/totals/total-marketcap-history for market-cap trajectories. "
        "Exchanges and Markets: /exchanges, /exchanges/{exchange}, /markets with filters (exchangeId/base/quote/asset) for microstructure context. "
        "Rates: /rates and /rates/{slug} for fiat/crypto conversion rates in USD context. "
        "Technical analysis: /ta/{slug}/sma|ema|macd|rsi (+ /latest), /ta/{slug}/vwap/latest, /ta/{slug}/candlesticks, /ta/{slug}/allLatest. "
        "Agent-friendly endpoints: /agentFriendly/history/{slug}, /agentFriendly/full_assets_by_slug, /agentFriendly/assets_search, /agentFriendly/news_top, /agentFriendly/ta/*, "
        "/agentFriendly/asset_mcap_history/{slug}, /agentFriendly/total_market_cap_history. "
        "Planning rule for LLM: propose only claims that can be verified by numeric fields from these endpoints; after data retrieval, compare claimed direction/target/relative performance vs observed metrics and output a confidence judgment."
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


def build_account_summary_llm(
    username: str,
    account_result: dict,
    nlp_api_key: str,
    nlp_model: str = AITUNNEL_MODEL,
    nlp_base_url: str = AITUNNEL_BASE_URL,
) -> tuple[str, str]:
    if not nlp_api_key or OpenAI is None:
        totals = account_result.get("totals", {})
        txt = (
            f"По аккаунту @{username}: проверено crypto-постов {totals.get('crypto_posts', 0)}, "
            f"подтверждено {totals.get('true', 0)}, не подтвердилось {totals.get('false', 0)}, "
            f"неопределенно {totals.get('unknown', 0)}."
        )
        return "heuristic", txt

    client = OpenAI(api_key=nlp_api_key, base_url=nlp_base_url)
    micro = account_result.get("micro_research", [])
    prompt = (
        "Изначальная задача: оценить надежность аккаунта на основе последних постов. "
        "Нужно учитывать только посты по крипто-тематике, для которых есть проверка через CoinCap.\n"
        f"{coincap_capabilities_text()}\n\n"
        f"Username: @{username}\n"
        f"Totals:\n{json.dumps(account_result.get('totals', {}), ensure_ascii=False, indent=2)}\n\n"
        f"Micro research per post (id/text/claim/coincap/verdict):\n{json.dumps(micro, ensure_ascii=False, indent=2)}\n\n"
        "Дай краткий вывод (4-7 предложений): насколько автор был прав, какие ограничения проверки, и итоговый уровень доверия (низкий/средний/высокий)."
    )
    response = client.chat.completions.create(
        model=nlp_model,
        temperature=0.1,
        messages=[
            {"role": "system", "content": "Ты аналитик достоверности прогнозов. Пиши по-русски, без markdown."},
            {"role": "user", "content": prompt},
        ],
    )
    return "llm", (response.choices[0].message.content or "").strip()


def extract_account_profile_from_timeline(timeline_json: dict, username: str, user_id: str) -> dict:
    user_result = (((timeline_json or {}).get("data") or {}).get("user") or {}).get("result") or {}
    legacy = user_result.get("legacy", {}) if isinstance(user_result, dict) else {}

    followers = int(legacy.get("followers_count", 0) or 0)
    friends = int(legacy.get("friends_count", 0) or 0)
    statuses = int(legacy.get("statuses_count", 0) or 0)
    favourites = int(legacy.get("favourites_count", 0) or 0)
    listed = int(legacy.get("listed_count", 0) or 0)
    media = int(legacy.get("media_count", 0) or 0)
    created_at = legacy.get("created_at", "")
    account_age_days = calculate_account_age(created_at)

    return {
        "user_id": str(user_id or user_result.get("rest_id") or ""),
        "username": legacy.get("screen_name") or username,
        "display_name": legacy.get("name") or "",
        "description": clean_text(legacy.get("description", "")),
        "verified": bool(user_result.get("is_blue_verified", False) or legacy.get("verified", False)),
        "protected": bool(legacy.get("protected", False)),
        "followers_count": followers,
        "friends_count": friends,
        "followers_friends_ratio": round((followers / max(friends, 1)), 4),
        "statuses_count": statuses,
        "favourites_count": favourites,
        "listed_count": listed,
        "media_count": media,
        "account_age_days": account_age_days,
        "tweets_per_day": round((statuses / max(account_age_days, 1)), 4),
    }


def build_account_stats_comment_llm(
    username: str,
    account_profile: dict,
    totals: dict,
    nlp_api_key: str,
    nlp_model: str = AITUNNEL_MODEL,
    nlp_base_url: str = AITUNNEL_BASE_URL,
) -> tuple[str, str]:
    if not nlp_api_key or OpenAI is None:
        p = account_profile or {}
        txt = (
            f"Профиль @{username}: подписчики={p.get('followers_count', 0)}, "
            f"подписки={p.get('friends_count', 0)}, твитов/день={p.get('tweets_per_day', 0)}. "
            "По одной статистике профиля это только ориентир, а не доказательство надежности прогнозов."
        )
        return "heuristic", txt

    client = OpenAI(api_key=nlp_api_key, base_url=nlp_base_url)
    prompt = (
        "Дай ОТДЕЛЬНЫЙ комментарий только по статистике аккаунта (без анализа содержания постов). "
        "Оцени стиль аккаунта по профилю и количественным метрикам: аудитория, активность, соотношение followers/friends, "
        "возраст аккаунта, верификация. Укажи ограничения такого анализа. Кратко: 3-5 предложений, русский язык, без markdown.\n\n"
        f"Username: @{username}\n"
        f"Account profile stats:\n{json.dumps(account_profile or {}, ensure_ascii=False, indent=2)}\n\n"
        f"Verification totals (context only):\n{json.dumps(totals or {}, ensure_ascii=False, indent=2)}"
    )
    response = client.chat.completions.create(
        model=nlp_model,
        temperature=0.1,
        messages=[
            {"role": "system", "content": "Ты аналитик статистики аккаунтов в X. Пиши по-русски, кратко и нейтрально."},
            {"role": "user", "content": prompt},
        ],
    )
    return "llm", (response.choices[0].message.content or "").strip()


def run_account_verification(
    username: str,
    api_key: str,
    *,
    count: int = 30,
    nlp_api_key: str = "",
    nlp_model: str = AITUNNEL_MODEL,
    nlp_base_url: str = AITUNNEL_BASE_URL,
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
    qids = load_query_ids()
    session = ts_build_session(cookies)
    user_id = user_by_screen_name(session, uname, qids["user_by_screen_name"])
    timeline_json = fetch_user_tweets(session, user_id, qids["user_tweets"], count=count)
    tweet_legacy_list = extract_entries(timeline_json)
    rows = tweets_to_rows(tweet_legacy_list)
    account_profile = extract_account_profile_from_timeline(timeline_json, uname, user_id)

    analyzed = []
    for row in rows[:count]:
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
            item["claim_payload"] = p.get("claim_payload", {})
            item["coincap_request"] = ((p.get("coincap") or {}).get("request") or {})
            item["coincap_response"] = ((p.get("coincap") or {}).get("response") or {})
            item["micro_summary"] = ((p.get("analysis") or {}).get("text") or "")
        micro_research.append(item)

    result["micro_research"] = micro_research
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
    return result

# Feature flags (reuse from working twitter_scraper)
FEATURES = {
    "rweb_tipjar_consumption_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "tweetypie_unmention_optimization_enabled": True,
    "vibe_api_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": False,
    "tweet_awards_web_tipping_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": False,
    "articles_preview_enabled": True,
    "responsive_web_media_download_video_enabled": False,
    "flexible_media_container_docking_enabled": True,
    "interactive_text_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_text_conversations_enabled": False,
    "responsive_web_enhance_cards_enabled": False,
    "subscriptions_verification_info_verified_since_enabled": True,
    "subscriptions_verification_info_is_identity_verified_enabled": True,
    "subscriptions_feature_can_gift_premium": False,
    "subscriptions_feature_defer_payment_enabled": False,
    "responsive_web_profile_redirect_enabled": True,
    "responsive_web_twitter_article_notes_tab_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": False,
    "highlights_tweets_tab_ui_enabled": False,
    "hidden_profile_subscriptions_enabled": False,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": False,
    "standardized_nudges_misinfo": True,
    "responsive_web_jetfuel_frame": True,
    "responsive_web_grok_show_grok_translated_post": False,
    "responsive_web_grok_community_note_auto_translation_is_enabled": False,
    "rweb_video_screen_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "premium_content_api_read_enabled": False,
    "responsive_web_grok_analysis_button_from_backend": False,
    "responsive_web_grok_imagine_annotation_enabled": False,
    "responsive_web_grok_image_annotation_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": False,
    "responsive_web_grok_share_attachment_enabled": False,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
}

def load_cookies() -> Dict[str, str]:
    if not os.path.isfile(COOKIES_FILE):
        raise FileNotFoundError(f"cookies file not found: {COOKIES_FILE}")
    with open(COOKIES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    cookies: Dict[str, str] = {}
    for item in data:
        name = item.get("name")
        if name in ("auth_token", "ct0"):
            cookies[name] = item.get("value")
    if "auth_token" not in cookies or "ct0" not in cookies:
        raise RuntimeError("auth_token or ct0 missing in cookies")
    return cookies


def build_session(cookies: Dict[str, str]) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "authorization": os.getenv("AUTHORIZATION", DEFAULT_AUTHORIZATION),
            "x-csrf-token": cookies["ct0"],
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-active-user": "yes",
            "content-type": "application/json",
            "user-agent": "Mozilla/5.0",
        }
    )
    session.cookies.update({"auth_token": cookies["auth_token"], "ct0": cookies["ct0"]})
    return session


def fetch_tweet_by_id(session: requests.Session, tweet_qid: str, tweet_id: str, proxy: Dict[str, str] | None = None) -> dict:
    variables = {
        "tweetId": str(tweet_id),
        "withCommunity": True,
        "includePromotedContent": False,
        "withVoice": True,
    }
    params = {
        "variables": json.dumps(variables, separators=(",", ":")),
        "features": json.dumps(FEATURES, separators=(",", ":")),
    }
    url = f"https://x.com/i/api/graphql/{tweet_qid}/TweetResultByRestId"
    resp = session.get(url, params=params, timeout=25, proxies=proxy)
    if not resp.ok:
        raise RuntimeError(f"TweetResultByRestId {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def load_tweet_qid() -> str:
    env_qid = os.getenv("TW_QID_TWEET", "").strip()
    if env_qid:
        if re.fullmatch(r"\d{16,}", env_qid):
            raise RuntimeError(
                "TW_QID_TWEET looks like a tweet_id, not GraphQL queryId. "
                "Set TW_QID_TWEET to the queryId from /i/api/graphql/{queryId}/TweetResultByRestId"
            )
        return env_qid
    if os.path.isfile(QIDS_FILE):
        try:
            with open(QIDS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k in ("tweet_result_by_rest_id", "tweet_by_id", "TweetResultByRestId", "tweet"):
                if data.get(k):
                    return data[k]
        except Exception:
            pass
    raise RuntimeError(
        "Tweet queryId not found. Set TW_QID_TWEET to the GraphQL queryId (not tweet_id)."
    )


def find_tweet_node(obj):
    if isinstance(obj, dict):
        legacy = obj.get("legacy")
        if isinstance(legacy, dict) and (legacy.get("full_text") or legacy.get("created_at")):
            if obj.get("rest_id") or legacy.get("id_str"):
                return obj
        for v in obj.values():
            got = find_tweet_node(v)
            if got:
                return got
    elif isinstance(obj, list):
        for it in obj:
            got = find_tweet_node(it)
            if got:
                return got
    return None


def find_tweet_node_by_id(obj, tweet_id: str):
    if isinstance(obj, dict):
        legacy = obj.get("legacy")
        rest_id = str(obj.get("rest_id") or "")
        legacy_id = str((legacy or {}).get("id_str") or "") if isinstance(legacy, dict) else ""
        if (rest_id == str(tweet_id) or legacy_id == str(tweet_id)) and isinstance(legacy, dict):
            if legacy.get("full_text") or legacy.get("text") or legacy.get("created_at"):
                return obj
        for v in obj.values():
            got = find_tweet_node_by_id(v, tweet_id)
            if got:
                return got
    elif isinstance(obj, list):
        for it in obj:
            got = find_tweet_node_by_id(it, tweet_id)
            if got:
                return got
    return None


def parse_tweet_json(payload: dict, tweet_id: str) -> dict:
    node = find_tweet_node_by_id(payload, tweet_id) or find_tweet_node(payload)
    if not node:
        raise RuntimeError("Tweet node not found in payload")
    legacy = node.get("legacy", {})
    text = clean_text(legacy.get("full_text", "") or legacy.get("text", ""))
    created_at = legacy.get("created_at", "")
    if not text or not created_at:
        raise RuntimeError("Tweet text/date missing")
    dt = parser.parse(created_at)
    return {
        "tweet_id": str(node.get("rest_id") or legacy.get("id_str") or tweet_id),
        "tweet_text": text,
        "created_at": created_at,
        "tweet_date": dt.date().isoformat(),
    }


def safe_float(v) -> float:
    try:
        return float(v)
    except Exception:
        return 0.0


def calculate_account_age(created_at_str: str) -> int:
    if not created_at_str:
        return 365
    try:
        created_at = parser.parse(created_at_str)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        days = (now - created_at).days
        if days < 0:
            return 365
        return max(1, min(days, 7300))
    except Exception:
        return 365


def get_all_fieldnames() -> List[str]:
    return [
        "followers_count",
        "friends_count",
        "followers_friends_ratio",
        "statuses_count",
        "favourites_count",
        "listed_count",
        "media_count",
        "account_age_days",
        "tweets_per_day",
        "has_custom_timelines",
        "verified",
        "protected",
        "has_professional",
        "description_length",
        "tweet_length",
        "has_hashtags",
        "has_mentions",
        "has_urls",
        "has_media",
        "is_retweet",
        "is_reply",
        "language",
        "engagement_rate",
        "reply_ratio",
        "retweet_ratio",
        "tweet_likes",
        "tweet_retweets",
        "tweet_replies",
        "is_media_like",
        "is_influencer_like",
        "is_personal_like",
        "is_bot_like",
        "has_profile_banner",
        "location_present",
        "username",
        "user_display_name",
        "tweet_text",
        "tweet_id",
    ]


def build_verification_summary_heuristic(claim_payload: dict, merged_result: dict) -> str:
    verdict = (merged_result.get("verdict") or {}).get("status", "unknown")
    checks = (merged_result.get("verdict") or {}).get("checks", [])
    ok_count = sum(1 for c in checks if c.get("ok"))
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
    return (
        f"Проверка прогноза по {coin} на горизонте {horizon} дней: {outcome} "
        f"Пройдено проверок: {ok_count}/{all_count}. {trust}"
    )


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


def _extract_user_result(tweet_result: dict) -> dict:
    def unwrap_user(candidate):
        cur = candidate
        for _ in range(6):
            if not isinstance(cur, dict):
                return {}
            if isinstance(cur.get("legacy"), dict):
                return cur
            if isinstance(cur.get("result"), dict):
                cur = cur["result"]
                continue
            if isinstance(cur.get("user"), dict):
                cur = cur["user"]
                continue
            break
        return cur if isinstance(cur, dict) and isinstance(cur.get("legacy"), dict) else {}

    core = tweet_result.get("core", {}) if isinstance(tweet_result, dict) else {}
    user_results = core.get("user_results", {}) if isinstance(core, dict) else {}
    direct = unwrap_user(user_results.get("result", {}) if isinstance(user_results, dict) else {})
    if direct:
        return direct

    def walk(obj):
        if isinstance(obj, dict):
            candidate = unwrap_user(obj)
            if candidate:
                return candidate
            for v in obj.values():
                got = walk(v)
                if got:
                    return got
        elif isinstance(obj, list):
            for it in obj:
                got = walk(it)
                if got:
                    return got
        return {}

    return walk(tweet_result)


def extract_features_from_user(user_data: dict, tweet_legacy: dict, tweet_result: dict) -> Dict[str, object]:
    user_legacy = user_data.get("legacy", {}) if isinstance(user_data, dict) else {}
    user_core = user_data.get("core", {}) if isinstance(user_data, dict) else {}

    tweet_id = tweet_result.get("rest_id") or tweet_legacy.get("id_str")
    followers_count = int(user_legacy.get("followers_count", 0) or 0)
    friends_count = max(int(user_legacy.get("friends_count", 1) or 1), 1)
    statuses_count = int(user_legacy.get("statuses_count", 0) or 0)
    favourites_count = int(user_legacy.get("favourites_count", 0) or 0)
    listed_count = int(user_legacy.get("listed_count", 0) or 0)
    media_count = int(user_legacy.get("media_count", 0) or 0)

    account_created_at = user_core.get("created_at", user_legacy.get("created_at", ""))
    account_age_days = calculate_account_age(account_created_at)

    tweets_per_day = safe_float(statuses_count) / max(account_age_days, 1)
    followers_friends_ratio = safe_float(followers_count) / max(safe_float(friends_count), 1)
    followers_friends_ratio = min(followers_friends_ratio, 10000)
    tweets_per_day = min(tweets_per_day, 1000)

    tweet_entities = tweet_legacy.get("entities", {}) if isinstance(tweet_legacy, dict) else {}
    favorite_count = int(tweet_legacy.get("favorite_count", 0) or 0)
    retweet_count = int(tweet_legacy.get("retweet_count", 0) or 0)
    reply_count = int(tweet_legacy.get("reply_count", 0) or 0)
    engagement_rate = safe_float(favorite_count + retweet_count) / max(safe_float(followers_count), 1)

    tweet_text = clean_text(tweet_legacy.get("full_text", "") or tweet_legacy.get("text", ""))
    user_description = clean_text(user_legacy.get("description", ""))

    features = {
        "followers_count": followers_count,
        "friends_count": friends_count,
        "followers_friends_ratio": round(followers_friends_ratio, 2),
        "statuses_count": statuses_count,
        "favourites_count": favourites_count,
        "listed_count": listed_count,
        "media_count": media_count,
        "account_age_days": account_age_days,
        "tweets_per_day": round(tweets_per_day, 2),
        "has_custom_timelines": int(bool(user_legacy.get("has_custom_timelines", False))),
        "verified": int(bool(user_data.get("is_blue_verified", False) or user_legacy.get("verified", False))),
        "protected": int(bool(user_legacy.get("protected", False))),
        "has_professional": int(bool(user_data.get("professional"))),
        "description_length": len(user_description),
        "tweet_length": len(tweet_text),
        "has_hashtags": int(bool(tweet_entities.get("hashtags"))),
        "has_mentions": int(bool(tweet_entities.get("user_mentions"))),
        "has_urls": int(bool(tweet_entities.get("urls"))),
        "has_media": int(bool(tweet_entities.get("media"))),
        "is_retweet": int(bool(tweet_legacy.get("retweeted_status_result"))),
        "is_reply": int(tweet_legacy.get("in_reply_to_user_id") is not None),
        "language": tweet_legacy.get("lang", "unknown"),
        "engagement_rate": round(engagement_rate, 4),
        "reply_ratio": round(safe_float(reply_count) / max(statuses_count, 1), 4),
        "retweet_ratio": round(safe_float(retweet_count) / max(statuses_count, 1), 4),
        "tweet_likes": favorite_count,
        "tweet_retweets": retweet_count,
        "tweet_replies": reply_count,
        "is_media_like": int(media_count > statuses_count * 0.3 if statuses_count else False),
        "is_influencer_like": int(followers_count > 10000 and followers_friends_ratio > 10),
        "is_personal_like": int(followers_count < 5000 and statuses_count > 1000),
        "is_bot_like": int(tweets_per_day > 50),
        "has_profile_banner": int(bool(user_legacy.get("profile_banner_url"))),
        "location_present": int(bool((user_data.get("location", {}) if isinstance(user_data.get("location"), dict) else {}).get("location") or user_legacy.get("location"))),
        "username": user_core.get("screen_name") or user_legacy.get("screen_name") or "Unknown",
        "user_display_name": user_core.get("name") or user_legacy.get("name") or "Unknown",
        "tweet_text": tweet_text,
        "tweet_id": str(tweet_id or ""),
    }
    return features


def build_author_info(user_data: dict, meta_features: dict) -> dict:
    user_legacy = user_data.get("legacy", {}) if isinstance(user_data, dict) else {}
    return {
        "username": meta_features.get("username") or user_legacy.get("screen_name"),
        "display_name": meta_features.get("user_display_name") or user_legacy.get("name"),
        "followers_count": meta_features.get("followers_count"),
        "friends_count": meta_features.get("friends_count"),
        "verified": meta_features.get("verified"),
        "account_age_days": meta_features.get("account_age_days"),
        "tweets_per_day": meta_features.get("tweets_per_day"),
        "engagement_rate": meta_features.get("engagement_rate"),
        "is_influencer_like": meta_features.get("is_influencer_like"),
        "is_bot_like": meta_features.get("is_bot_like"),
        "description": clean_text(user_legacy.get("description", "")),
    }


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


def evaluate_claim(claim_payload: dict, base_price: float | None, target_price: float | None) -> dict:
    claim = claim_payload.get("claim", {})
    target_claim = claim.get("target_price_usd")
    direction = claim.get("direction")
    checks = []

    if target_claim is not None and target_price is not None:
        tol = 0.05
        ok = abs(target_price - target_claim) / max(target_claim, 1e-9) <= tol
        checks.append({"name": "target_price_5pct", "ok": ok})

    if direction and base_price is not None and target_price is not None:
        if direction == "up":
            checks.append({"name": "direction_up", "ok": target_price > base_price})
        elif direction == "down":
            checks.append({"name": "direction_down", "ok": target_price < base_price})

    if not checks:
        return {"status": "unknown", "reason": "Not enough structured claim info"}
    return {"status": "true" if all(c["ok"] for c in checks) else "false", "checks": checks}


def compact_coincap_result(result: dict) -> dict:
    raw_api = result.get("raw_api") if isinstance(result, dict) else None
    chosen = raw_api.get("chosen") if isinstance(raw_api, dict) else None
    chosen_ts = chosen[0] if isinstance(chosen, list) and len(chosen) >= 2 else None

    return {
        "asset_id": result.get("asset_id"),
        "target_date": result.get("target_date"),
        "price_usd": result.get("price_usd"),
        "success": result.get("success"),
        "status_code": result.get("status_code"),
        "chosen_timestamp_ms": chosen_ts,
        "error": result.get("error"),
    }


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
) -> dict:
    from coingecko_query import process_input

    if not api_key:
        raise RuntimeError("Pass CoinCap API key")

    cookies = load_cookies()
    session = build_session(cookies)
    if tweet_qid:
        os.environ["TW_QID_TWEET"] = tweet_qid
    loaded_tweet_qid = load_tweet_qid()

    raw_payload = None
    last_err = None
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
    if tweet_out:
        with open(tweet_out, "w", encoding="utf-8") as f:
            json.dump(tweet_info, f, ensure_ascii=False, indent=2)

    tweet_node = find_tweet_node_by_id(raw_payload, tweet_id) or find_tweet_node(raw_payload) or {}
    text_features = extract_features(tweet_info["tweet_text"])
    user_data = _extract_user_result(tweet_node)
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
        return merged

    features = {
        "text_features": text_features,
        "meta_features": meta_features,
    }

    claim_payload, nlp_source, nlp_raw = extract_claim_struct(
        tweet_info["tweet_text"],
        tweet_info["tweet_date"],
        nlp_api_key=nlp_api_key,
        nlp_model=nlp_model,
        nlp_base_url=nlp_base_url,
        context_features=features,
    )

    target_result = process_input(claim_payload, api_key)[0]
    base_payload = dict(claim_payload)
    base_payload["relative_days"] = 0
    base_result = process_input(base_payload, api_key)[0]

    verdict = evaluate_claim(claim_payload, base_result.get("price_usd"), target_result.get("price_usd"))

    merged = {
        "tweet": tweet_info,
        "author": build_author_info(user_data, meta_features),
        "classifier": classifier_result,
        "nlp": {
            "source": nlp_source,
            "context_features": features,
            "claim_payload": claim_payload,
            "raw_model_output": nlp_raw,
        },
        "coincap": {
            "request": {
                "base": base_payload,
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
    summary_source, summary_text, summary_raw = build_verification_summary_llm(
        task_description=task_description,
        claim_payload=claim_payload,
        merged_result=merged,
        nlp_api_key=nlp_api_key,
        nlp_model=nlp_model,
        nlp_base_url=nlp_base_url,
    )
    merged["analysis"] = {
        "source": summary_source,
        "text": summary_text,
        "raw_model_output": summary_raw,
    }

    if output:
        with open(output, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
    if runs_table:
        append_run_table(runs_table, tweet_info, claim_payload, merged)
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
