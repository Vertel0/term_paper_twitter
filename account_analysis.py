#!/usr/bin/env python
# -*- coding: utf-8 -*-

import html
import json
import re
from datetime import datetime, timezone

from dateutil import parser

from claim_planner import market_data_capabilities_text

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


def _clean_text(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _calculate_account_age(created_at_str: str) -> int:
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


def _unwrap_user(candidate):
    cur = candidate
    for _ in range(8):
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


def _find_best_user_result(timeline_json: dict, username: str, user_id: str) -> dict:
    root = (((timeline_json or {}).get("data") or {}).get("user") or {}).get("result") or {}
    direct = _unwrap_user(root)
    if direct:
        return direct

    wanted_username = (username or "").strip().lstrip("@").lower()
    wanted_user_id = str(user_id or "").strip()
    candidates = []

    def walk(obj):
        if isinstance(obj, dict):
            c = _unwrap_user(obj)
            if c:
                candidates.append(c)
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for it in obj:
                walk(it)

    walk(timeline_json)
    if not candidates:
        return {}

    def score(c: dict) -> int:
        s = 0
        legacy = c.get("legacy", {}) if isinstance(c, dict) else {}
        rest_id = str(c.get("rest_id") or "")
        screen_name = str(legacy.get("screen_name") or "").lower()
        if wanted_user_id and rest_id == wanted_user_id:
            s += 10
        if wanted_username and screen_name == wanted_username:
            s += 8
        if legacy.get("followers_count") is not None:
            s += 1
        return s

    candidates.sort(key=score, reverse=True)
    return candidates[0]


def build_account_summary_llm(
    username: str,
    account_result: dict,
    nlp_api_key: str,
    nlp_model: str,
    nlp_base_url: str,
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
        "Нужно учитывать только посты по крипто-тематике, для которых есть проверка через Binance data-api.\n"
        f"{market_data_capabilities_text()}\n\n"
        f"Username: @{username}\n"
        f"Totals:\n{json.dumps(account_result.get('totals', {}), ensure_ascii=False, indent=2)}\n\n"
        f"Micro research per post (id/text/claim/binance/verdict):\n{json.dumps(micro, ensure_ascii=False, indent=2)}\n\n"
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
    user_result = _find_best_user_result(timeline_json, username, user_id)
    legacy = user_result.get("legacy", {}) if isinstance(user_result, dict) else {}

    followers = int(legacy.get("followers_count", 0) or 0)
    friends = int(legacy.get("friends_count", 0) or 0)
    statuses = int(legacy.get("statuses_count", 0) or 0)
    favourites = int(legacy.get("favourites_count", 0) or 0)
    listed = int(legacy.get("listed_count", 0) or 0)
    media = int(legacy.get("media_count", 0) or 0)
    created_at = legacy.get("created_at") or (user_result.get("core", {}) or {}).get("created_at") or ""
    account_age_days = _calculate_account_age(created_at)

    return {
        "user_id": str(user_id or user_result.get("rest_id") or ""),
        "username": legacy.get("screen_name") or username,
        "display_name": legacy.get("name") or "",
        "description": _clean_text(legacy.get("description", "")),
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
    nlp_model: str,
    nlp_base_url: str,
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
