import csv
import json
import os
import re
import sys
import html
import time
import requests
from typing import List, Dict, Optional, Tuple

"""
Twitter user timeline scraper (console, interactive).

Как использовать один раз и дальше без ручных действий:
1) Экспортируйте cookies своей авторизованной сессии X (Twitter) в файл cookies.json (формат экспорта из браузера: список объектов {"name": ..., "value": ...}).
   Подойдут расширения EditThisCookie / Cookie-Editor. Должны быть куки auth_token и ct0.
2) Положите cookies.json рядом со скриптом или укажите путь через переменную TW_COOKIE_FILE.
3) Запустите: python twitter_scraper.py, введите username без @. Скрипт сам возьмет нужные токены из cookies.json
   и выполнит запросы GraphQL UserByScreenName + UserTweets (20 последних твитов), сохранив CSV <username>_tweets.csv.

Если не хотите экспортировать куки — можно задать через окружение:
  AUTH_TOKEN, CT0, AUTHORIZATION (bearer) — но bearer обычно стабильный публичный, оставлен по умолчанию.

Ограничения: требуется действующая авторизованная сессия (auth_token, ct0). Один раз экспортировали — можно дергать любых пользователей.
"""

DEFAULT_AUTHORIZATION = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

FEATURES = {
    # Базовые
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
    # Новые флаги, которые GraphQL требует не-null
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
    # Дополнительно, чтобы избежать null
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

# Актуальные queryId могут меняться. Если Twitter вернул 400/404, возьмите свежие из DevTools (фильтр UserByScreenName / UserTweets)
# Теперь: можно задать через env TW_QID_USER / TW_QID_TWEETS или через файл query_ids.json:
# {"user_by_screen_name": "...", "user_tweets": "..."}
DEFAULT_QIDS = {
    "user_by_screen_name": "-oaLodhGbbnzJBACb1kk2Q",  # может устареть
    "user_tweets": "-V26I6Pb5xDZ3C7BWwCQ_Q",      # может устареть
}


def load_cookies() -> Dict[str, str]:
    """Ищем куки в cookies.json или окружении."""
    cookie_file = os.getenv("TW_COOKIE_FILE", "cookies.json")
    cookies = {"auth_token": os.getenv("AUTH_TOKEN"), "ct0": os.getenv("CT0")}

    if os.path.isfile(cookie_file):
        try:
            with open(cookie_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data:
                if item.get("name") in ("auth_token", "ct0"):
                    cookies[item["name"]] = item.get("value")
        except Exception as e:
            print(f"Не удалось прочитать {cookie_file}: {e}")

    if not cookies.get("auth_token") or not cookies.get("ct0"):
        raise RuntimeError("Нужны куки auth_token и ct0 (cookies.json или переменные AUTH_TOKEN/CT0)")
    return cookies


def load_query_ids() -> Dict[str, str]:
    """Берем queryId из env или query_ids.json, иначе дефолты."""
    qids = DEFAULT_QIDS.copy()
    cfg_file = os.getenv("TW_QIDS_FILE", "query_ids.json")

    if os.path.isfile(cfg_file):
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            qids["user_by_screen_name"] = data.get("user_by_screen_name", qids["user_by_screen_name"])
            qids["user_tweets"] = data.get("user_tweets", qids["user_tweets"])
        except Exception as e:
            print(f"Не удалось прочитать {cfg_file}: {e}")

    qids["user_by_screen_name"] = os.getenv("TW_QID_USER", qids["user_by_screen_name"])
    qids["user_tweets"] = os.getenv("TW_QID_TWEETS", qids["user_tweets"])

    return qids


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def build_session(cookies: Dict[str, str]) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "authorization": os.getenv("AUTHORIZATION", DEFAULT_AUTHORIZATION),
        "x-csrf-token": cookies["ct0"],
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-active-user": "yes",
        "content-type": "application/json",
        "user-agent": "Mozilla/5.0",
    })
    session.cookies.update({"auth_token": cookies["auth_token"], "ct0": cookies["ct0"]})
    return session


