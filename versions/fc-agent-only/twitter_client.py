#!/usr/bin/env python
# -*- coding: utf-8 -*-

import html
import json
import os
import re
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict

import requests
from dateutil import parser

try:
    from dotenv import load_dotenv
except Exception:
    load_dotenv = None

if load_dotenv is not None:
    _BASE_DIR = Path(__file__).resolve().parent
    load_dotenv(dotenv_path=_BASE_DIR / ".env", override=False)

COOKIES_FILE = os.getenv("TW_COOKIE_FILE", "cookies.json")
QIDS_FILE = os.getenv("TW_QIDS_FILE", "query_ids.json")

DEFAULT_AUTHORIZATION = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

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


def clean_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


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
        return max(1, days)
    except Exception:
        return 365


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
    last_err = None
    for attempt in range(4):
        try:
            resp = session.get(url, params=params, timeout=25, proxies=proxy)
            if resp.ok:
                return resp.json()
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(0.8 * (attempt + 1))
                continue
            raise RuntimeError(f"TweetResultByRestId {resp.status_code}: {resp.text[:300]}")
        except requests.RequestException as exc:
            last_err = str(exc)
            time.sleep(0.8 * (attempt + 1))
            continue
    raise RuntimeError(f"TweetResultByRestId connection failed: {last_err}")


def load_tweet_qid() -> str:
    def _validate_qid(value: str, source: str) -> str:
        qid = (value or "").strip()
        if not qid:
            raise RuntimeError(f"Tweet queryId missing in {source}")
        if re.fullmatch(r"\d{16,}", qid):
            raise RuntimeError(
                f"Invalid Tweet queryId in {source}: looks like tweet_id. "
                "Use /i/api/graphql/{queryId}/TweetResultByRestId"
            )
        return qid

    env_qid = os.getenv("TW_QID_TWEET", "")
    if env_qid:
        return _validate_qid(env_qid, "TW_QID_TWEET")

    if not os.path.isfile(QIDS_FILE):
        raise RuntimeError(
            "Tweet queryId not found: set TW_QID_TWEET in .env or create query_ids.json with key tweet_result_by_rest_id"
        )

    with open(QIDS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError(f"Invalid {QIDS_FILE}: expected JSON object")

    if "tweet_result_by_rest_id" not in data:
        raise RuntimeError(f"Invalid {QIDS_FILE}: required key 'tweet_result_by_rest_id' is missing")

    return _validate_qid(str(data.get("tweet_result_by_rest_id") or ""), f"{QIDS_FILE}:tweet_result_by_rest_id")


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


def extract_user_result(tweet_result: dict) -> dict:
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

    account_created_at = user_core.get("created_at") or user_legacy.get("created_at") or ""
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

    return {
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
