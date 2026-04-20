#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Function-calling verification agent (separate experimental module).

What it does:
1) Accepts tweet text/date/metrics.
2) Lets LLM autonomously choose tools (Binance + web search).
3) Returns transparent final JSON with claims, evidence, comparison and scoring.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from dateutil import parser as dt_parser

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

from binance_query import (
    call_binance_endpoint,
    fetch_price_on_date,
    fetch_price_series_in_window,
    get_all_trading_symbols,
    get_endpoint_catalog,
    resolve_symbol,
)
from account_analysis import build_account_stats_comment_llm
from account_analysis import build_account_summary_llm, extract_account_profile_from_timeline
from crypto_tweet_gate import classify_tweet_text
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

AITUNNEL_BASE_URL = os.getenv("AITUNNEL_BASE_URL", "https://api.aitunnel.ru/v1/")
AITUNNEL_API_KEY = os.getenv("AITUNNEL_API_KEY", "")
AGENT_MODEL = os.getenv("AGENT_FC_MODEL", "gpt-5.4-nano")
AGENT_FALLBACK_MODEL = os.getenv("AGENT_FC_FALLBACK_MODEL", os.getenv("AITUNNEL_MODEL", "deepseek-v3.2"))
EXTRACT_MODEL = os.getenv("AGENT_EXTRACT_MODEL", os.getenv("AITUNNEL_MODEL", "deepseek-v3.2"))
WEB_MODEL = os.getenv("AGENT_WEB_MODEL", "sonar")
TW_PROXIES = os.getenv("TW_PROXIES", "http://03hGHq:8dUFC8@95.164.202.193:9233")
FC_FORCE_FORMULA = os.getenv("FC_FORCE_FORMULA", "0") == "1"