def _get_json_with_retries(
    session: requests.Session,
    url: str,
    params: Dict[str, str],
    *,
    timeout: int = 20,
    op_name: str = "request",
    retries: int = 4,
) -> Dict:
    last_err = None
    for attempt in range(retries):
        try:
            resp = session.get(url, params=params, timeout=timeout)
            if resp.ok:
                return resp.json()
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(0.8 * (attempt + 1))
                continue
            print(f"{op_name} {resp.status_code}: {resp.text[:400]} ...")
            resp.raise_for_status()
        except requests.RequestException as exc:
            last_err = str(exc)
            time.sleep(0.8 * (attempt + 1))
            continue
    raise RuntimeError(f"{op_name} failed after retries: {last_err}")


def user_by_screen_name(session: requests.Session, username: str, qid: str) -> str:
    payload = {
        "variables": {"screen_name": username, "withSafetyModeUserFields": True},
        "features": FEATURES,
    }
    url = f"https://x.com/i/api/graphql/{qid}/UserByScreenName"
    data = _get_json_with_retries(
        session,
        url,
        {
            "variables": json.dumps(payload["variables"], separators=(",", ":")),
            "features": json.dumps(payload["features"], separators=(",", ":")),
        },
        timeout=15,
        op_name="UserByScreenName",
    )
    return data["data"]["user"]["result"]["rest_id"]


def fetch_user_tweets(session: requests.Session, user_id: str, qid: str, count: int = 20):
    requested = max(1, min(int(count or 20), 200))
    per_page = min(max(requested, 20), 100)

    url = f"https://x.com/i/api/graphql/{qid}/UserTweets"
    cursor = None
    pages = []
    seen_ids = set()
    collected = []

    for _ in range(8):
        variables = {
            "userId": user_id,
            "count": per_page,
            "includePromotedContent": False,
            "withQuickPromoteEligibilityTweetFields": True,
            "withVoice": True,
            "withV2Timeline": True,
        }
        if cursor:
            variables["cursor"] = cursor

        payload = {"variables": variables, "features": FEATURES}
        page = _get_json_with_retries(
            session,
            url,
            {
                "variables": json.dumps(payload["variables"], separators=(",", ":")),
                "features": json.dumps(payload["features"], separators=(",", ":")),
            },
            timeout=20,
            op_name="UserTweets",
        )
        pages.append(page)

        items, next_cursor = extract_entries_with_cursor(page)
        for item in items:
            legacy = item.get("legacy", {}) if isinstance(item, dict) else {}
            tid = str(item.get("rest_id") or legacy.get("id_str") or "")
            if not tid or tid in seen_ids:
                continue
            seen_ids.add(tid)
            collected.append(item)

        if len(collected) >= requested:
            break
        if not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor

    first_page = pages[0] if pages else {}
    first_page["_aggregated_items"] = collected[:requested]
    return first_page


def _unwrap_tweet_result(node: Dict) -> Dict:
    cur = node
    for _ in range(8):
        if not isinstance(cur, dict):
            return {}
        if isinstance(cur.get("legacy"), dict):
            return cur
        # Common wrappers in X GraphQL
        if isinstance(cur.get("tweet"), dict):
            cur = cur["tweet"]
            continue
        if isinstance(cur.get("result"), dict):
            cur = cur["result"]
            continue
        if isinstance(cur.get("tweet_results"), dict) and isinstance(cur["tweet_results"].get("result"), dict):
            cur = cur["tweet_results"]["result"]
            continue
        break
    return cur if isinstance(cur, dict) and isinstance(cur.get("legacy"), dict) else {}


def extract_entries_with_cursor(timeline_json) -> Tuple[List[Dict], Optional[str]]:
    user_result = timeline_json.get("data", {}).get("user", {}).get("result", {})

    timeline_obj = None
    if "timeline_v2" in user_result:
        timeline_obj = user_result["timeline_v2"].get("timeline")
    elif "timeline" in user_result:
        timeline_obj = user_result["timeline"].get("timeline")

    if not timeline_obj:
        return [], None

    instructions = timeline_obj.get("instructions", [])
    items: List[Dict] = []
    next_cursor = None

    for instr in instructions:
        if instr.get("type") != "TimelineAddEntries":
            continue
        for entry in instr.get("entries", []):
            entry_id = entry.get("entryId", "")
            if entry_id.startswith("tweet-"):
                try:
                    item = entry["content"]["itemContent"]["tweet_results"]["result"]
                except Exception:
                    continue
                norm = _unwrap_tweet_result(item if isinstance(item, dict) else {})
                if norm:
                    items.append(norm)
            elif entry_id.startswith("cursor-bottom"):
                content = entry.get("content", {})
                cur_val = content.get("value")
                if not cur_val and isinstance(content.get("itemContent"), dict):
                    cur_val = content["itemContent"].get("value")
                if cur_val:
                    next_cursor = cur_val

    return items, next_cursor


