#!/usr/bin/env python
# -*- coding: utf-8 -*-

import json
import re
from typing import Any, Dict, List, Optional

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


def _extract_json_object_from_text(text: str) -> Dict[str, Any]:
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
            raise RuntimeError("Model did not return JSON")
        return json.loads(m.group(0))


def is_news_like_tweet(tweet_text: str) -> bool:
    low = (tweet_text or "").lower()
    # News / policy / statement style markers
    markers = [
        "just in",
        "breaking",
        "fun fact",
        "years ago",
        "for the first time",
        "on this day",
        "says",
        "announces",
        "regulation",
        "sec",
        "eu",
        "government",
        "ministry",
        "ban",
        "approval",
        "lawsuit",
        "partnership",
        "hacked",
        "exploit",
    ]
    return any(m in low for m in markers)


def verify_news_with_perplexity(
    tweet_text: str,
    nlp_api_key: str,
    *,
    nlp_base_url: str = "https://api.aitunnel.ru/v1/",
    model: str = "sonar",
    query: str = "",
    expected_claims: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Verify news-like claims in a tweet using Perplexity Sonar via AITunnel-compatible OpenAI client.
    Returns strict JSON-ready dict.
    """
    if not tweet_text:
        return {
            "source": "news_verifier",
            "status": "unknown",
            "confidence_0_100": 0,
            "used": False,
            "reason": "empty_tweet_text",
            "summary": "",
            "sources": [],
            "limitations": ["Empty tweet text"],
        }

    if not nlp_api_key or OpenAI is None:
        return {
            "source": "news_verifier",
            "status": "unknown",
            "confidence_0_100": 0,
            "used": False,
            "reason": "missing_api_or_openai",
            "summary": "",
            "sources": [],
            "limitations": ["Missing API key or openai package"],
        }

    client = OpenAI(api_key=nlp_api_key, base_url=nlp_base_url)

    schema = {
        "status": "true|false|mixed|unknown",
        "confidence_0_100": "int 0..100",
        "summary": "short russian summary",
        "key_facts": ["list of key facts"],
        "sources": [
            {
                "title": "source title",
                "url": "https://...",
                "published_at": "ISO or empty",
                "supports_claim": "true|false|mixed",
                "snippet": "short quote/extract",
            }
        ],
        "limitations": ["list of limitations"],
    }

    system_prompt = (
        "Ты факт-чекер новостей. Используй web-доступ модели и верни только JSON. "
        "Нельзя придумывать источники: указывай только реально найденные URL."
    )

    effective_query = (query or "").strip() or tweet_text
    expected_claims = expected_claims or []

    user_prompt = (
        "Проверь новостное утверждение из твита и верни строго JSON по схеме:\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
        f"Твит:\n{tweet_text}\n\n"
        f"Поисковый запрос для web-поиска:\n{effective_query}\n\n"
        f"Ожидания автора (из первого прогона LLM):\n{json.dumps(expected_claims, ensure_ascii=False, indent=2)}\n\n"
        "Правила:\n"
        "1) Минимум 2 источника, если найдены.\n"
        "2) Если источников мало или они конфликтуют — status=unknown или mixed.\n"
        "3) confidence_0_100 отражает качество источников и согласованность фактов.\n"
        "4) В key_facts явно сопоставь найденные факты с ожиданиями автора.\n"
        "5) summary на русском, 2-4 предложения.\n"
        "6) Ответ ТОЛЬКО JSON, без markdown."
    )

    try:
        resp = client.chat.completions.create(
            model=model,
            temperature=0,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw = (resp.choices[0].message.content or "").strip()
        parsed = _extract_json_object_from_text(raw)
    except Exception as exc:  # noqa: BLE001
        return {
            "source": "news_verifier",
            "status": "unknown",
            "confidence_0_100": 0,
            "used": True,
            "reason": f"api_error:{exc}",
            "summary": "",
            "sources": [],
            "limitations": ["Perplexity request failed"],
            "raw_model_output": "",
        }

    status = str(parsed.get("status") or "unknown").lower()
    if status not in {"true", "false", "mixed", "unknown"}:
        status = "unknown"

    conf = parsed.get("confidence_0_100", 0)
    try:
        conf = int(conf)
    except Exception:
        conf = 0
    conf = max(0, min(conf, 100))

    out = {
        "source": "perplexity_sonar",
        "model": model,
        "query_used": effective_query,
        "expected_claims": expected_claims,
        "used": True,
        "status": status,
        "confidence_0_100": conf,
        "summary": str(parsed.get("summary") or "").strip(),
        "key_facts": parsed.get("key_facts") if isinstance(parsed.get("key_facts"), list) else [],
        "sources": parsed.get("sources") if isinstance(parsed.get("sources"), list) else [],
        "limitations": parsed.get("limitations") if isinstance(parsed.get("limitations"), list) else [],
        "raw_model_output": raw,
    }
    return out