def _extract_json_object(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    try:
        return json.loads(raw)
    except Exception:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end < 0 or end <= start:
            return {}
        try:
            return json.loads(raw[start : end + 1])
        except Exception:
            return {}


def _safe_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _is_provider_400_error(exc: Exception) -> bool:
    s = str(exc or "")
    return ("Error code: 400" in s) or ("Provider returned error" in s)


def _compact_price_points(points: List[Dict[str, Any]], max_points: int = 24) -> List[Dict[str, Any]]:
    if not isinstance(points, list) or not points:
        return []
    if len(points) <= max_points:
        return points
    step = max(1, len(points) // max_points)
    sampled = [points[i] for i in range(0, len(points), step)]
    if sampled[-1] is not points[-1]:
        sampled.append(points[-1])
    return sampled[:max_points]


def _compact_for_llm(obj: Any, max_symbols: int = 80) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "symbols" and isinstance(v, list):
                out[k] = v[:max_symbols]
                out["symbols_truncated"] = max(0, len(v) - max_symbols)
            elif k == "data" and isinstance(v, list):
                out[k] = _compact_price_points(v, max_points=24)
                out["data_points_total"] = len(v)
            else:
                out[k] = _compact_for_llm(v, max_symbols=max_symbols)
        return out
    if isinstance(obj, list):
        if len(obj) > 60:
            return [_compact_for_llm(x, max_symbols=max_symbols) for x in obj[:60]] + [{"truncated": len(obj) - 60}]
        return [_compact_for_llm(x, max_symbols=max_symbols) for x in obj]
    return obj


def _as_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _extract_precheck_context_llm(
    *,
    api_key: str,
    base_url: str,
    tweet_text: str,
    tweet_date: str,
    tweet_metrics: Optional[Dict[str, Any]] = None,
    model: str = EXTRACT_MODEL,
) -> Dict[str, Any]:
    if OpenAI is None or not api_key:
        return {}

    schema = {
        "mentioned_assets": ["BTC", "ETH"],
        "primary_symbol_hint": "BTCUSDT",
        "primary_target_price_usd": None,
        "time_horizon_hint_days": None,
        "has_price_target": False,
        "has_directional_claim": False,
        "has_news_claim": False,
        "is_realtime_claim": False,
        "needs_minute_level_price": False,
        "requires_web_validation": False,
        "confidence_0_100": 0,
    }

    prompt = (
        "Extract concise machine-readable trading-claim context from tweet. Return JSON only.\n"
        "Do not verify facts. Only extract values/flags useful for downstream tool-planning.\n"
        f"Required schema:\n{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
        f"tweet_date: {tweet_date}\n"
        f"tweet_text: {tweet_text}\n"
        f"tweet_metrics: {_safe_json(tweet_metrics or {})}\n"
    )

    client = OpenAI(api_key=api_key, base_url=base_url)
    models = [model]
    if AGENT_FALLBACK_MODEL and AGENT_FALLBACK_MODEL not in models:
        models.append(AGENT_FALLBACK_MODEL)
    last_exc: Optional[Exception] = None
    for mdl in models:
        for attempt in range(2):
            try:
                resp = client.chat.completions.create(
                    model=mdl,
                    temperature=0,
                    messages=[
                        {"role": "system", "content": "You extract structured fields. Output JSON only."},
                        {"role": "user", "content": prompt},
                    ],
                )
                raw = (resp.choices[0].message.content or "").strip()
                parsed = _extract_json_object(raw)
                if not isinstance(parsed, dict):
                    return {}
                out = dict(parsed)
                out["mentioned_assets"] = [str(x).upper() for x in (out.get("mentioned_assets") or []) if str(x).strip()][:10]
                out["primary_symbol_hint"] = str(out.get("primary_symbol_hint") or "").upper().strip()
                out["primary_target_price_usd"] = _as_float(out.get("primary_target_price_usd"))
                th = out.get("time_horizon_hint_days")
                out["time_horizon_hint_days"] = int(th) if isinstance(th, (int, float)) else None
                for k in (
                    "has_price_target",
                    "has_directional_claim",
                    "has_news_claim",
                    "is_realtime_claim",
                    "needs_minute_level_price",
                    "requires_web_validation",
                ):
                    out[k] = bool(out.get(k, False))
                conf = out.get("confidence_0_100")
                try:
                    out["confidence_0_100"] = max(0, min(int(conf), 100))
                except Exception:
                    out["confidence_0_100"] = 0
                out["model"] = mdl
                return out
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                time.sleep(0.6 * (attempt + 1))
                continue
    return {"error": str(last_exc)} if last_exc else {}


def _price_deviation_score(deviation_pct: float) -> int:
    d = max(0.0, float(deviation_pct))
    if d <= 0.05:
        return 100
    if d <= 0.10:
        return 98
    if d <= 0.25:
        return 95
    if d <= 0.50:
        return 90
    if d <= 1.00:
        return 84
    if d <= 2.00:
        return 72
    if d <= 3.00:
        return 60
    if d <= 5.00:
        return 42
    return 20


def _price_deviation_status(deviation_pct: float) -> str:
    d = max(0.0, float(deviation_pct))
    if d <= 1.0:
        return "true"
    if d <= 3.0:
        return "unknown"
    return "false"


def _collect_price_observation(agent_steps: List[Dict[str, Any]]) -> Dict[str, Any]:
    near_steps = [s for s in (agent_steps or []) if str(s.get("tool") or "") == "binance_get_price_near_datetime"]
    for s in reversed(near_steps):
        r = s.get("result") if isinstance(s.get("result"), dict) else {}
        sm = r.get("summary") if isinstance(r.get("summary"), dict) else {}
        nc = sm.get("nearest_close")
        if isinstance(nc, (int, float)):
            return {
                "source": "binance_get_price_near_datetime",
                "observed_price_usd": float(nc),
                "observed_at": sm.get("nearest_date"),
                "window_high": sm.get("window_high"),
                "window_low": sm.get("window_low"),
                "symbol": r.get("symbol"),
            }

    date_steps = [s for s in (agent_steps or []) if str(s.get("tool") or "") == "binance_get_price_on_date"]
    for s in reversed(date_steps):
        r = s.get("result") if isinstance(s.get("result"), dict) else {}
        rr = r.get("result") if isinstance(r.get("result"), dict) else {}
        p = rr.get("price")
        if isinstance(p, (int, float)):
            raw = rr.get("raw") if isinstance(rr.get("raw"), dict) else {}
            chosen = raw.get("chosen") if isinstance(raw, dict) else None
            chosen_date = None
            if isinstance(chosen, list) and len(chosen) >= 1:
                try:
                    chosen_date = datetime.fromtimestamp(int(chosen[0]) / 1000, tz=timezone.utc).isoformat()
                except Exception:
                    chosen_date = None
            return {
                "source": "binance_get_price_on_date",
                "observed_price_usd": float(p),
                "observed_at": chosen_date,
                "window_high": None,
                "window_low": None,
                "symbol": r.get("symbol"),
            }

    return {"source": "none", "observed_price_usd": None}


def _reconcile_comparison_with_formula(
    claims: List[Dict[str, Any]],
    comparison: List[Dict[str, Any]],
    agent_steps: List[Dict[str, Any]],
    extracted_context: Optional[Dict[str, Any]] = None,
    tweet_text: str = "",
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    obs = _collect_price_observation(agent_steps)
    observed_price = obs.get("observed_price_usd")
    adjusted: List[Dict[str, Any]] = []
    score_list: List[int] = []
    formula_rows: List[Dict[str, Any]] = []

    for i, c in enumerate(comparison or []):
        row = dict(c)
        claim_text = str(row.get("claim") or "")
        claim_meta = claims[i] if i < len(claims) and isinstance(claims[i], dict) else {}
        target = _as_float(claim_meta.get("target_price_usd"))
        if target is None:
            target = _as_float(row.get("target_price_usd"))
        if target is None and i == 0 and isinstance(extracted_context, dict):
            target = _as_float(extracted_context.get("primary_target_price_usd"))

        base_status = str(row.get("status") or "unknown").lower().strip()
        if base_status == "mixed":
            base_status = "unknown"
        if base_status not in {"true", "false", "unknown"}:
            base_status = "unknown"

        if isinstance(target, (int, float)) and isinstance(observed_price, (int, float)) and target > 0:
            nearest_dev_pct = abs(float(observed_price) - float(target)) / float(target) * 100.0
            hi = obs.get("window_high")
            lo = obs.get("window_low")
            hit_in_window = False
            if isinstance(hi, (int, float)) and isinstance(lo, (int, float)):
                lo_f = min(float(lo), float(hi))
                hi_f = max(float(lo), float(hi))
                hit_in_window = lo_f <= float(target) <= hi_f

            # If target level was reached inside minute window, do not penalize for exact tick mismatch.
            effective_dev_pct = 0.0 if hit_in_window else nearest_dev_pct
            sc = _price_deviation_score(effective_dev_pct)
            if hit_in_window:
                sc = max(sc, 95)
            st = _price_deviation_status(effective_dev_pct)
            row["status"] = st
            row["confidence_0_100"] = sc
            row["deviation_pct"] = round(effective_dev_pct, 6)
            row["nearest_deviation_pct"] = round(nearest_dev_pct, 6)
            row["hit_in_window"] = hit_in_window
            row["target_price_usd"] = float(target)
            row["observed_price_usd"] = float(observed_price)
            row["observed_at"] = obs.get("observed_at")
            row["reason"] = (
                f"Formula-based: target=${float(target):,.2f}, observed=${float(observed_price):,.2f}, "
                f"nearest_deviation={nearest_dev_pct:.6f}%, hit_in_window={hit_in_window}."
            )
            score_list.append(sc)
            formula_rows.append(
                {
                    "claim": claim_text,
                    "mode": "price_deviation",
                    "target_price_usd": float(target),
                    "observed_price_usd": float(observed_price),
                    "deviation_pct": round(effective_dev_pct, 6),
                    "nearest_deviation_pct": round(nearest_dev_pct, 6),
                    "hit_in_window": hit_in_window,
                    "score_0_100": sc,
                    "status": st,
                    "source": obs.get("source"),
                }
            )
        else:
            map_score = {"true": 90, "unknown": 55, "false": 20}
            ctype = str(row.get("claim_type") or claim_meta.get("claim_type") or "").lower().strip()
            reason_low = str(row.get("reason") or "").lower()
            status_eff = base_status
            if ctype == "news" and base_status == "false" and ("web" in reason_low or "source" in reason_low or "news" in reason_low):
                # Missing web confirmation should not hard-fail a price claim in the same tweet.
                status_eff = "unknown"
            sc = map_score.get(status_eff, 55)
            row["status"] = status_eff
            row["confidence_0_100"] = sc
            score_list.append(sc)
            formula_rows.append(
                {
                    "claim": claim_text,
                    "mode": "status_mapping",
                    "score_0_100": sc,
                    "status": status_eff,
                }
            )

        adjusted.append(row)

    avg_score = round(sum(score_list) / len(score_list), 2) if score_list else 0.0
    formula_meta = {
        "version": "fc_formula_v1",
        "rules": {
            "price_deviation_status": "<=1% => true, 1..3% => unknown, >3% => false (effective deviation)",
            "minute_window_hit_override": "if target is inside [window_low, window_high], effective deviation=0 and score>=95",
            "price_deviation_score": "piecewise: <=0.05%=100, <=0.1%=98, <=0.25%=95, <=0.5%=90, <=1%=84, <=2%=72, <=3%=60, <=5%=42, else 20",
            "fallback_status_score": {"true": 90, "unknown": 55, "false": 20},
        },
        "observation_used": obs,
        "extracted_context_used": extracted_context if isinstance(extracted_context, dict) else {},
        "claim_rows": formula_rows,
        "avg_claim_score": avg_score,
    }
    return adjusted, formula_meta


def _calc_score_from_comparison(comparison: List[Dict[str, Any]], score_hint: Dict[str, Any]) -> Dict[str, Any]:
    counts = {"true": 0, "false": 0, "unknown": 0, "total": 0}
    checks: List[Dict[str, Any]] = []
    per_claim_scores: List[float] = []
    for c in comparison or []:
        st = str(c.get("status") or "unknown").lower().strip()
        if st == "mixed":
            st = "unknown"
        if st not in {"true", "false", "unknown"}:
            st = "unknown"
        counts[st] += 1
        counts["total"] += 1
        cscore = c.get("confidence_0_100")
        if isinstance(cscore, (int, float)):
            per_claim_scores.append(float(cscore))
        checks.append(
            {
                "claim": c.get("claim"),
                "status": st,
                "reason": str(c.get("reason") or "").strip(),
                "confidence_0_100": cscore if isinstance(cscore, (int, float)) else None,
            }
        )

    decidable = counts["true"] + counts["false"]
    truth_ratio = (counts["true"] / max(decidable, 1)) if decidable > 0 else 0.0
    weighted_true = counts["true"] + 0.5 * counts["unknown"]
    weighted_false = counts["false"]
    weighted_unknown = counts["unknown"]

    conf = score_hint.get("confidence_0_100")
    if per_claim_scores:
        conf_val = int(round(sum(per_claim_scores) / len(per_claim_scores)))
    else:
        try:
            conf_val = int(conf)
        except Exception:
            conf_val = int(round(min(100.0, max(0.0, truth_ratio * 100))))
    conf_val = max(0, min(conf_val, 100))

    status = str(score_hint.get("status") or "").lower().strip()
    if status not in {"true", "false", "mixed", "unknown"}:
        if counts["true"] > counts["false"]:
            status = "true"
        elif counts["false"] > counts["true"]:
            status = "false"
        else:
            status = "unknown"

    return {
        "checks": checks,
        "counts": counts,
        "score": {
            "confidence_0_100": conf_val,
            "truth_ratio": round(float(truth_ratio), 4),
            "weighted_true": round(float(weighted_true), 4),
            "weighted_false": round(float(weighted_false), 4),
            "weighted_unknown": round(float(weighted_unknown), 4),
            "status": status,
        },
        "status": status,
    }


def _build_news_verification_from_logs(agent_steps: List[Dict[str, Any]], comparison: List[Dict[str, Any]]) -> Dict[str, Any]:
    web_steps = [s for s in (agent_steps or []) if str(s.get("tool") or "") == "search_web"]
    if not web_steps:
        return {"used": False, "status": "unknown", "confidence_0_100": 0, "summary": "", "sources": []}

    last = web_steps[-1]
    res = (last.get("result") or {}) if isinstance(last.get("result"), dict) else {}
    body = (res.get("result") or {}) if isinstance(res.get("result"), dict) else {}
    conf = body.get("confidence_0_100", 0)
    try:
        conf = int(conf)
    except Exception:
        conf = 0
    conf = max(0, min(conf, 100))

    st = "unknown"
    for c in comparison or []:
        if str(c.get("claim_type") or "").lower() == "news":
            st = str(c.get("status") or "unknown").lower()
            if st == "mixed":
                st = "unknown"
            break

    return {
        "used": True,
        "source": "fc_search_web",
        "status": st,
        "confidence_0_100": conf,
        "summary": str(body.get("summary") or "").strip(),
        "sources": body.get("sources") if isinstance(body.get("sources"), list) else [],
        "raw": res,
    }


def _append_runs_table(csv_path: str, tweet_info: Dict[str, Any], payload: Dict[str, Any], merged: Dict[str, Any]) -> None:
    if not csv_path:
        return
    headers = ["run_utc", "tweet_id", "tweet_date", "tweet_text", "request_json", "result_json"]
    write_header = (not os.path.exists(csv_path)) or os.path.getsize(csv_path) == 0
    row = {
        "run_utc": datetime.now(timezone.utc).isoformat(),
        "tweet_id": tweet_info.get("tweet_id", ""),
        "tweet_date": tweet_info.get("tweet_date", ""),
        "tweet_text": tweet_info.get("tweet_text", ""),
        "request_json": _safe_json(payload),
        "result_json": _safe_json(merged),
    }
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=headers)
        if write_header:
            wr.writeheader()
        wr.writerow(row)


_ENDPOINT_KEY_ALIASES: Dict[str, str] = {
    "exchangeinfo": "exchange_info",
    "exchange-info": "exchange_info",
    "recent_trades": "trades_recent",
    "recenttrades": "trades_recent",
    "historical_trades": "trades_historical",
    "historicaltrades": "trades_historical",
    "aggtrades": "agg_trades",
    "depth": "order_book_depth",
    "orderbook": "order_book_depth",
    "order_book": "order_book_depth",
    "price": "ticker_price",
    "tickerprice": "ticker_price",
    "ticker24h": "ticker_24hr",
    "24hr": "ticker_24hr",
    "bookticker": "book_ticker",
    "windowticker": "ticker_window",
}


def _canonical_endpoint_key(raw_key: str) -> tuple[Optional[str], List[str]]:
    key = str(raw_key or "").strip()
    catalog = get_endpoint_catalog(include_signed=True)
    known = set(catalog.keys())
    if key in known:
        return key, []

    norm = key.lower().strip().replace(" ", "_").replace("-", "_").replace("/", "_")
    if norm in known:
        return norm, []

    norm_compact = norm.replace("_", "")
    if norm_compact in _ENDPOINT_KEY_ALIASES:
        cand = _ENDPOINT_KEY_ALIASES[norm_compact]
        if cand in known:
            return cand, []
    if norm in _ENDPOINT_KEY_ALIASES:
        cand = _ENDPOINT_KEY_ALIASES[norm]
        if cand in known:
            return cand, []

    suggestions = [k for k in sorted(known) if norm in k or k in norm][:8]
    if not suggestions and norm_compact:
        suggestions = [k for k in sorted(known) if norm_compact in k.replace("_", "")][:8]
    return None, suggestions


def _resolve_symbol_safe(symbol_or_coin: str) -> Dict[str, Any]:
    raw = str(symbol_or_coin or "").strip()
    if not raw:
        return {
            "success": False,
            "error": "symbol_empty",
            "requested": symbol_or_coin,
            "symbol": None,
            "tried": [],
            "suggestions": [],
        }

    candidates: List[str] = []

    def push(v: str) -> None:
        vv = (v or "").strip()
        if vv and vv not in candidates:
            candidates.append(vv)

    push(raw)
    cleaned = re.sub(r"[^A-Za-z0-9/_\-]", "", raw)
    push(cleaned)
    push(cleaned.replace("$", ""))
    compact = cleaned.replace("/", "").replace("-", "").replace("_", "")
    push(compact)
    if compact.upper().endswith(("USDT", "USDC", "BUSD", "USD", "BTC", "ETH", "BNB")):
        for q in ("USDT", "USDC", "BUSD", "USD", "BTC", "ETH", "BNB"):
            if compact.upper().endswith(q):
                push(compact[: -len(q)])
                break

    for c in candidates:
        rs = resolve_symbol(c)
        if rs:
            return {
                "success": True,
                "requested": symbol_or_coin,
                "symbol": rs,
                "resolved_from": c,
                "tried": candidates,
                "suggestions": [],
            }

    syms = get_all_trading_symbols(force_refresh=False)
    token = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    base = token
    for q in ("USDT", "USDC", "BUSD", "USD", "BTC", "ETH", "BNB"):
        if token.endswith(q) and len(token) > len(q):
            base = token[: -len(q)]
            break
    suggestions = [s for s in syms if s.startswith(base)][:8] if base else []
    return {
        "success": False,
        "error": "symbol_not_resolved",
        "requested": symbol_or_coin,
        "symbol": None,
        "tried": candidates,
        "suggestions": suggestions,
    }


def _normalize_symbols_param(params: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(params or {})
    if out.get("symbol"):
        r = _resolve_symbol_safe(str(out.get("symbol")))
        if r.get("success"):
            out["symbol"] = r.get("symbol")
    if out.get("symbols") and isinstance(out.get("symbols"), list):
        norm_list = []
        for item in out.get("symbols") or []:
            r = _resolve_symbol_safe(str(item))
            norm_list.append(r.get("symbol") if r.get("success") else str(item))
        out["symbols"] = norm_list
    return out


def search_web(query: str, api_key: str, base_url: str, model: str = WEB_MODEL) -> Dict[str, Any]:
    if OpenAI is None:
        return {"success": False, "error": "openai_not_installed", "query": query}
    if not api_key:
        return {"success": False, "error": "missing_api_key", "query": query}

    client = OpenAI(api_key=api_key, base_url=base_url)
    schema = {
        "summary": "short summary",
        "facts": ["fact 1", "fact 2"],
        "sources": [{"title": "...", "url": "https://...", "snippet": "..."}],
        "confidence_0_100": 0,
    }
    prompt = (
        "Проведи web-search по запросу и верни только JSON по схеме:\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
        f"Запрос: {query}\n"
        "Не выдумывай источники. Указывай только реальные URL."
    )
    models = [model]
    if AGENT_FALLBACK_MODEL and AGENT_FALLBACK_MODEL not in models:
        models.append(AGENT_FALLBACK_MODEL)
    last_exc: Optional[Exception] = None
    for mdl in models:
        for attempt in range(3):
            try:
                resp = client.chat.completions.create(
                    model=mdl,
                    temperature=0,
                    messages=[
                        {"role": "system", "content": "Ты web-research модуль. Ответ только JSON."},
                        {"role": "user", "content": prompt},
                    ],
                )
                raw = (resp.choices[0].message.content or "").strip()
                parsed = _extract_json_object(raw)
                return {
                    "success": True,
                    "query": query,
                    "model": mdl,
                    "result": parsed,
                    "raw": raw,
                }
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if _is_provider_400_error(exc) or attempt < 2:
                    time.sleep(0.7 * (attempt + 1))
                    continue
                break
    return {"success": False, "error": str(last_exc), "query": query}


def _tool_specs() -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": "list_binance_symbols",
                "description": "Get current tradable Binance symbols from exchangeInfo cache/source (returns compact sample + count).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "force_refresh": {
                            "type": "boolean",
                            "description": "Force refresh exchangeInfo cache",
                        }
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "binance_call_public_endpoint",
                "description": "Call Binance public endpoint by key (ping/time/exchange_info/trades/agg_trades/depth/klines/tickers). Use symbol-scoped queries to avoid huge payloads.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "endpoint_key": {"type": "string", "description": "Endpoint key from catalog"},
                        "params": {"type": "object", "description": "Query params"},
                    },
                    "required": ["endpoint_key"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "binance_get_price_on_date",
                "description": "Get closest close price on date for any coin/symbol via Binance klines.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "coin_or_symbol": {"type": "string"},
                        "date": {"type": "string", "description": "YYYY-MM-DD"},
                        "window_days": {"type": "integer", "minimum": 0, "maximum": 30},
                    },
                    "required": ["coin_or_symbol", "date"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "binance_get_price_window",
                "description": "Get OHLCV-based price points for a date window to verify target/direction claims.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "coin_or_symbol": {"type": "string"},
                        "tweet_date": {"type": "string", "description": "YYYY-MM-DD"},
                        "relative_days": {"type": "integer", "minimum": 0, "maximum": 365},
                        "interval_hint": {"type": "string", "description": "1m/5m/15m/30m/1h/4h/1d"},
                    },
                    "required": ["coin_or_symbol", "tweet_date", "relative_days"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "binance_get_price_near_datetime",
                "description": "Get minute-level Binance candles around exact tweet datetime (critical for 'JUST IN / right now' claims).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "coin_or_symbol": {"type": "string"},
                        "datetime_utc": {"type": "string", "description": "ISO datetime in UTC"},
                        "minutes_before": {"type": "integer", "minimum": 0, "maximum": 180},
                        "minutes_after": {"type": "integer", "minimum": 0, "maximum": 180},
                    },
                    "required": ["coin_or_symbol", "datetime_utc"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_web",
                "description": "Search web for factual/news validation and return source-backed evidence.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_binance_endpoint_catalog",
                "description": "Get available Binance endpoints metadata for planning.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "include_signed": {"type": "boolean"},
                    },
                    "required": [],
                },
            },
        },
    ]


