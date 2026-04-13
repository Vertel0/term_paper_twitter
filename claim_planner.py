#!/usr/bin/env python
# -*- coding: utf-8 -*-

import json
import os
import re
from typing import List, Optional

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


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


ALLOWED_CLAIM_TYPES = {
    "price_target",
    "direction",
    "relative_performance",
    "market_cap",
    "volatility_risk",
    "timeframe_only",
}


def _has_explicit_long_horizon(tweet_text: str) -> bool:
    low = (tweet_text or "").lower()
    patterns = [
        r"\b30\s*d(ay|ays)?\b",
        r"\bmonth\b|\bmonthly\b",
        r"\bчерез\s+месяц\b|\bв\s+течение\s+месяца\b",
        r"\b2\s*weeks?\b|\b14\s*d(ay|ays)?\b",
    ]
    return any(re.search(p, low) for p in patterns)


def _apply_horizon_policy(tweet_text: str, planned: dict) -> dict:
    claims = (planned or {}).get("claims") or []
    explicit_long = _has_explicit_long_horizon(tweet_text)
    out = []
    for c in claims:
        cc = dict(c)
        rel = int(cc.get("relative_days", 7) or 7)
        rel = max(0, min(rel, 365))
        if not explicit_long and rel > 14:
            rel = 14
            if not cc.get("rationale"):
                cc["rationale"] = "Horizon normalized to 14 days by policy"
        cc["relative_days"] = rel
        out.append(cc)
    return {"planner_version": (planned or {}).get("planner_version", "v2"), "claims": out}


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


def build_candidate_claims(tweet_text: str, tweet_date: str, features: dict) -> List[dict]:
    tf = (features or {}).get("text_features", {})
    cands: List[dict] = []

    cashtags = tf.get("cashtags") or []
    coin = cashtags[0].lower() if cashtags else None
    rel_days = int(tf.get("relative_days_hint") or 7)
    rel_days = max(0, min(rel_days, 365))
    target_price = tf.get("target_price_hint_usd")
    direction = tf.get("direction_hint")

    if coin and target_price is not None:
        cands.append(
            {
                "claim_type": "price_target",
                "coin": coin,
                "relative_days": rel_days,
                "target_price_usd": target_price,
                "direction": direction,
                "cannot_verify_reason": "",
            }
        )
    if coin and direction in ("up", "down"):
        cands.append(
            {
                "claim_type": "direction",
                "coin": coin,
                "relative_days": rel_days,
                "direction": direction,
                "cannot_verify_reason": "",
            }
        )

    text_low = (tweet_text or "").lower()
    if any(k in text_low for k in ["vs", "versus", "outperform", "лучше", "хуже"]):
        if len(cashtags) >= 2:
            cands.append(
                {
                    "claim_type": "relative_performance",
                    "coin": cashtags[0].lower(),
                    "comparison_coin": cashtags[1].lower(),
                    "relative_days": rel_days,
                    "expected_outperformance": True,
                    "cannot_verify_reason": "",
                }
            )

    if any(k in text_low for k in ["market cap", "mcap", "капитализац", "dominance", "доминац"]):
        cands.append(
            {
                "claim_type": "market_cap",
                "coin": coin,
                "relative_days": rel_days,
                "cannot_verify_reason": "",
            }
        )

    if any(k in text_low for k in ["volatile", "volatility", "риск", "волатиль"]):
        cands.append(
            {
                "claim_type": "volatility_risk",
                "coin": coin,
                "relative_days": rel_days,
                "cannot_verify_reason": "",
            }
        )

    if not cands:
        cands.append(
            {
                "claim_type": "timeframe_only",
                "coin": coin,
                "relative_days": rel_days,
                "cannot_verify_reason": "No clear verifiable numeric claim detected from heuristics",
            }
        )
    return cands


