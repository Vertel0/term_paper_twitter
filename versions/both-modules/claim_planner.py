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


def market_data_capabilities_text() -> str:
    return (
        "Binance verification toolkit for planning: "
        "Endpoints available to route checks: "
        "/api/v3/ping, /api/v3/time, /api/v3/exchangeInfo, /api/v3/trades, /api/v3/historicalTrades, "
        "/api/v3/aggTrades, /api/v3/depth, /api/v3/klines, /api/v3/ticker/price, /api/v3/avgPrice, "
        "/api/v3/ticker/bookTicker, /api/v3/ticker/24hr, /api/v3/ticker (rolling window), "
        "/sapi/v1/system/status, /api/v3/account, /sapi/v3/accountStatus, /sapi/v3/apiTradingStatus, "
        "/sapi/v1/asset/query/trading-fee, /sapi/v1/asset/query/trading-volume. "
        "Use /api/v3/exchangeInfo to resolve all tradable symbols dynamically (not a small fixed coin list). "
        "LLM should normalize coin names to valid Binance symbol format (e.g., BASEQUOTE such as BTCUSDT) and can cover any listed crypto. "
        "Prefer public endpoints for market evidence; signed endpoints should be marked as requires private account scope. "
        "For non-market news/fundamental claims, route to Perplexity and keep Binance scope boundaries explicit."
    )


def coincap_capabilities_text() -> str:
    # Backward-compatible alias used in other modules.
    return market_data_capabilities_text()


ALLOWED_CLAIM_TYPES = {
    "price_target",
    "direction",
    "relative_performance",
    "market_cap",
    "volatility_risk",
    "timeframe_only",
    "liquidation_amount",
}


def _is_null_token(v: object) -> bool:
    if v is None:
        return True
    s = str(v).strip().lower()
    return s in {"", "none", "null", "n/a", "na"}


def _norm_optional_str(v: object) -> Optional[str]:
    if _is_null_token(v):
        return None
    return str(v).strip().lower()


def _extract_money_values(text: str) -> List[float]:
    vals: List[float] = []
    for m in re.finditer(r"\$\s*(\d{1,3}(?:[\s,]\d{3})*(?:\.\d+)?)\s*([kKmMbB]?)", text or ""):
        try:
            v = float(m.group(1).replace(" ", "").replace(",", ""))
            sfx = (m.group(2) or "").lower()
            if sfx == "k":
                v *= 1_000
            elif sfx == "m":
                v *= 1_000_000
            elif sfx == "b":
                v *= 1_000_000_000
            vals.append(v)
        except Exception:
            continue
    return vals


def _force_liquidation_claim_if_needed(tweet_text: str, claims: List[dict], default_coin: Optional[str]) -> List[dict]:
    low = (tweet_text or "").lower()
    if not any(k in low for k in ["liquidat", "ликвид", "forced", "liquidation"]):
        return claims

    has_liq = any(str(c.get("claim_type") or "") == "liquidation_amount" for c in claims)
    if has_liq:
        fixed: List[dict] = []
        for c in claims:
            cc = dict(c)
            if str(cc.get("claim_type") or "") == "liquidation_amount" and _is_null_token(cc.get("coin")) and default_coin:
                cc["coin"] = default_coin
            fixed.append(cc)
        return fixed

    money_values = _extract_money_values(tweet_text)
    amount = max(money_values) if money_values else None
    if amount is not None and amount < 100_000:
        # Likely just a price level, not liquidation magnitude
        amount = None

    lookback_minutes = 15 if ("15 minute" in low or "15 min" in low or "15m" in low) else 60
    forced = {
        "claim_id": f"c{len(claims) + 1}",
        "claim_type": "liquidation_amount",
        "coin": default_coin,
        "comparison_coin": None,
        "relative_days": 0,
        "target_price_usd": None,
        "amount_usd": amount,
        "lookback_minutes": lookback_minutes,
        "direction": None,
        "expected_outperformance": False,
        "max_volatility_pct": None,
        "cannot_verify_reason": "",
        "rationale": "Tweet explicitly mentions liquidation amount",
        "data_endpoints": ["/api/v3/klines"],
    }
    return [*claims, forced]


def _has_explicit_long_horizon(tweet_text: str) -> bool:
    low = (tweet_text or "").lower()
    patterns = [
        r"\b30\s*d(ay|ays)?\b",
        r"\bmonth\b|\bmonthly\b",
        r"\bчерез\s+месяц\b|\bв\s+течение\s+месяца\b",
        r"\b2\s*weeks?\b|\b14\s*d(ay|ays)?\b",
    ]
    return any(re.search(p, low) for p in patterns)