class FunctionCallingVerifier:
    def __init__(
        self,
        api_key: str,
        base_url: str = AITUNNEL_BASE_URL,
        model: str = AGENT_MODEL,
        web_model: str = WEB_MODEL,
        max_steps: int = 8,
    ) -> None:
        if OpenAI is None:
            raise RuntimeError("openai package is not installed")
        if not api_key:
            raise RuntimeError("AITUNNEL_API_KEY is required")

        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.fallback_model = AGENT_FALLBACK_MODEL
        self.web_model = web_model
        self.max_steps = max(1, int(max_steps))

    def _chat_create(self, *, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None, tool_choice: Optional[str] = None):
        last_exc: Optional[Exception] = None
        models = [self.model]
        if self.fallback_model and self.fallback_model != self.model:
            models.append(self.fallback_model)
        for mdl in models:
            for attempt in range(3):
                try:
                    kwargs: Dict[str, Any] = {
                        "model": mdl,
                        "temperature": 0,
                        "messages": messages,
                    }
                    if tools is not None:
                        kwargs["tools"] = tools
                    if tool_choice is not None:
                        kwargs["tool_choice"] = tool_choice
                    return self.client.chat.completions.create(**kwargs)
                except Exception as exc:  # noqa: BLE001
                    last_exc = exc
                    # Retry known transient/provider issues.
                    if _is_provider_400_error(exc) or attempt < 2:
                        time.sleep(0.8 * (attempt + 1))
                        continue
                    break
        raise RuntimeError(f"chat_completion_failed: {last_exc}")

    def _dispatch_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        try:
            if name == "list_binance_symbols":
                symbols = get_all_trading_symbols(force_refresh=bool(args.get("force_refresh", False)))
                return {"success": True, "count": len(symbols), "symbols": symbols[:80], "symbols_truncated": max(0, len(symbols) - 80)}

            if name == "get_binance_endpoint_catalog":
                include_signed = bool(args.get("include_signed", True))
                catalog = get_endpoint_catalog(include_signed=include_signed)
                compact = {k: {"path": v.get("path"), "required_params": v.get("required_params", []), "signed": bool(v.get("signed", False))} for k, v in catalog.items()}
                return {"success": True, "catalog": compact, "count": len(compact)}

            if name == "binance_call_public_endpoint":
                endpoint_key_raw = str(args.get("endpoint_key") or "").strip()
                endpoint_key, suggestions = _canonical_endpoint_key(endpoint_key_raw)
                if not endpoint_key:
                    return {
                        "success": False,
                        "error": "unknown_endpoint_key",
                        "endpoint_key": endpoint_key_raw,
                        "suggestions": suggestions,
                        "hint": "Use get_binance_endpoint_catalog to choose a valid endpoint key",
                    }

                params = args.get("params") if isinstance(args.get("params"), dict) else {}
                params = _normalize_symbols_param(params)
                heavy = {"ticker_price", "ticker_24hr", "book_ticker", "ticker_window", "trades_recent", "trades_historical", "agg_trades", "order_book_depth", "klines"}
                if endpoint_key in heavy and not (params.get("symbol") or params.get("symbols")):
                    return {
                        "success": False,
                        "error": "symbol_required_for_cost_control",
                        "endpoint_key": endpoint_key,
                        "hint": "Pass symbol or symbols to reduce payload",
                    }
                res = call_binance_endpoint(endpoint_key=endpoint_key, params=params)
                out = _compact_for_llm(res)
                if not bool(out.get("success")) and str(out.get("error") or "").startswith("unknown_endpoint_key"):
                    return {
                        "success": False,
                        "error": "unknown_endpoint_key",
                        "endpoint_key": endpoint_key_raw,
                        "suggestions": suggestions,
                    }
                return out

            if name == "binance_get_price_on_date":
                coin_or_symbol = str(args.get("coin_or_symbol") or "").strip()
                date = str(args.get("date") or "").strip()
                window_days = int(args.get("window_days", 1) or 1)
                resolved = _resolve_symbol_safe(coin_or_symbol)
                if not resolved.get("success"):
                    return {
                        "success": False,
                        "error": "symbol_not_resolved",
                        "coin_or_symbol": coin_or_symbol,
                        "suggestions": resolved.get("suggestions", []),
                        "tried": resolved.get("tried", []),
                    }
                symbol = str(resolved.get("symbol"))
                try:
                    dt = datetime.fromisoformat(date)
                except Exception:
                    return {"success": False, "error": "invalid_date", "date": date, "expected": "YYYY-MM-DD"}
                res = fetch_price_on_date(symbol, dt, window_days=window_days)
                compact = {
                    "price": res.get("price"),
                    "status_code": res.get("status_code"),
                    "raw": {
                        "chosen": ((res.get("raw") or {}).get("chosen") if isinstance(res.get("raw"), dict) else None),
                        "interval": ((res.get("raw") or {}).get("interval") if isinstance(res.get("raw"), dict) else None),
                        "symbol": ((res.get("raw") or {}).get("symbol") if isinstance(res.get("raw"), dict) else symbol),
                        "points": _compact_price_points(((res.get("raw") or {}).get("data") if isinstance(res.get("raw"), dict) and isinstance((res.get("raw") or {}).get("data"), list) else []), max_points=18),
                    },
                }
                return {"success": bool(res.get("price") is not None), "symbol": symbol, "date": date, "result": compact}

            if name == "binance_get_price_near_datetime":
                coin_or_symbol = str(args.get("coin_or_symbol") or "").strip()
                datetime_utc = str(args.get("datetime_utc") or "").strip()
                minutes_before = max(0, min(int(args.get("minutes_before", 15) or 15), 180))
                minutes_after = max(0, min(int(args.get("minutes_after", 15) or 15), 180))
                resolved = _resolve_symbol_safe(coin_or_symbol)
                if not resolved.get("success"):
                    return {
                        "success": False,
                        "error": "symbol_not_resolved",
                        "coin_or_symbol": coin_or_symbol,
                        "suggestions": resolved.get("suggestions", []),
                        "tried": resolved.get("tried", []),
                    }
                symbol = str(resolved.get("symbol"))
                try:
                    dt = dt_parser.parse(datetime_utc)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    dt = dt.astimezone(timezone.utc)
                except Exception:
                    return {"success": False, "error": "invalid_datetime_utc", "datetime_utc": datetime_utc}

                start_ms = int((dt.timestamp() - minutes_before * 60) * 1000)
                end_ms = int((dt.timestamp() + minutes_after * 60) * 1000)
                res = call_binance_endpoint(
                    endpoint_key="klines",
                    params={
                        "symbol": symbol,
                        "interval": "1m",
                        "startTime": start_ms,
                        "endTime": end_ms,
                        "limit": 1000,
                    },
                )
                if not res.get("success"):
                    return res
                rows = res.get("data") if isinstance(res.get("data"), list) else []
                candles = []
                for r in rows:
                    try:
                        ts = int(r[0])
                        o = float(r[1]); h = float(r[2]); l = float(r[3]); c = float(r[4])
                        candles.append({
                            "time": ts,
                            "date": datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat(),
                            "open": o,
                            "high": h,
                            "low": l,
                            "close": c,
                        })
                    except Exception:
                        continue
                if not candles:
                    return {"success": False, "error": "no_candles", "symbol": symbol}
                nearest = min(candles, key=lambda x: abs(int(x.get("time") or 0) - int(dt.timestamp() * 1000)))
                hi = max(float(x.get("high") or 0.0) for x in candles)
                lo = min(float(x.get("low") or 0.0) for x in candles)
                return {
                    "success": True,
                    "symbol": symbol,
                    "datetime_utc": dt.isoformat(),
                    "window_minutes": {"before": minutes_before, "after": minutes_after},
                    "summary": {
                        "nearest_close": nearest.get("close"),
                        "nearest_date": nearest.get("date"),
                        "window_high": hi,
                        "window_low": lo,
                        "candles_count": len(candles),
                    },
                    "candles": _compact_price_points(candles, max_points=30),
                }

            if name == "binance_get_price_window":
                coin_or_symbol = str(args.get("coin_or_symbol") or "").strip()
                tweet_date = str(args.get("tweet_date") or "").strip()
                relative_days = int(args.get("relative_days", 7) or 7)
                interval_hint = str(args.get("interval_hint") or "").strip() or None
                resolved = _resolve_symbol_safe(coin_or_symbol)
                if not resolved.get("success"):
                    return {
                        "success": False,
                        "error": "symbol_not_resolved",
                        "coin_or_symbol": coin_or_symbol,
                        "suggestions": resolved.get("suggestions", []),
                        "tried": resolved.get("tried", []),
                    }
                series = fetch_price_series_in_window(
                    symbol_or_coin=str(resolved.get("symbol") or coin_or_symbol),
                    tweet_date=tweet_date,
                    rel_days=relative_days,
                    interval_hint=interval_hint,
                )
                compact_series = dict(series)
                points = compact_series.get("points") if isinstance(compact_series.get("points"), list) else []
                compact_series["points_total"] = len(points)
                compact_series["points"] = _compact_price_points(points, max_points=24)
                if not compact_series.get("symbol"):
                    compact_series["symbol"] = resolved.get("symbol")
                return {"success": bool(series.get("success")), "result": compact_series}

            if name == "search_web":
                query = str(args.get("query") or "").strip()
                return search_web(query=query, api_key=self.api_key, base_url=self.base_url, model=self.web_model)

            return {"success": False, "error": f"unknown_tool:{name}"}
        except Exception as exc:  # noqa: BLE001
            return {
                "success": False,
                "error": f"tool_dispatch_exception:{type(exc).__name__}",
                "message": str(exc),
                "tool": name,
                "args": args,
            }

    def verify(self, tweet_text: str, tweet_date: str, tweet_metrics: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        tool_specs = _tool_specs()
        now_iso = datetime.now(timezone.utc).isoformat()
        metrics = tweet_metrics or {}

        system_prompt = (
            "Ты автономный агент проверки крипто-твитов с Function Calling. "
            "Работай строго по шагам: "
            "(1) Самостоятельно выдели claims, "
            "(2) Для каждого claim самостоятельно сформулируй критерии успешности (success criteria), "
            "(3) Реши какие инструменты нужны (Binance, web, или оба), "
            "(4) Вызови инструменты и собери доказательства, "
            "(5) Оцени каждый claim только по своим же критериям и выдай скоринг. "
            "Обязательно показывай использованные метрики и источники данных. "
            "Если данных недостаточно, помечай unknown и объясняй ограничения."
        )

        final_schema = {
            "scoring_rubric": {
                "weights": [{"criterion": "name", "weight_0_1": 0.0}],
                "aggregation_rule": "how claim score and final score are computed",
                "status_thresholds": {"true_min": 70, "unknown_min": 40, "false_max": 39},
            },
            "claims": [
                {
                    "claim": "text",
                    "claim_type": "price_target|direction|news|historical|other",
                    "coin_or_symbol": "BTC|ETH|BTCUSDT|...",
                    "target_price_usd": None,
                    "author_expectation": "text",
                    "success_criteria": [
                        {
                            "criterion": "text",
                            "pass_condition": "text",
                            "required_tools": ["binance", "web"],
                        }
                    ],
                    "requires": ["binance", "web"],
                }
            ],
            "evidence": {
                "binance": ["short facts"],
                "web": ["short facts"],
            },
            "comparison": [
                {
                    "claim": "text",
                    "status": "true|false|mixed|unknown",
                    "criteria_results": [
                        {
                            "criterion": "text",
                            "status": "true|false|unknown",
                            "confidence_0_100": 0,
                            "evidence": ["short fact"],
                        }
                    ],
                    "claim_score_0_100": 0,
                    "reason": "short",
                }
            ],
            "score": {
                "confidence_0_100": 0,
                "truth_ratio_0_1": 0,
                "status": "true|false|mixed|unknown",
            },
            "used_metrics": {"tweet_metrics": {}},
            "limitations": ["text"],
            "final_summary_ru": "2-6 sentences",
        }

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    "Проверь твит.\n"
                    f"Текущая дата (UTC): {now_iso}\n"
                    f"tweet_date: {tweet_date}\n"
                    f"tweet_text: {tweet_text}\n"
                    f"tweet_datetime_utc: {metrics.get('tweet_datetime_utc') if isinstance(metrics, dict) else ''}\n"
                    f"pre_extracted_context_json: {_safe_json((metrics.get('extracted_context') if isinstance(metrics, dict) else {}) or {})}\n"
                    f"tweet_metrics: {_safe_json(metrics)}\n\n"
                    "Требование: используй function calling, когда нужны внешние данные. "
                    "Используй pre_extracted_context_json как первичный слой извлеченных значений и флагов. "
                    "Если в claim есть цена-таргет, обязательно заполни target_price_usd числом. "
                    "Ты сам создаешь success_criteria и сам оцениваешь claim по этим критериям. "
                    "Не используй заранее заданные внешние формулы скоринга. "
                    "Финальный ответ верни строго JSON по схеме:\n"
                    f"{json.dumps(final_schema, ensure_ascii=False, indent=2)}"
                ),
            },
        ]

        raw_steps: List[Dict[str, Any]] = []

        for _ in range(self.max_steps):
            resp = self._chat_create(messages=messages, tools=tool_specs, tool_choice="auto")
            msg = resp.choices[0].message
            tool_calls = msg.tool_calls or []

            if not tool_calls:
                final_text = (msg.content or "").strip()
                final_json = _extract_json_object(final_text)
                return {
                    "success": True,
                    "model": self.model,
                    "tweet_date": tweet_date,
                    "tweet_text": tweet_text,
                    "tweet_metrics": metrics,
                    "agent_steps": raw_steps,
                    "final_raw": final_text,
                    "final_json": final_json,
                }

            assistant_tool_calls = []
            for tc in tool_calls:
                assistant_tool_calls.append(
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": assistant_tool_calls,
                }
            )

            for tc in tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except Exception:
                    args = {}

                tool_result = self._dispatch_tool(name, args)
                raw_steps.append({"tool": name, "args": args, "result": tool_result})
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": _safe_json(tool_result),
                    }
                )

        return {
            "success": False,
            "error": "max_steps_reached",
            "model": self.model,
            "agent_steps": raw_steps,
        }


