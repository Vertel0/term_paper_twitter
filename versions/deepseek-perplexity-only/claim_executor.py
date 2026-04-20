#!/usr/bin/env python
# -*- coding: utf-8 -*-

from datetime import datetime, timedelta, timezone
from typing import List, Optional

from dateutil import parser


_POINT_QUERY_CACHE: dict[tuple, dict] = {}


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
    from binance_query import process_input

    key = _point_query_cache_key(query, api_key)
    if key in _POINT_QUERY_CACHE:
        return dict(_POINT_QUERY_CACHE[key])
    res = process_input(query, api_key)[0]
    _POINT_QUERY_CACHE[key] = dict(res or {})
    return dict(res or {})


def _fetch_price_series_in_window(coin: str, tweet_date: str, rel_days: int, api_key: str) -> dict:
    from binance_query import fetch_price_series_in_window

    return fetch_price_series_in_window(coin, tweet_date, rel_days)


def execute_claim_with_binance(claim: dict, tweet_date: str, api_key: str) -> dict:
    ctype = claim.get("claim_type")
    coin = claim.get("coin")
    rel_raw = claim.get("relative_days", 7)
    try:
        rel_days = int(7 if rel_raw is None else rel_raw)
    except Exception:
        rel_days = 7
    target_price_claim = _to_float_or_none(claim.get("target_price_usd"))
    amount_usd_claim = _to_float_or_none(claim.get("amount_usd"))
    lookback_minutes = int(claim.get("lookback_minutes", 15) or 15)
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

    if "historical statement" in str(result.get("cannot_verify_reason") or "").lower():
        result["status"] = "unknown"
        result["checks"].append({"name": "historical_statement_not_forward_predict", "ok": False})
        return result

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

    if ctype == "liquidation_amount":
        from binance_query import fetch_quote_volume_multi_intervals

        if not coin:
            result["cannot_verify_reason"] = result["cannot_verify_reason"] or "Missing coin in liquidation claim"
            return result

        if lookback_minutes <= 5:
            intervals = ["1m", "5m", "15m", "30m", "1h"]
        elif lookback_minutes <= 15:
            intervals = ["5m", "15m", "30m", "1h"]
        else:
            intervals = ["15m", "30m", "1h", "4h"]

        series = fetch_quote_volume_multi_intervals(coin, tweet_date, intervals=intervals)
        if not series.get("success"):
            result["cannot_verify_reason"] = result["cannot_verify_reason"] or str(series.get("error") or "No Binance candles")
            return result

        trials = [t for t in (series.get("trials") or []) if t.get("success") and _to_float_or_none(t.get("max_quote_volume_usd")) is not None]
        if not trials:
            result["cannot_verify_reason"] = result["cannot_verify_reason"] or "No successful Binance interval trials"
            return result

        if amount_usd_claim is None:
            result["status"] = "unknown"
            result["checks"].append({"name": "liquidation_proxy_observed", "ok": True})
            result["cannot_verify_reason"] = result["cannot_verify_reason"] or "Claim amount missing or Binance proxy unavailable"
            result["evidence"].update(
                {
                    "symbol": series.get("symbol"),
                    "interval_trials": trials,
                    "proxy_note": "quote_volume_usd is a proxy metric and not direct liquidation feed",
                }
            )
            return result

        best = min(trials, key=lambda t: abs(_to_float_or_none(t.get("max_quote_volume_usd")) - amount_usd_claim))
        best_observed = _to_float_or_none(best.get("max_quote_volume_usd"))
        best_dev = abs(best_observed - amount_usd_claim) / max(abs(amount_usd_claim), 1e-9) * 100.0

        threshold_pct = 40.0  # proxy tolerance
        passed_intervals = []
        for t in trials:
            obs = _to_float_or_none(t.get("max_quote_volume_usd"))
            dev = abs(obs - amount_usd_claim) / max(abs(amount_usd_claim), 1e-9) * 100.0
            t["deviation_pct"] = round(dev, 4)
            t["claim_amount_usd"] = amount_usd_claim
            if dev <= threshold_pct:
                passed_intervals.append(str(t.get("interval")))

        ok = bool(passed_intervals)
        result["checks"].append(
            {
                "name": "liquidation_amount_proxy_multi_interval_40pct",
                "ok": ok,
                "claimed_amount_usd": amount_usd_claim,
                "best_interval": best.get("interval"),
                "observed_proxy_usd": best_observed,
                "deviation_pct": round(best_dev, 4),
                "passed_intervals": passed_intervals,
            }
        )
        result["evidence"].update(
            {
                "symbol": series.get("symbol"),
                "best_interval": best.get("interval"),
                "best_observed_proxy_usd": best_observed,
                "best_observed_date": best.get("max_quote_volume_date"),
                "best_deviation_pct": round(best_dev, 4),
                "interval_trials": trials,
                "proxy_note": "quote_volume_usd is a proxy metric and not direct liquidation feed",
            }
        )
        result["status"] = "true" if ok else "false"
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
            target_day_dev_pct = abs(p_target - target_price_claim) / max(abs(target_price_claim), 1e-9) * 100.0
            if points:
                hits = [
                    p
                    for p in points
                    if abs(float(p.get("price_usd") or 0.0) - target_price_claim) / max(abs(target_price_claim), 1e-9) <= tol
                ]
                closest = min(points, key=lambda p: abs(float(p.get("price_usd") or 0.0) - target_price_claim))
                closest_dev_pct = abs(float(closest.get("price_usd") or 0.0) - target_price_claim) / max(abs(target_price_claim), 1e-9) * 100.0
                ok = bool(hits)
                result["checks"].append(
                    {
                        "name": "target_price_5pct_in_window",
                        "ok": ok,
                        "target_claim": target_price_claim,
                        "hits_in_window": len(hits),
                        "closest_observed": closest.get("price_usd"),
                        "closest_date": closest.get("date"),
                        "closest_deviation_pct": round(closest_dev_pct, 4),
                        "target_day_deviation_pct": round(target_day_dev_pct, 4),
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
                    "closest_deviation_pct": round(closest_dev_pct, 4),
                    "target_day_deviation_pct": round(target_day_dev_pct, 4),
                }
            else:
                ok = abs(p_target - target_price_claim) / max(abs(target_price_claim), 1e-9) <= tol
                result["checks"].append({
                    "name": "target_price_5pct",
                    "ok": ok,
                    "target_claim": target_price_claim,
                    "observed": p_target,
                    "target_day_deviation_pct": round(target_day_dev_pct, 4),
                })

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
        result["cannot_verify_reason"] = (
            result["cannot_verify_reason"]
            or "Market cap is not available in Binance data-api spot candles; use a fundamentals provider for this claim type"
        )
        result["status"] = "unknown"
        result["checks"].append({"name": "market_cap_not_supported_by_binance_data_api", "ok": False})
        return result

    if ctype == "volatility_risk":
        series = _fetch_price_series_in_window(coin, tweet_date, max(rel_days, 14), api_key)
        pts = series.get("points") or []
        prices = [_to_float_or_none(p.get("price_usd")) for p in pts]
        prices = [p for p in prices if p is not None and p > 0]
        if len(prices) < 5:
            result["cannot_verify_reason"] = result["cannot_verify_reason"] or "Not enough Binance history points for volatility"
            return result
        rets = []
        for i in range(1, len(prices)):
            rets.append((prices[i] - prices[i - 1]) / max(abs(prices[i - 1]), 1e-9))
        vol_pct = (sum((r - (sum(rets) / len(rets))) ** 2 for r in rets) / max(len(rets) - 1, 1)) ** 0.5 * 100
        result["evidence"].update({"volatility_pct_daily": vol_pct, "history_points": len(prices), "series_meta": {
            "symbol": series.get("symbol"),
            "interval": series.get("interval"),
            "start_date": series.get("start_date"),
            "end_date": series.get("end_date"),
        }})
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
        "liquidation_amount": 0.65,
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

    precise_price_hit = False
    core_false = False
    for r in claim_results:
        ctype = str(r.get("claim_type") or "")
        st = str(r.get("status") or "unknown")
        if ctype in ("price_target", "relative_performance") and st == "false":
            core_false = True
        if ctype == "price_target" and st == "true":
            ev = r.get("evidence") or {}
            tw = ev.get("target_window") or {}
            cdp = tw.get("closest_deviation_pct")
            tdp = tw.get("target_day_deviation_pct")
            if isinstance(cdp, (int, float)) and cdp <= 1.0:
                precise_price_hit = True
            if isinstance(tdp, (int, float)) and tdp <= 1.0:
                precise_price_hit = True

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

    if status == "false" and precise_price_hit and not core_false:
        status = "true"

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
    rel_raw = claim.get("relative_days", 7)
    try:
        rel_days = int(7 if rel_raw is None else rel_raw)
    except Exception:
        rel_days = 7
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


def compact_market_result(result: dict) -> dict:
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


# Backward compatibility aliases
execute_claim_with_coincap = execute_claim_with_binance
compact_coincap_result = compact_market_result
