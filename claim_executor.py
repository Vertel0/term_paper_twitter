#!/usr/bin/env python
# -*- coding: utf-8 -*-

from datetime import datetime, timedelta, timezone
from typing import List, Optional

import requests
from dateutil import parser


_POINT_QUERY_CACHE: dict[tuple, dict] = {}


def _coincap_get_json(path: str, api_key: str, params: Optional[dict] = None) -> tuple[Optional[dict], Optional[str], int]:
    import os

    url = f"{os.getenv('COINCAP_BASE', 'https://rest.coincap.io/v3')}{path}"
    try:
        r = requests.get(url, params=params, headers={"Authorization": f"Bearer {api_key}"}, timeout=25)
        if not r.ok:
            return None, f"status_{r.status_code}:{r.text[:250]}", r.status_code
        return r.json() or {}, None, r.status_code
    except Exception as e:  # noqa: BLE001
        return None, str(e), 0


def _to_float_or_none(v) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _point_query_cache_key(query: dict, api_key: str) -> tuple:
    q = query or {}
    return (
        api_key,
        str(q.get("coin") or "").lower(),
        str(q.get("tweet_date") or ""),
        int(q.get("relative_days") or 0),
        str(q.get("vs_currency") or "usd").lower(),
        int(q.get("search_window_days") or 0),
    )


def _process_single_query_cached(query: dict, api_key: str) -> dict:
    from coingecko_query import process_input

    key = _point_query_cache_key(query, api_key)
    if key in _POINT_QUERY_CACHE:
        return dict(_POINT_QUERY_CACHE[key])
    res = process_input(query, api_key)[0]
    _POINT_QUERY_CACHE[key] = dict(res or {})
    return dict(res or {})


def _fetch_price_series_in_window(coin: str, tweet_date: str, rel_days: int, api_key: str) -> dict:
    from coingecko_query import resolve_asset_slug

    try:
        start_date = parser.parse(tweet_date).date()
    except Exception:
        return {"success": False, "error": "invalid_tweet_date", "points": []}

    rel_days = max(0, int(rel_days or 0))
    end_date = start_date + timedelta(days=rel_days)
    start_dt = datetime(start_date.year, start_date.month, start_date.day, 0, 0, tzinfo=timezone.utc)
    end_dt = datetime(end_date.year, end_date.month, end_date.day, 23, 59, tzinfo=timezone.utc)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    if rel_days <= 14:
        interval = "h1"
    elif rel_days <= 60:
        interval = "h6"
    else:
        interval = "d1"

    asset_id = resolve_asset_slug(coin, api_key) or coin
    payload, err, status = _coincap_get_json(
        f"/assets/{asset_id}/history",
        api_key,
        params={"interval": interval, "start": start_ms, "end": end_ms},
    )
    if err or not payload:
        return {
            "success": False,
            "error": err or "history_request_failed",
            "status_code": status,
            "asset_id": asset_id,
            "interval": interval,
            "points": [],
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }

    points_raw = (payload.get("data") or []) if isinstance(payload, dict) else []
    points = []
    for p in points_raw:
        ts = p.get("time")
        price = _to_float_or_none(p.get("priceUsd"))
        if ts is None or price is None:
            continue
        points.append({"time": int(ts), "price_usd": float(price), "date": p.get("date")})

    return {
        "success": bool(points),
        "error": "" if points else "no_points",
        "status_code": status,
        "asset_id": asset_id,
        "interval": interval,
        "points": points,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
    }