def _has_immediate_context(tweet_text: str) -> bool:
    low = (tweet_text or "").lower()
    patterns = [
        r"\bjust in\b",
        r"\bright now\b",
        r"\bnow\b",
        r"\btoday\b",
        r"\bpast\s+\d+\s*(minute|min|m)\b",
        r"\bв\s+течение\s+\d+\s+мин",
        r"\bза\s+последн(ие|их)\s+\d+\s+мин",
    ]
    return any(re.search(p, low) for p in patterns)


def _has_explicit_future_context(tweet_text: str) -> bool:
    low = (tweet_text or "").lower()
    patterns = [
        r"\bnext\s+week\b",
        r"\bin\s+\d+\s+day",
        r"\bin\s+\d+\s+week",
        r"\bwithin\s+\d+\s+day",
        r"\bчерез\s+\d+\s+д",
        r"\bчерез\s+недел",
        r"\bк\s+концу\s+недел",
    ]
    return any(re.search(p, low) for p in patterns)


def _has_historical_context(tweet_text: str) -> bool:
    low = (tweet_text or "").lower()
    patterns = [
        r"\b\d+\s+years?\s+ago\b",
        r"\b\d+\s+months?\s+ago\b",
        r"\b\d+\s+days?\s+ago\b",
        r"\bon this day\b",
        r"\bfor the first time\b",
        r"\bfun fact\b",
        r"\bисторич",
        r"\bвпервые\b",
        r"\bлет\s+назад\b",
    ]
    return any(re.search(p, low) for p in patterns)


def _extract_historical_offset_days(tweet_text: str) -> Optional[int]:
    low = (tweet_text or "").lower()
    m_years = re.search(r"\b(\d{1,3})\s+years?\s+ago\b", low)
    if m_years:
        return int(m_years.group(1)) * 365
    m_months = re.search(r"\b(\d{1,3})\s+months?\s+ago\b", low)
    if m_months:
        return int(m_months.group(1)) * 30
    m_days = re.search(r"\b(\d{1,4})\s+days?\s+ago\b", low)
    if m_days:
        return int(m_days.group(1))
    return None