def extract_entries(timeline_json) -> List[Dict]:
    if isinstance(timeline_json, dict) and isinstance(timeline_json.get("_aggregated_items"), list):
        return timeline_json.get("_aggregated_items") or []

    user_result = timeline_json.get("data", {}).get("user", {}).get("result", {})

    timeline_obj = None
    if "timeline_v2" in user_result:
        timeline_obj = user_result["timeline_v2"].get("timeline")
    elif "timeline" in user_result:
        timeline_obj = user_result["timeline"].get("timeline")

    if not timeline_obj:
        # fallback debug
        print("Нет timeline_v2 / timeline в ответе. Ключи user_result:", list(user_result.keys()))
        return []

    instructions = timeline_obj.get("instructions", [])
    tweets = []
    for instr in instructions:
        if instr.get("type") == "TimelineAddEntries":
            for entry in instr.get("entries", []):
                if entry.get("entryId", "").startswith("tweet-"):
                    item = entry["content"]["itemContent"]["tweet_results"]["result"]
                    norm = _unwrap_tweet_result(item if isinstance(item, dict) else {})
                    if norm:
                        tweets.append(norm)
    return tweets


def tweets_to_rows(tweets: List[Dict]) -> List[Dict]:
    rows = []
    seen_ids = set()
    for tw in tweets:
        if not isinstance(tw, dict):
            continue
        legacy = tw.get("legacy", tw)
        if not isinstance(legacy, dict):
            continue

        tweet_id = str(tw.get("rest_id") or legacy.get("id_str") or "")
        if not tweet_id or tweet_id in seen_ids:
            continue
        seen_ids.add(tweet_id)

        note_text = (
            (((tw.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}).get("text")
            if isinstance(tw, dict)
            else ""
        )
        full_text = clean_text(note_text or legacy.get("full_text") or legacy.get("text") or "")

        entities = tw.get("entities", {})
        if not isinstance(entities, dict):
            entities = legacy.get("entities", {}) if isinstance(legacy.get("entities", {}), dict) else {}
        hashtags = [h.get("text") for h in entities.get("hashtags", [])]
        mentions = [m.get("screen_name") for m in entities.get("user_mentions", [])]
        urls = [u.get("expanded_url") for u in entities.get("urls", [])]
        views_count = 0
        try:
            views_count = int((((tw.get("views") or {}).get("count")) or legacy.get("views") or 0) or 0)
        except Exception:
            views_count = 0
        rows.append({
            "tweet_id": tweet_id,
            "created_at": legacy.get("created_at"),
            "full_text": full_text,
            "favorite_count": legacy.get("favorite_count", 0),
            "retweet_count": legacy.get("retweet_count", 0),
            "reply_count": legacy.get("reply_count", 0),
            "quote_count": legacy.get("quote_count", 0),
            "bookmark_count": legacy.get("bookmark_count", 0),
            "views_count": views_count,
            "lang": legacy.get("lang"),
            "possibly_sensitive": legacy.get("possibly_sensitive", False),
            "hashtags": ",".join(hashtags),
            "mentions": ",".join(mentions),
            "urls": ",".join(urls),
        })
    return rows


def save_csv(rows: List[Dict], filename: str) -> None:
    if not rows:
        print("Нет твитов для сохранения.")
        return
    fieldnames = list(rows[0].keys())
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)
    print(f"Сохранено {len(rows)} строк в {filename}")


def main():
    username = input("Введите username (без @): ").strip().lstrip("@")
    if not username:
        print("Username пуст.")
        return
    try:
        cookies = load_cookies()
        qids = load_query_ids()
        session = build_session(cookies)
        user_id = user_by_screen_name(session, username, qids["user_by_screen_name"])
        print(f"User id: {user_id}")
        data = fetch_user_tweets(session, user_id, qids["user_tweets"], count=20)
        tweets = extract_entries(data)
        rows = tweets_to_rows(tweets)
        fname = f"{username}_tweets.csv"
        save_csv(rows, fname)
    except Exception as exc:  # noqa: BLE001
        print(f"Ошибка: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