def _validate_planner_claims(raw: dict) -> tuple[bool, str, dict]:
    if not isinstance(raw, dict):
        return False, "Planner output must be JSON object", {}
    claims = raw.get("claims")
    if not isinstance(claims, list) or not claims:
        return False, "Field 'claims' must be non-empty array", {}

    norm_claims = []
    for i, c in enumerate(claims, start=1):
        if not isinstance(c, dict):
            return False, f"claim[{i}] must be object", {}
        ctype = str(c.get("claim_type", "")).strip()
        if ctype not in ALLOWED_CLAIM_TYPES:
            return False, f"claim[{i}].claim_type invalid: {ctype}", {}
        if "cannot_verify_reason" not in c:
            return False, f"claim[{i}] missing required cannot_verify_reason", {}

        rel_days = c.get("relative_days", 7)
        try:
            rel_days = int(rel_days)
        except Exception:
            rel_days = 7
        rel_days = max(0, min(rel_days, 365))

        norm_claims.append(
            {
                "claim_id": str(c.get("claim_id") or f"c{i}"),
                "claim_type": ctype,
                "coin": (str(c.get("coin", "")).strip().lower() or None),
                "comparison_coin": (str(c.get("comparison_coin", "")).strip().lower() or None),
                "relative_days": rel_days,
                "target_price_usd": c.get("target_price_usd"),
                "direction": c.get("direction") if c.get("direction") in ("up", "down", None) else None,
                "expected_outperformance": bool(c.get("expected_outperformance", False)),
                "max_volatility_pct": c.get("max_volatility_pct"),
                "cannot_verify_reason": str(c.get("cannot_verify_reason") or ""),
                "rationale": str(c.get("rationale") or ""),
                "coincap_endpoints": c.get("coincap_endpoints") or [],
            }
        )

    out = {
        "planner_version": "v2",
        "claims": norm_claims,
    }
    return True, "", out


def plan_claims_llm(
    tweet_text: str,
    tweet_date: str,
    context_features: dict,
    nlp_api_key: str,
    nlp_model: str,
    nlp_base_url: str,
) -> tuple[dict, str, str]:
    candidates = build_candidate_claims(tweet_text, tweet_date, context_features)
    if not nlp_api_key or OpenAI is None:
        planned = _apply_horizon_policy(tweet_text, {"planner_version": "v2", "claims": candidates})
        return planned, "heuristic", ""

    client = OpenAI(api_key=nlp_api_key, base_url=nlp_base_url)
    schema = {
        "planner_version": "v2",
        "claims": [
            {
                "claim_id": "c1",
                "claim_type": "price_target|direction|relative_performance|market_cap|volatility_risk|timeframe_only",
                "coin": "lowercase symbol or slug or null",
                "comparison_coin": "lowercase symbol or slug or null",
                "relative_days": "int 0..365",
                "target_price_usd": "number or null",
                "direction": "up|down|null",
                "expected_outperformance": "bool",
                "max_volatility_pct": "number or null",
                "cannot_verify_reason": "string REQUIRED, empty if claim is verifiable",
                "rationale": "short string",
                "coincap_endpoints": ["list of endpoint paths to use"],
            }
        ],
    }

    system_prompt = (
        "You are a strict Planner for crypto tweet verification. "
        "Your job is ONLY to extract and normalize multiple verifiable claims and map them to CoinCap endpoints. "
        "Return JSON only."
    )

    base_user_prompt = (
        "Build Planner JSON for this tweet.\n"
        f"CoinCap capabilities:\n{coincap_capabilities_text()}\n\n"
        f"Tweet date: {tweet_date}\n"
        f"Tweet text:\n{tweet_text}\n\n"
        f"Candidate claims from heuristics:\n{json.dumps(candidates, ensure_ascii=False, indent=2)}\n\n"
        f"Context features:\n{json.dumps(context_features, ensure_ascii=False, indent=2)}\n\n"
        "Strict requirements:\n"
        "1) Return ONLY valid JSON matching this structure:\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2)}\n"
        "2) Extract 2-5 claims if present, otherwise 1 claim.\n"
        "3) cannot_verify_reason must be present for every claim (empty if verifiable).\n"
        "4) Use specific CoinCap endpoints in coincap_endpoints.\n"
        "5) Avoid hallucinations: if a claim is not truly verifiable, set cannot_verify_reason."
    )

    last_error = ""
    last_raw = ""
    for attempt in range(2):
        retry_hint = ""
        if attempt > 0:
            retry_hint = f"\n\nPost-validation error from previous output: {last_error}\nFix JSON and try again."
        resp = client.chat.completions.create(
            model=nlp_model,
            temperature=0,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": base_user_prompt + retry_hint},
            ],
        )
        raw = resp.choices[0].message.content or ""
        last_raw = raw
        try:
            parsed = _extract_json_object_from_text(raw)
        except Exception as e:  # noqa: BLE001
            last_error = f"invalid_json:{e}"
            continue
        ok, err, normalized = _validate_planner_claims(parsed)
        if ok:
            normalized = _apply_horizon_policy(tweet_text, normalized)
            return normalized, "llm", raw
        last_error = err

    fallback = _apply_horizon_policy(tweet_text, {"planner_version": "v2", "claims": candidates})
    return fallback, "heuristic_fallback", f"LLM_PLANNER_ERROR: {last_error}\nRAW:\n{last_raw}"