def _apply_horizon_policy(tweet_text: str, planned: dict) -> dict:
    claims = (planned or {}).get("claims") or []
    explicit_long = _has_explicit_long_horizon(tweet_text)
    immediate_ctx = _has_immediate_context(tweet_text)
    future_ctx = _has_explicit_future_context(tweet_text)
    historical_ctx = _has_historical_context(tweet_text)
    hist_offset_days = _extract_historical_offset_days(tweet_text)
    out = []
    for c in claims:
        cc = dict(c)
        rel = int(cc.get("relative_days", 7) or 7)
        rel = max(0, min(rel, 365))

        if immediate_ctx and not future_ctx:
            rel = 0
        if historical_ctx and not future_ctx:
            rel = 0
            cc["cannot_verify_reason"] = "Historical statement, not a forward prediction"
            cc["is_historical_fact"] = True
            if hist_offset_days is not None:
                cc["historical_offset_days"] = hist_offset_days

        if not explicit_long and rel > 14:
            rel = 14
            if not cc.get("rationale"):
                cc["rationale"] = "Horizon normalized to 14 days by policy"

        if rel == 0 and str(cc.get("claim_type") or "") == "direction" and immediate_ctx and not future_ctx:
            # Avoid forcing direction checks against same-day anchor when tweet is about immediate event.
            continue

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
    if not coin:
        low = (tweet_text or "").lower()
        if "bitcoin" in low or re.search(r"\bbtc\b", low):
            coin = "btc"
        elif "ethereum" in low or re.search(r"\beth\b", low):
            coin = "eth"
        elif "solana" in low or re.search(r"\bsol\b", low):
            coin = "sol"
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

    if any(k in text_low for k in ["liquidat", "ликвид", "forced", "liquidation"]):
        all_money = _extract_money_values(tweet_text)
        amt = max(all_money) if all_money else None
        if amt is not None and amt < 100_000:
            amt = None
        lookback_minutes = 15 if ("15 minute" in text_low or "15 min" in text_low or "15m" in text_low) else 60
        cands.append(
            {
                "claim_type": "liquidation_amount",
                "coin": coin,
                "relative_days": 0,
                "lookback_minutes": lookback_minutes,
                "amount_usd": amt,
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
        preferred_source = str(c.get("preferred_source") or "").strip().lower()
        if preferred_source not in {"binance", "perplexity", "both", "none"}:
            preferred_source = "binance"

        norm_claims.append(
            {
                "claim_id": str(c.get("claim_id") or f"c{i}"),
                "claim_type": ctype,
                "coin": _norm_optional_str(c.get("coin")),
                "comparison_coin": _norm_optional_str(c.get("comparison_coin")),
                "relative_days": rel_days,
                "target_price_usd": c.get("target_price_usd"),
                "amount_usd": c.get("amount_usd"),
                "lookback_minutes": c.get("lookback_minutes"),
                "historical_offset_days": c.get("historical_offset_days"),
                "is_historical_fact": bool(c.get("is_historical_fact", False)),
                "direction": c.get("direction") if c.get("direction") in ("up", "down", None) else None,
                "expected_outperformance": bool(c.get("expected_outperformance", False)),
                "max_volatility_pct": c.get("max_volatility_pct"),
                "author_expectation": str(c.get("author_expectation") or "").strip(),
                "preferred_source": preferred_source,
                "perplexity_query": str(c.get("perplexity_query") or "").strip(),
                "cannot_verify_reason": str(c.get("cannot_verify_reason") or ""),
                "rationale": str(c.get("rationale") or ""),
                "data_endpoints": c.get("data_endpoints") or c.get("coincap_endpoints") or [],
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
                "claim_type": "price_target|direction|relative_performance|market_cap|volatility_risk|timeframe_only|liquidation_amount",
                "coin": "lowercase symbol or slug or null",
                "comparison_coin": "lowercase symbol or slug or null",
                "relative_days": "int 0..365",
                "target_price_usd": "number or null",
                "amount_usd": "number or null",
                "lookback_minutes": "int or null",
                "direction": "up|down|null",
                "expected_outperformance": "bool",
                "max_volatility_pct": "number or null",
                "author_expectation": "short explicit expectation from tweet",
                "preferred_source": "binance|perplexity|both|none",
                "perplexity_query": "string or null",
                "cannot_verify_reason": "string REQUIRED, empty if claim is verifiable",
                "rationale": "short string",
                "data_endpoints": ["list of endpoint paths to use"],
            }
        ],
    }

    system_prompt = (
        "You are a strict Planner for crypto tweet verification. "
        "Your job is ONLY to extract and normalize multiple claims, fix the author's explicit expectation, "
        "and route each claim to Binance / Perplexity / both based on verifiability. "
        "IMPORTANT: if tweet is historical (e.g., 'X years ago', 'for first time', 'on this day'), "
        "do not convert it into a present/future prediction. Mark claim as historical with cannot_verify_reason and is_historical_fact=true. "
        "Return JSON only."
    )

    base_user_prompt = (
        "Build Planner JSON for this tweet.\n"
        f"Market data capabilities:\n{market_data_capabilities_text()}\n\n"
        f"Tweet date: {tweet_date}\n"
        f"Tweet text:\n{tweet_text}\n\n"
        f"Candidate claims from heuristics:\n{json.dumps(candidates, ensure_ascii=False, indent=2)}\n\n"
        f"Context features:\n{json.dumps(context_features, ensure_ascii=False, indent=2)}\n\n"
        "Strict requirements:\n"
        "1) Return ONLY valid JSON matching this structure:\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2)}\n"
        "2) Extract 2-5 claims if present, otherwise 1 claim.\n"
        "3) cannot_verify_reason must be present for every claim (empty if verifiable).\n"
        "4) Fill author_expectation explicitly for every claim (what author expects to happen / what fact author asserts).\n"
        "5) Set preferred_source: binance if OHLCV is enough; perplexity for external news/fundamental claims; both if mixed.\n"
        "6) If preferred_source includes perplexity, provide a concrete perplexity_query in English for web search.\n"
        "7) Use specific Binance data-api endpoints in data_endpoints.\n"
        "8) Avoid hallucinations: if a claim is not truly verifiable, set cannot_verify_reason.\n"
        "9) For historical statements ('years ago', 'for first time', 'on this day') set is_historical_fact=true and historical_offset_days where possible; do not validate against tweet_date market level."
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
            normalized["claims"] = _force_liquidation_claim_if_needed(
                tweet_text,
                normalized.get("claims") or [],
                (normalized.get("claims") or [{}])[0].get("coin") if (normalized.get("claims") or []) else None,
            )
            return normalized, "llm", raw
        last_error = err

    fallback = _apply_horizon_policy(tweet_text, {"planner_version": "v2", "claims": candidates})
    fallback["claims"] = _force_liquidation_claim_if_needed(
        tweet_text,
        fallback.get("claims") or [],
        (fallback.get("claims") or [{}])[0].get("coin") if (fallback.get("claims") or []) else None,
    )
    return fallback, "heuristic_fallback", f"LLM_PLANNER_ERROR: {last_error}\nRAW:\n{last_raw}"