def execute_claim_with_coincap(claim: dict, tweet_date: str, api_key: str) -> dict:
    ctype = claim.get("claim_type")
    coin = claim.get("coin")
    rel_days = int(claim.get("relative_days", 7) or 7)
    target_price_claim = _to_float_or_none(claim.get("target_price_usd"))
    direction = claim.get("direction")

    result = {
        "claim_id": claim.get("claim_id"),
        "claim_type": ctype,
        "coin": coin,
        "status": "unknown",
        "checks": [],
        "evidence": {},
        "cannot_verify_reason": claim.get("cannot_verify_reason", ""),
    }

    if ctype == "timeframe_only":
        if not coin:
            result["cannot_verify_reason"] = result["cannot_verify_reason"] or "Missing coin in claim"
            return result
        if rel_days <= 7:
            checkpoint_days = list(range(0, rel_days + 1))
            window_days = 7
        else:
            checkpoint_days = sorted(
                {
                    max(rel_days - 3, 0),
                    rel_days,
                    rel_days + 3,
                    rel_days + 7,
                    rel_days + 14,
                    rel_days + 30,
                }
            )
            window_days = 1

        queries = [
            {
                "coin": coin,
                "tweet_date": tweet_date,
                "relative_days": d,
                "vs_currency": "usd",
                "search_window_days": window_days,
            }
            for d in checkpoint_days
        ]
        points = [_process_single_query_cached(q, api_key) for q in queries]
        result["evidence"]["checkpoints"] = points
        observed = [p for p in points if p.get("success")]
        result["checks"].append(
            {
                "name": "timeframe_checkpoints_observed",
                "ok": bool(observed),
                "count": len(observed),
                "requested": len(points),
            }
        )
        result["status"] = "unknown"
        result["cannot_verify_reason"] = result["cannot_verify_reason"] or "Timeframe-only claim without numeric target"
        return result
    if not coin:
        result["cannot_verify_reason"] = result["cannot_verify_reason"] or "Missing coin in claim"
        return result

    if ctype in ("price_target", "direction"):
        target_query = {
            "coin": coin,
            "tweet_date": tweet_date,
            "relative_days": rel_days,
            "vs_currency": "usd",
            "search_window_days": 0,
        }
        base_query = {
            "coin": coin,
            "tweet_date": tweet_date,
            "relative_days": 0,
            "vs_currency": "usd",
            "search_window_days": 0,
        }
        target_res = _process_single_query_cached(target_query, api_key)
        base_res = _process_single_query_cached(base_query, api_key)
        p_base = _to_float_or_none(base_res.get("price_usd"))
        p_target = _to_float_or_none(target_res.get("price_usd"))

        result["evidence"].update(
            {
                "base": base_res,
                "target": target_res,
                "base_price_usd": p_base,
                "target_price_usd": p_target,
            }
        )

        if p_base is None or p_target is None:
            result["cannot_verify_reason"] = result["cannot_verify_reason"] or "Missing base/target prices"
            return result

        if ctype == "price_target" and target_price_claim is not None:
            tol = 0.05
            window_series = _fetch_price_series_in_window(coin, tweet_date, rel_days, api_key)
            points = window_series.get("points") or []
            if points:
                hits = [
                    p
                    for p in points
                    if abs(float(p.get("price_usd") or 0.0) - target_price_claim) / max(abs(target_price_claim), 1e-9) <= tol
                ]
                closest = min(points, key=lambda p: abs(float(p.get("price_usd") or 0.0) - target_price_claim))
                ok = bool(hits)
                result["checks"].append(
                    {
                        "name": "target_price_5pct_in_window",
                        "ok": ok,
                        "target_claim": target_price_claim,
                        "hits_in_window": len(hits),
                        "closest_observed": closest.get("price_usd"),
                        "closest_date": closest.get("date"),
                        "window_start": window_series.get("start_date"),
                        "window_end": window_series.get("end_date"),
                        "interval": window_series.get("interval"),
                    }
                )
                result["evidence"]["target_window"] = {
                    "start_date": window_series.get("start_date"),
                    "end_date": window_series.get("end_date"),
                    "interval": window_series.get("interval"),
                    "points_count": len(points),
                    "hits_count": len(hits),
                    "closest": closest,
                }
            else:
                ok = abs(p_target - target_price_claim) / max(abs(target_price_claim), 1e-9) <= tol
                result["checks"].append({"name": "target_price_5pct", "ok": ok, "target_claim": target_price_claim, "observed": p_target})

        if direction == "up":
            result["checks"].append({"name": "direction_up", "ok": p_target > p_base})
        elif direction == "down":
            result["checks"].append({"name": "direction_down", "ok": p_target < p_base})

        if result["checks"]:
            result["status"] = "true" if all(c.get("ok") for c in result["checks"]) else "false"
        return result

    if ctype == "relative_performance":
        comp_coin = claim.get("comparison_coin")
        if not comp_coin:
            result["cannot_verify_reason"] = result["cannot_verify_reason"] or "Missing comparison_coin"
            return result

        target_query = {
            "coin": coin,
            "tweet_date": tweet_date,
            "relative_days": rel_days,
            "vs_currency": "usd",
            "search_window_days": 0,
        }
        base_query = {
            "coin": coin,
            "tweet_date": tweet_date,
            "relative_days": 0,
            "vs_currency": "usd",
            "search_window_days": 0,
        }
        target_res = _process_single_query_cached(target_query, api_key)
        base_res = _process_single_query_cached(base_query, api_key)
        p_base = _to_float_or_none(base_res.get("price_usd"))
        p_target = _to_float_or_none(target_res.get("price_usd"))
        result["evidence"].update({"base": base_res, "target": target_res, "base_price_usd": p_base, "target_price_usd": p_target})

        comp_target_query = dict(target_query)
        comp_target_query["coin"] = comp_coin
        comp_base_query = dict(base_query)
        comp_base_query["coin"] = comp_coin
        comp_t = _process_single_query_cached(comp_target_query, api_key)
        comp_b = _process_single_query_cached(comp_base_query, api_key)
        c_base = _to_float_or_none(comp_b.get("price_usd"))
        c_target = _to_float_or_none(comp_t.get("price_usd"))
        result["evidence"].update({"comparison_base": comp_b, "comparison_target": comp_t})

        if None in (p_base, p_target, c_base, c_target):
            result["cannot_verify_reason"] = result["cannot_verify_reason"] or "Missing prices for relative performance"
            return result

        ret_main = (p_target - p_base) / max(abs(p_base), 1e-9)
        ret_comp = (c_target - c_base) / max(abs(c_base), 1e-9)
        expected_out = bool(claim.get("expected_outperformance", True))
        ok = ret_main > ret_comp if expected_out else ret_main < ret_comp
        result["checks"].append(
            {
                "name": "relative_performance",
                "ok": ok,
                "main_return": ret_main,
                "comparison_return": ret_comp,
                "comparison_coin": comp_coin,
            }
        )
        result["status"] = "true" if ok else "false"
        return result

    if ctype == "market_cap":
        payload, err, _ = _coincap_get_json(f"/assets/{coin}", api_key)
        if err or not payload:
            result["cannot_verify_reason"] = result["cannot_verify_reason"] or f"market cap endpoint failed: {err}"
            return result
        asset = (payload.get("data") or {})
        mcap = _to_float_or_none(asset.get("marketCapUsd"))
        result["evidence"].update({"asset_snapshot": asset, "market_cap_usd": mcap})
        if mcap is None:
            result["cannot_verify_reason"] = result["cannot_verify_reason"] or "marketCapUsd is missing"
            return result
        result["status"] = "unknown"
        result["checks"].append({"name": "market_cap_observed", "ok": True})
        return result

    if ctype == "volatility_risk":
        target_dt = parser.parse(tweet_date) + timedelta(days=rel_days)
        end_ms = int(datetime(target_dt.year, target_dt.month, target_dt.day, 23, 59, tzinfo=timezone.utc).timestamp() * 1000)
        start_ms = int((datetime(target_dt.year, target_dt.month, target_dt.day, 0, 0, tzinfo=timezone.utc) - timedelta(days=14)).timestamp() * 1000)
        payload, err, _ = _coincap_get_json(f"/assets/{coin}/history", api_key, params={"interval": "d1", "start": start_ms, "end": end_ms})
        if err or not payload:
            result["cannot_verify_reason"] = result["cannot_verify_reason"] or f"volatility endpoint failed: {err}"
            return result
        pts = (payload.get("data") or [])
        prices = [_to_float_or_none(p.get("priceUsd")) for p in pts]
        prices = [p for p in prices if p is not None and p > 0]
        if len(prices) < 5:
            result["cannot_verify_reason"] = result["cannot_verify_reason"] or "Not enough history points for volatility"
            return result
        rets = []
        for i in range(1, len(prices)):
            rets.append((prices[i] - prices[i - 1]) / max(abs(prices[i - 1]), 1e-9))
        vol_pct = (sum((r - (sum(rets) / len(rets))) ** 2 for r in rets) / max(len(rets) - 1, 1)) ** 0.5 * 100
        result["evidence"].update({"volatility_pct_daily": vol_pct, "history_points": len(prices)})
        max_vol = _to_float_or_none(claim.get("max_volatility_pct"))
        if max_vol is None:
            result["status"] = "unknown"
            result["checks"].append({"name": "volatility_observed", "ok": True})
        else:
            ok = vol_pct <= max_vol
            result["checks"].append({"name": "volatility_threshold", "ok": ok, "observed": vol_pct, "claimed_max": max_vol})
            result["status"] = "true" if ok else "false"
        return result

    result["cannot_verify_reason"] = result["cannot_verify_reason"] or f"Unsupported claim type: {ctype}"
    return result