def run_verification_fc(
    tweet_id: str,
    api_key: str,
    *,
    tweet_qid: str = "",
    nlp_api_key: str = "",
    nlp_model: str = AGENT_MODEL,
    nlp_base_url: str = AITUNNEL_BASE_URL,
    tweet_out: Optional[str] = "tweet_output.json",
    output: Optional[str] = "verification_output.json",
    runs_table: Optional[str] = "verification_runs.csv",
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> Dict[str, Any]:
    nlp_api_key = (nlp_api_key or AITUNNEL_API_KEY or "").strip()
    nlp_model = (nlp_model or AGENT_MODEL).strip()
    if not nlp_api_key:
        raise RuntimeError("AITUNNEL_API_KEY is required for FC-agent mode")

    if progress_callback:
        progress_callback(3, "Инициализация FC-агента")

    cookies = load_cookies()
    session = build_session(cookies)
    if tweet_qid:
        os.environ["TW_QID_TWEET"] = tweet_qid
    loaded_tweet_qid = load_tweet_qid()

    proxies = [p.strip() for p in (TW_PROXIES or "").split(",") if p.strip()]
    raw_payload = None
    last_err = None
    if progress_callback:
        progress_callback(10, "Получаем твит из X")
    for proxy in proxies:
        try:
            raw_payload = fetch_tweet_by_id(session, loaded_tweet_qid, tweet_id, {"http": proxy, "https": proxy})
            break
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
            continue
    if raw_payload is None:
        try:
            raw_payload = fetch_tweet_by_id(session, loaded_tweet_qid, tweet_id, None)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Tweet fetch failed. proxy_err={last_err}; direct_err={exc}")

    tweet_info = parse_tweet_json(raw_payload, tweet_id)
    try:
        dt = dt_parser.parse(tweet_info.get("created_at") or "")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
        tweet_info["tweet_datetime_utc"] = dt.isoformat()
        tweet_info["tweet_epoch_ms"] = int(dt.timestamp() * 1000)
    except Exception:
        tweet_info["tweet_datetime_utc"] = ""
        tweet_info["tweet_epoch_ms"] = 0
    if tweet_out:
        with open(tweet_out, "w", encoding="utf-8") as f:
            json.dump(tweet_info, f, ensure_ascii=False, indent=2)

    tweet_node = find_tweet_node_by_id(raw_payload, tweet_id) or find_tweet_node(raw_payload) or {}
    user_data = extract_user_result(tweet_node)
    tweet_legacy = tweet_node.get("legacy", {}) if isinstance(tweet_node, dict) else {}
    meta_features = extract_features_from_user(user_data, tweet_legacy, tweet_node) if tweet_node else {}
    if isinstance(tweet_legacy, dict):
        meta_features["favorite_count"] = int(tweet_legacy.get("favorite_count", 0) or 0)
        meta_features["retweet_count"] = int(tweet_legacy.get("retweet_count", 0) or 0)
        meta_features["reply_count"] = int(tweet_legacy.get("reply_count", 0) or 0)
        meta_features["quote_count"] = int(tweet_legacy.get("quote_count", 0) or 0)
        meta_features["bookmark_count"] = int(tweet_legacy.get("bookmark_count", 0) or 0)
    views_count = 0
    try:
        views_count = int((((tweet_node.get("views") or {}).get("count")) or ((tweet_node.get("legacy") or {}).get("views") or 0)) or 0)
    except Exception:
        views_count = 0
    meta_features["views_count"] = views_count
    author_info = build_author_info(user_data, meta_features)

    if progress_callback:
        progress_callback(24, "Локальная классификация твита")
    classifier_result = classify_tweet_text(tweet_info.get("tweet_text") or "")
    if not classifier_result.get("is_crypto", False):
        merged = {
            "tweet": tweet_info,
            "author": author_info,
            "classifier": classifier_result,
            "verdict": {"status": "skipped_non_crypto", "reason": "Tweet classified as non-crypto by local classifier"},
            "analysis": {
                "source": "classifier",
                "text": "Твит отнесен локальным классификатором к non-crypto. FC-агент и внешние инструменты не запускались.",
                "raw_model_output": "",
            },
            "binance": {"request": {}, "response": {}, "base": {}, "target": {}},
            "agent_logs": [],
        }
        if output:
            with open(output, "w", encoding="utf-8") as f:
                json.dump(merged, f, ensure_ascii=False, indent=2)
        return merged

    if progress_callback:
        progress_callback(35, "Запуск FC-агента (LLM + tools)")
    extracted_context = _extract_precheck_context_llm(
        api_key=nlp_api_key,
        base_url=nlp_base_url,
        tweet_text=tweet_info.get("tweet_text") or "",
        tweet_date=tweet_info.get("tweet_date") or "",
        tweet_metrics={
            "tweet_datetime_utc": tweet_info.get("tweet_datetime_utc"),
            "tweet_epoch_ms": tweet_info.get("tweet_epoch_ms"),
            "author": author_info,
            "meta_features": meta_features,
        },
        model=EXTRACT_MODEL,
    )
    verifier = FunctionCallingVerifier(
        api_key=nlp_api_key,
        base_url=nlp_base_url,
        model=nlp_model or AGENT_MODEL,
        web_model=WEB_MODEL,
        max_steps=10,
    )
    tweet_metrics = {
        "meta_features": meta_features,
        "author": author_info,
        "tweet_datetime_utc": tweet_info.get("tweet_datetime_utc"),
        "tweet_epoch_ms": tweet_info.get("tweet_epoch_ms"),
        "extracted_context": extracted_context,
    }
    fc_result = verifier.verify(
        tweet_text=tweet_info.get("tweet_text") or "",
        tweet_date=tweet_info.get("tweet_date") or "",
        tweet_metrics=tweet_metrics,
    )

    final_json = fc_result.get("final_json") if isinstance(fc_result.get("final_json"), dict) else {}
    claims = final_json.get("claims") if isinstance(final_json.get("claims"), list) else []
    comparison = final_json.get("comparison") if isinstance(final_json.get("comparison"), list) else []
    agent_steps = fc_result.get("agent_steps") if isinstance(fc_result.get("agent_steps"), list) else []
    if FC_FORCE_FORMULA:
        comparison_adj, formula_meta = _reconcile_comparison_with_formula(
            claims,
            comparison,
            agent_steps,
            extracted_context=extracted_context,
            tweet_text=tweet_info.get("tweet_text") or "",
        )
    else:
        comparison_adj = [dict(c) for c in comparison] if isinstance(comparison, list) else []
        formula_meta = {
            "version": "llm_adaptive_v1",
            "mode": "llm_defined_claims_criteria_scoring",
            "observation_used": _collect_price_observation(agent_steps),
            "extracted_context_used": extracted_context if isinstance(extracted_context, dict) else {},
            "claim_rows": [],
            "avg_claim_score": None,
        }
    score_hint = final_json.get("score") if isinstance(final_json.get("score"), dict) else {}
    calc = _calc_score_from_comparison(comparison_adj, score_hint)

    binance_calls = [s for s in agent_steps if str(s.get("tool") or "").startswith("binance_") or str(s.get("tool") or "") in {"get_binance_endpoint_catalog", "list_binance_symbols"}]
    compact_binance_calls = []
    for s in binance_calls:
        compact_binance_calls.append(
            {
                "tool": s.get("tool"),
                "args": _compact_for_llm(s.get("args") if isinstance(s.get("args"), dict) else {}),
                "result": _compact_for_llm(s.get("result") if isinstance(s.get("result"), dict) else {}),
            }
        )
    news_verification = _build_news_verification_from_logs(agent_steps, comparison_adj)

    claim_results = []
    for c in comparison_adj:
        claim_results.append(
            {
                "claim": c.get("claim"),
                "claim_type": c.get("claim_type") or "other",
                "status": c.get("status") or "unknown",
                "cannot_verify_reason": "" if str(c.get("status") or "unknown").lower() != "unknown" else str(c.get("reason") or "").strip(),
                "criteria_results": c.get("criteria_results") if isinstance(c.get("criteria_results"), list) else [],
                "checks": [{"status": c.get("status") or "unknown", "reason": str(c.get("reason") or "").strip(), "confidence_0_100": c.get("confidence_0_100")}],
                "score_formula": {
                    "confidence_0_100": c.get("claim_score_0_100") if isinstance(c.get("claim_score_0_100"), (int, float)) else c.get("confidence_0_100"),
                    "deviation_pct": c.get("deviation_pct"),
                    "nearest_deviation_pct": c.get("nearest_deviation_pct"),
                    "hit_in_window": c.get("hit_in_window"),
                    "target_price_usd": c.get("target_price_usd"),
                    "observed_price_usd": c.get("observed_price_usd"),
                    "observed_at": c.get("observed_at"),
                },
            }
        )

    mini_summaries = []
    for i, c in enumerate(comparison_adj, start=1):
        dev = c.get("deviation_pct")
        dev_txt = f" | отклонение={float(dev):.6f}%" if isinstance(dev, (int, float)) else ""
        ndev = c.get("nearest_deviation_pct")
        ndev_txt = f" | nearest={float(ndev):.6f}%" if isinstance(ndev, (int, float)) else ""
        hit_txt = f" | hit_in_window={bool(c.get('hit_in_window'))}" if c.get("hit_in_window") is not None else ""
        mini_summaries.append(
            {
                "index": i,
                "claim": c.get("claim"),
                "status": c.get("status") or "unknown",
                "reason": (str(c.get("reason") or "").strip() + dev_txt + ndev_txt + hit_txt).strip(),
                "confidence_0_100": c.get("claim_score_0_100") if isinstance(c.get("claim_score_0_100"), (int, float)) else c.get("confidence_0_100"),
            }
        )

    if progress_callback:
        progress_callback(85, "Оценка автора и сбор финального JSON")
    author_username = author_info.get("username") or "author"
    author_profile = {
        "user_id": "",
        "username": author_username,
        "display_name": author_info.get("display_name") or "",
        "description": author_info.get("description") or "",
        "verified": bool(author_info.get("verified")),
        "protected": False,
        "followers_count": int(meta_features.get("followers_count") or 0),
        "friends_count": int(meta_features.get("friends_count") or 0),
        "followers_friends_ratio": round(float(meta_features.get("followers_count") or 0) / max(float(meta_features.get("friends_count") or 0), 1.0), 4),
        "statuses_count": int(meta_features.get("statuses_count") or 0),
        "favourites_count": int(meta_features.get("favourites_count") or 0),
        "listed_count": int(meta_features.get("listed_count") or 0),
        "media_count": int(meta_features.get("media_count") or 0),
        "account_age_days": int(meta_features.get("account_age_days") or 0),
        "tweets_per_day": float(meta_features.get("tweets_per_day") or 0),
    }
    stats_src, stats_txt = build_account_stats_comment_llm(
        author_username,
        author_profile,
        {"from_mode": "single_tweet_fc"},
        nlp_api_key,
        nlp_model,
        nlp_base_url,
    )

    summary_text = str(final_json.get("final_summary_ru") or "").strip()
    if not summary_text:
        summary_text = "FC-агент завершил проверку, но не вернул финальный текст."
    if FC_FORCE_FORMULA:
        avg_claim_score = formula_meta.get("avg_claim_score")
        if isinstance(avg_claim_score, (int, float)):
            summary_text += f" Формульный скор по claim-ам: {float(avg_claim_score):.2f}/100."
    merged = {
        "tweet": tweet_info,
        "author": author_info,
        "author_profile": author_profile,
        "author_profile_comment": {"source": stats_src, "text": stats_txt},
        "classifier": classifier_result,
        "planner": {
            "source": "fc_agent",
            "claims": claims,
            "raw_model_output": fc_result.get("final_raw") or "",
        },
        "executor": {
            "claim_results": claim_results,
            "mini_summaries": mini_summaries,
        },
        "judge": calc,
        "verdict": {
            "status": calc.get("status", "unknown"),
            "checks": calc.get("checks", []),
            "counts": calc.get("counts", {}),
            "score": calc.get("score", {}),
        },
        "analysis": {
            "source": "fc_agent_llm",
            "text": summary_text,
            "raw_model_output": fc_result.get("final_raw") or "",
        },
        "scoring_formula": formula_meta,
        "binance": {
            "request": {"calls": [{"tool": s.get("tool"), "args": s.get("args")} for s in compact_binance_calls]},
            "response": {"calls": [{"tool": s.get("tool"), "result": s.get("result")} for s in compact_binance_calls]},
            "base": {},
            "target": {},
        },
        "news_verification": news_verification,
        "agent_logs": agent_steps,
        "fc_agent": fc_result,
        "extractor": {
            "source": "cheap_llm_single_prompt",
            "model": EXTRACT_MODEL,
            "result": extracted_context,
        },
    }

    if output:
        with open(output, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
    if runs_table:
        _append_runs_table(runs_table, tweet_info, {"claims": claims}, merged)
    if progress_callback:
        progress_callback(100, "Готово")
    return merged


def run_account_verification_fc(
    username: str,
    api_key: str,
    *,
    count: int = 30,
    nlp_api_key: str = "",
    nlp_model: str = AGENT_MODEL,
    nlp_base_url: str = AITUNNEL_BASE_URL,
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> Dict[str, Any]:
    from twitter_scraper import (
        build_session as ts_build_session,
        extract_entries,
        fetch_user_tweets,
        load_cookies as ts_load_cookies,
        load_query_ids,
        tweets_to_rows,
        user_by_screen_name,
    )

    nlp_api_key = (nlp_api_key or AITUNNEL_API_KEY or "").strip()
    nlp_model = (nlp_model or AGENT_MODEL).strip()
    if not nlp_api_key:
        raise RuntimeError("AITUNNEL_API_KEY is required for FC-agent account mode")

    uname = (username or "").strip().lstrip("@")
    if not uname:
        raise RuntimeError("Username is empty")

    if progress_callback:
        progress_callback(5, "Загружаем профиль и последние посты")

    cookies = ts_load_cookies()
    qids = load_query_ids()
    session = ts_build_session(cookies)
    user_id = user_by_screen_name(session, uname, qids["user_by_screen_name"])

    fetch_pool = 200
    timeline_json = fetch_user_tweets(session, user_id, qids["user_tweets"], count=fetch_pool)
    tweet_legacy_list = extract_entries(timeline_json)
    rows = tweets_to_rows(tweet_legacy_list)
    account_profile = extract_account_profile_from_timeline(timeline_json, uname, user_id)

    analyzed: List[Dict[str, Any]] = []
    selected_rows = rows[: max(1, int(count or 30))]
    total_rows = max(len(selected_rows), 1)
    for idx_row, row in enumerate(selected_rows, start=1):
        if progress_callback:
            base = 10
            span = 78
            progress_callback(base + int((idx_row - 1) / total_rows * span), f"FC: Обрабатываем пост {idx_row}/{total_rows}")

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
                    "tweet_metrics": {
                        "favorite_count": int(row.get("favorite_count", 0) or 0),
                        "retweet_count": int(row.get("retweet_count", 0) or 0),
                        "reply_count": int(row.get("reply_count", 0) or 0),
                        "quote_count": int(row.get("quote_count", 0) or 0),
                        "bookmark_count": int(row.get("bookmark_count", 0) or 0),
                        "views_count": int(row.get("views_count", 0) or 0),
                    },
                    "included": False,
                    "skip_reason": "non_crypto_by_local_classifier",
                    "classifier": cls,
                }
            )
            continue

        try:
            one = run_verification_fc(
                tweet_id=tweet_id,
                api_key=api_key,
                tweet_qid=os.getenv("TW_QID_TWEET", "").strip(),
                nlp_api_key=nlp_api_key,
                nlp_model=nlp_model,
                nlp_base_url=nlp_base_url,
                tweet_out=None,
                output=None,
                runs_table=os.getenv("RUNS_TABLE", "verification_runs.csv"),
                progress_callback=None,
            )
            analyzed.append(
                {
                    "tweet_id": tweet_id,
                    "tweet_text": one.get("tweet", {}).get("tweet_text", tweet_text),
                    "tweet_date": one.get("tweet", {}).get("tweet_date"),
                    "tweet_datetime_utc": (one.get("tweet", {}) or {}).get("tweet_datetime_utc"),
                    "included": True,
                    "verdict": one.get("verdict", {}),
                    "analysis": one.get("analysis", {}),
                    "binance": one.get("binance", {}),
                    "executor": one.get("executor", {}),
                    "claim_payload": {"claims": ((one.get("planner") or {}).get("claims") or [])},
                    "scoring_formula": one.get("scoring_formula", {}),
                    "classifier": (one.get("classifier") or cls),
                    "agent_logs": one.get("agent_logs", []),
                    "tweet_metrics": {
                        "favorite_count": int(row.get("favorite_count", 0) or 0),
                        "retweet_count": int(row.get("retweet_count", 0) or 0),
                        "reply_count": int(row.get("reply_count", 0) or 0),
                        "quote_count": int(row.get("quote_count", 0) or 0),
                        "bookmark_count": int(row.get("bookmark_count", 0) or 0),
                        "views_count": int(row.get("views_count", 0) or 0),
                    },
                }
            )
        except Exception as exc:  # noqa: BLE001
            analyzed.append(
                {
                    "tweet_id": tweet_id,
                    "tweet_text": tweet_text,
                    "created_at": row.get("created_at"),
                    "tweet_metrics": {
                        "favorite_count": int(row.get("favorite_count", 0) or 0),
                        "retweet_count": int(row.get("retweet_count", 0) or 0),
                        "reply_count": int(row.get("reply_count", 0) or 0),
                        "quote_count": int(row.get("quote_count", 0) or 0),
                        "bookmark_count": int(row.get("bookmark_count", 0) or 0),
                        "views_count": int(row.get("views_count", 0) or 0),
                    },
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
    decidable = true_n + false_n
    score_values = []
    for x in crypto_posts:
        sc = (((x.get("verdict") or {}).get("score") or {}).get("confidence_0_100"))
        if isinstance(sc, (int, float)):
            score_values.append(float(sc))
    avg_score = round(sum(score_values) / len(score_values), 2) if score_values else 0.0

    # Trust policy based only on crypto posts; skipped non-crypto posts are neutral.
    if decidable >= 2 and accuracy >= 0.9 and avg_score >= 80:
        trust_level = "high"
    elif decidable >= 1 and accuracy >= 0.6 and avg_score >= 65:
        trust_level = "medium"
    else:
        trust_level = "low"

    agg_likes = sum(int((x.get("tweet_metrics") or {}).get("favorite_count", 0) or 0) for x in analyzed)
    agg_retweets = sum(int((x.get("tweet_metrics") or {}).get("retweet_count", 0) or 0) for x in analyzed)
    agg_replies = sum(int((x.get("tweet_metrics") or {}).get("reply_count", 0) or 0) for x in analyzed)
    agg_quotes = sum(int((x.get("tweet_metrics") or {}).get("quote_count", 0) or 0) for x in analyzed)
    agg_bookmarks = sum(int((x.get("tweet_metrics") or {}).get("bookmark_count", 0) or 0) for x in analyzed)
    agg_views = sum(int((x.get("tweet_metrics") or {}).get("views_count", 0) or 0) for x in analyzed)
    followers = int((account_profile or {}).get("followers_count") or 0)
    interactions = agg_likes + agg_retweets + agg_replies + agg_quotes
    engagement_rate_overall = round(float(interactions) / max(float(followers) * max(len(analyzed), 1), 1.0), 6)

    result: Dict[str, Any] = {
        "username": uname,
        "user_id": user_id,
        "account_profile": account_profile,
        "mode": "account_fc",
        "totals": {
            "fetched_posts": len(selected_rows),
            "crypto_posts": len(crypto_posts),
            "skipped_non_crypto": len([x for x in analyzed if not x.get("included")]),
            "true": true_n,
            "false": false_n,
            "unknown": unknown_n,
            "accuracy_on_decidable": round(accuracy, 4),
            "decidable_crypto_posts": decidable,
            "avg_confidence_score": avg_score,
            "trust_level": trust_level,
            "engagement": {
                "likes": agg_likes,
                "retweets": agg_retweets,
                "replies": agg_replies,
                "quotes": agg_quotes,
                "bookmarks": agg_bookmarks,
                "views": agg_views,
                "interactions_total": interactions,
                "engagement_rate_overall": engagement_rate_overall,
            },
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
            item["tweet_metrics"] = p.get("tweet_metrics", {})
        elif p.get("error"):
            item["status"] = "error"
            item["error"] = p.get("error")
            item["tweet_metrics"] = p.get("tweet_metrics", {})
        else:
            item["status"] = (p.get("verdict") or {}).get("status", "unknown")
            item["score"] = ((p.get("verdict") or {}).get("score") or {}).get("confidence_0_100")
            item["claim_payload"] = p.get("claim_payload", {})
            item["binance_request"] = ((p.get("binance") or {}).get("request") or {})
            item["binance_response"] = ((p.get("binance") or {}).get("response") or {})
            item["micro_summary"] = ((p.get("analysis") or {}).get("text") or "")
            item["scoring_formula"] = p.get("scoring_formula", {})
            item["tweet_metrics"] = p.get("tweet_metrics", {})
        micro_research.append(item)
    result["micro_research"] = micro_research

    if progress_callback:
        progress_callback(92, "Генерируем сводный вывод по аккаунту (FC)")
    src, txt = build_account_summary_llm(uname, result, nlp_api_key, nlp_model, nlp_base_url)
    stats_src, stats_txt = build_account_stats_comment_llm(
        uname,
        account_profile,
        result.get("totals", {}),
        nlp_api_key,
        nlp_model,
        nlp_base_url,
    )
    result["account_profile_comment"] = {"source": stats_src, "text": stats_txt}
    combined_text = (txt or "").strip()
    if stats_txt:
        combined_text = f"{combined_text}\n\nС учетом метрик аккаунта: {stats_txt}" if combined_text else f"С учетом метрик аккаунта: {stats_txt}"
    result["account_assessment"] = {"source": src, "text": combined_text}

    if progress_callback:
        progress_callback(100, "Готово")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Function-calling LLM crypto verification agent")
    parser.add_argument("--tweet-text", required=True, help="Tweet text to verify")
    parser.add_argument("--tweet-date", required=True, help="Tweet date YYYY-MM-DD")
    parser.add_argument("--metrics-json", default="", help="Optional JSON string with tweet/account metrics")
    parser.add_argument("--output", default="fc_agent_output.json", help="Output JSON path")
    parser.add_argument("--model", default=AGENT_MODEL, help="Agent model (default: gpt-5.4-nano)")
    parser.add_argument("--web-model", default=WEB_MODEL, help="Web model for search tool (default: sonar)")
    args = parser.parse_args()

    metrics: Dict[str, Any] = {}
    if args.metrics_json:
        try:
            metrics = json.loads(args.metrics_json)
            if not isinstance(metrics, dict):
                metrics = {}
        except Exception:
            metrics = {}

    verifier = FunctionCallingVerifier(
        api_key=AITUNNEL_API_KEY,
        base_url=AITUNNEL_BASE_URL,
        model=args.model,
        web_model=args.web_model,
    )
    result = verifier.verify(
        tweet_text=args.tweet_text,
        tweet_date=args.tweet_date,
        tweet_metrics=metrics,
    )

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"Saved: {args.output}")


if __name__ == "__main__":
    main()