def judge_claim_results(claim_results: List[dict]) -> dict:
    true_n = sum(1 for r in claim_results if r.get("status") == "true")
    false_n = sum(1 for r in claim_results if r.get("status") == "false")
    unknown_n = sum(1 for r in claim_results if r.get("status") not in ("true", "false"))

    weights = {
        "price_target": 1.0,
        "direction": 1.0,
        "relative_performance": 0.8,
        "volatility_risk": 0.35,
        "market_cap": 0.25,
        "timeframe_only": 0.15,
    }
    weighted_true = 0.0
    weighted_false = 0.0
    weighted_unknown = 0.0
    hard_false = 0
    hard_total = 0
    for r in claim_results:
        ctype = str(r.get("claim_type") or "")
        st = str(r.get("status") or "unknown")
        w = float(weights.get(ctype, 0.5))
        if ctype in ("price_target", "direction", "relative_performance"):
            hard_total += 1
            if st == "false":
                hard_false += 1
        if st == "true":
            weighted_true += w
        elif st == "false":
            weighted_false += w
        else:
            weighted_unknown += w

    denom = max(weighted_true + weighted_false + weighted_unknown, 1e-9)
    effective = max(weighted_true + weighted_false, 1e-9)
    truth_ratio = weighted_true / effective
    confidence_score = int(round(max(weighted_true, weighted_false) / denom * 100))

    if hard_total > 0 and hard_false >= max(1, int(hard_total * 0.66)):
        status = "false"
    elif weighted_false > weighted_true * 1.1 and weighted_false >= 0.8:
        status = "false"
    elif weighted_true > weighted_false * 1.1 and weighted_true >= 0.8:
        status = "true"
    elif true_n == 0 and false_n == 0:
        status = "unknown"
    else:
        status = "mixed"

    checks = []
    for r in claim_results:
        checks.append(
            {
                "claim_id": r.get("claim_id"),
                "claim_type": r.get("claim_type"),
                "status": r.get("status"),
            }
        )

    return {
        "status": status,
        "counts": {"true": true_n, "false": false_n, "unknown": unknown_n, "total": len(claim_results)},
        "score": {
            "confidence_0_100": confidence_score,
            "truth_ratio": round(truth_ratio, 4),
            "weighted_true": round(weighted_true, 4),
            "weighted_false": round(weighted_false, 4),
            "weighted_unknown": round(weighted_unknown, 4),
        },
        "checks": checks,
    }


def claim_to_legacy_payload(claim: dict, tweet_date: str) -> dict:
    rel_days = int(claim.get("relative_days", 7) or 7)
    rel_days = max(0, min(rel_days, 365))
    return {
        "coin": claim.get("coin"),
        "tweet_date": tweet_date,
        "relative_days": rel_days,
        "vs_currency": "usd",
        "claim": {
            "target_price_usd": claim.get("target_price_usd"),
            "direction": claim.get("direction"),
            "horizon_days": rel_days,
            "confidence": None,
            "rationale": claim.get("rationale", ""),
        },
    }


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
