"""
Binance market data adapter.
Compatible with previous process_input() contract used by verifier.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

BINANCE_BASE = os.getenv("BINANCE_BASE", "https://data-api.binance.vision")
BINANCE_PROXY = os.getenv("BINANCE_PROXY", "").strip()

COIN_TO_SYMBOL = {
    "btc": "BTCUSDT",
    "bitcoin": "BTCUSDT",
    "eth": "ETHUSDT",
    "ethereum": "ETHUSDT",
    "bnb": "BNBUSDT",
    "binance-coin": "BNBUSDT",
    "sol": "SOLUSDT",
    "solana": "SOLUSDT",
    "ada": "ADAUSDT",
    "cardano": "ADAUSDT",
    "xrp": "XRPUSDT",
    "doge": "DOGEUSDT",
    "dogecoin": "DOGEUSDT",
}

# Public and semi-public endpoints that can be orchestrated by LLM planning.
# NOTE: Endpoints marked as signed require API secret/HMAC and are intentionally
# exposed as metadata for planning, but are not executed by default caller below.
BINANCE_ENDPOINTS: Dict[str, Dict[str, Any]] = {
    "ping": {
        "path": "/api/v3/ping",
        "method": "GET",
        "required_params": [],
        "optional_params": [],
        "signed": False,
        "description": "Connectivity check.",
    },
    "time": {
        "path": "/api/v3/time",
        "method": "GET",
        "required_params": [],
        "optional_params": [],
        "signed": False,
        "description": "Server time sync.",
    },
    "exchange_info": {
        "path": "/api/v3/exchangeInfo",
        "method": "GET",
        "required_params": [],
        "optional_params": ["symbol", "symbols", "permissions"],
        "signed": False,
        "description": "All tradable symbols, filters, precision, status.",
    },
    "trades_recent": {
        "path": "/api/v3/trades",
        "method": "GET",
        "required_params": ["symbol"],
        "optional_params": ["limit"],
        "signed": False,
        "description": "Recent trades tape.",
    },
    "trades_historical": {
        "path": "/api/v3/historicalTrades",
        "method": "GET",
        "required_params": ["symbol"],
        "optional_params": ["limit", "fromId"],
        "signed": False,
        "description": "Older trades (database).",
    },
    "agg_trades": {
        "path": "/api/v3/aggTrades",
        "method": "GET",
        "required_params": ["symbol"],
        "optional_params": ["fromId", "startTime", "endTime", "limit"],
        "signed": False,
        "description": "Compressed aggregated trades.",
    },
    "order_book_depth": {
        "path": "/api/v3/depth",
        "method": "GET",
        "required_params": ["symbol"],
        "optional_params": ["limit"],
        "signed": False,
        "description": "Order book snapshot.",
    },
    "klines": {
        "path": "/api/v3/klines",
        "method": "GET",
        "required_params": ["symbol", "interval"],
        "optional_params": ["startTime", "endTime", "limit"],
        "signed": False,
        "description": "OHLCV candles.",
    },
    "ticker_price": {
        "path": "/api/v3/ticker/price",
        "method": "GET",
        "required_params": [],
        "optional_params": ["symbol", "symbols"],
        "signed": False,
        "description": "Last traded price.",
    },
    "avg_price": {
        "path": "/api/v3/avgPrice",
        "method": "GET",
        "required_params": ["symbol"],
        "optional_params": [],
        "signed": False,
        "description": "Rolling average price.",
    },
    "book_ticker": {
        "path": "/api/v3/ticker/bookTicker",
        "method": "GET",
        "required_params": [],
        "optional_params": ["symbol", "symbols"],
        "signed": False,
        "description": "Best bid/ask and quantities.",
    },
    "ticker_24hr": {
        "path": "/api/v3/ticker/24hr",
        "method": "GET",
        "required_params": [],
        "optional_params": ["symbol", "symbols", "type"],
        "signed": False,
        "description": "24h price/volume/open-high-low stats.",
    },
    "ticker_window": {
        "path": "/api/v3/ticker",
        "method": "GET",
        "required_params": [],
        "optional_params": ["symbol", "symbols", "windowSize", "type"],
        "signed": False,
        "description": "Rolling window ticker statistics.",
    },
    "system_status": {
        "path": "/sapi/v1/system/status",
        "method": "GET",
        "required_params": ["timestamp"],
        "optional_params": [],
        "signed": True,
        "description": "System maintenance status (signed).",
    },
    "account_info": {
        "path": "/api/v3/account",
        "method": "GET",
        "required_params": ["timestamp"],
        "optional_params": ["recvWindow"],
        "signed": True,
        "description": "Spot account balances/permissions (signed).",
    },
    "account_status": {
        "path": "/sapi/v3/accountStatus",
        "method": "GET",
        "required_params": ["timestamp"],
        "optional_params": [],
        "signed": True,
        "description": "Account status diagnostics (signed).",
    },
    "api_trading_status": {
        "path": "/sapi/v3/apiTradingStatus",
        "method": "GET",
        "required_params": ["timestamp"],
        "optional_params": ["recvWindow"],
        "signed": True,
        "description": "API risk/lock indicators (signed).",
    },
    "trade_fee": {
        "path": "/sapi/v1/asset/query/trading-fee",
        "method": "GET",
        "required_params": ["timestamp"],
        "optional_params": ["symbol"],
        "signed": True,
        "description": "Maker/taker fees (signed).",
    },
    "trade_volume_30d": {
        "path": "/sapi/v1/asset/query/trading-volume",
        "method": "GET",
        "required_params": ["timestamp"],
        "optional_params": [],
        "signed": True,
        "description": "Past 30-day trading volume (signed).",
    },
}

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

_MIN_REQUEST_DELAY_SEC = 0.2
_LAST_REQUEST_TS = 0.0
_EXCHANGE_INFO_CACHE: Dict[str, Any] = {"ts": 0.0, "symbols": set(), "by_base": {}}


def _sleep_if_needed() -> None:
    global _LAST_REQUEST_TS
    now = time.time()
    delta = now - _LAST_REQUEST_TS
    if delta < _MIN_REQUEST_DELAY_SEC:
        time.sleep(_MIN_REQUEST_DELAY_SEC - delta)
    _LAST_REQUEST_TS = time.time()


def _get(url: str, params: Optional[Dict[str, Any]] = None, retries: int = 3, backoff: float = 0.8) -> Tuple[Optional[requests.Response], Optional[str]]:
    proxies = None
    if BINANCE_PROXY:
        proxies = {"http": BINANCE_PROXY, "https": BINANCE_PROXY}

    last_error = None
    for attempt in range(retries):
        try:
            _sleep_if_needed()
            resp = requests.get(url, params=params, timeout=20, proxies=proxies)
            if resp.status_code == 200:
                return resp, None
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(backoff * (attempt + 1))
                continue
            return resp, None
        except requests.RequestException as exc:
            last_error = str(exc)
            time.sleep(backoff * (attempt + 1))
    return None, last_error


def normalize_symbol(symbol: str) -> str:
    sym = (symbol or "").strip().upper()
    sym = sym.replace("/", "").replace("-", "").replace("_", "")
    return sym


def get_endpoint_catalog(include_signed: bool = True) -> Dict[str, Dict[str, Any]]:
    if include_signed:
        return BINANCE_ENDPOINTS
    return {k: v for k, v in BINANCE_ENDPOINTS.items() if not bool(v.get("signed"))}


def _refresh_exchange_info_cache(force: bool = False, ttl_sec: int = 3600) -> None:
    now = time.time()
    if not force and _EXCHANGE_INFO_CACHE.get("symbols") and now - float(_EXCHANGE_INFO_CACHE.get("ts") or 0) < ttl_sec:
        return

    resp, err = _get(f"{BINANCE_BASE}/api/v3/exchangeInfo")
    if resp is None:
        if err:
            return
        return
    if resp.status_code != 200:
        return
    data = resp.json() if resp.content else {}
    symbols = set()
    by_base: Dict[str, List[str]] = {}
    for row in (data or {}).get("symbols") or []:
        try:
            if str(row.get("status") or "").upper() != "TRADING":
                continue
            sym = normalize_symbol(str(row.get("symbol") or ""))
            base = normalize_symbol(str(row.get("baseAsset") or ""))
            quote = normalize_symbol(str(row.get("quoteAsset") or ""))
            if not sym or not base or not quote:
                continue
            symbols.add(sym)
            by_base.setdefault(base, []).append(sym)
        except Exception:
            continue
    if symbols:
        _EXCHANGE_INFO_CACHE["ts"] = now
        _EXCHANGE_INFO_CACHE["symbols"] = symbols
        _EXCHANGE_INFO_CACHE["by_base"] = by_base


def get_all_trading_symbols(force_refresh: bool = False) -> List[str]:
    _refresh_exchange_info_cache(force=force_refresh)
    syms = _EXCHANGE_INFO_CACHE.get("symbols") or set()
    return sorted(list(syms))


def resolve_symbol(symbol_or_coin: str) -> Optional[str]:
    if not symbol_or_coin:
        return None
    key = (symbol_or_coin or "").strip().lower()
    if key in COIN_TO_SYMBOL:
        return COIN_TO_SYMBOL[key]

    _refresh_exchange_info_cache(force=False)
    symbols = _EXCHANGE_INFO_CACHE.get("symbols") or set()

    sym = normalize_symbol(symbol_or_coin)
    if sym in symbols:
        return sym

    # Try base+quote candidates for broad asset coverage.
    quote_candidates = ["USDT", "USDC", "BUSD", "USD", "BTC", "ETH", "BNB", "TRY", "EUR"]
    if sym.isalpha() and len(sym) <= 20:
        for q in quote_candidates:
            cand = f"{sym}{q}"
            if not symbols or cand in symbols:
                return cand

    # Fallback: find by base asset and prefer USDT when available.
    by_base = _EXCHANGE_INFO_CACHE.get("by_base") or {}
    if sym in by_base:
        options = by_base.get(sym) or []
        if not options:
            return None
        for cand in options:
            if cand.endswith("USDT"):
                return cand
        return options[0]

    return None


def call_binance_endpoint(endpoint_key: str, params: Optional[Dict[str, Any]] = None, api_key: str = "") -> Dict[str, Any]:
    spec = BINANCE_ENDPOINTS.get(endpoint_key)
    if not spec:
        return {"success": False, "error": f"unknown_endpoint_key:{endpoint_key}", "endpoint_key": endpoint_key}

    if bool(spec.get("signed")):
        return {
            "success": False,
            "error": "signed_endpoint_not_supported_in_public_mode",
            "endpoint_key": endpoint_key,
            "path": spec.get("path"),
            "requires_hmac": True,
        }

    params = dict(params or {})
    for req in spec.get("required_params") or []:
        if req not in params or params.get(req) in (None, ""):
            return {
                "success": False,
                "error": f"missing_required_param:{req}",
                "endpoint_key": endpoint_key,
                "path": spec.get("path"),
            }

    # Normalize symbol-like parameters automatically.
    for sym_key in ("symbol",):
        if sym_key in params and params.get(sym_key):
            rs = resolve_symbol(str(params.get(sym_key)))
            if rs:
                params[sym_key] = rs

    url = f"{BINANCE_BASE}{spec['path']}"
    resp, err = _get(url, params=params)
    if resp is None:
        return {
            "success": False,
            "error": f"binance_no_response:{err}",
            "endpoint_key": endpoint_key,
            "path": spec.get("path"),
            "params": params,
        }
    data: Any
    try:
        data = resp.json() if resp.content else {}
    except Exception:
        data = {"raw": resp.text[:5000]}
    return {
        "success": resp.status_code == 200,
        "status_code": resp.status_code,
        "endpoint_key": endpoint_key,
        "path": spec.get("path"),
        "params": params,
        "data": data,
    }


def _closest_point(points: List[Dict[str, Any]], target_ms: int) -> Optional[Tuple[int, float]]:
    best = None
    best_dist = None
    for p in points:
        ts = p.get("time")
        price = p.get("priceUsd")
        if ts is None or price is None:
            continue
        try:
            ts_i = int(ts)
            pr = float(price)
        except Exception:
            continue
        dist = abs(ts_i - target_ms)
        if best is None or dist < best_dist:
            best = (ts_i, pr)
            best_dist = dist
    return best


def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int, limit: int = 1000) -> List[List[Any]]:
    url = f"{BINANCE_BASE}/api/v3/klines"
    out: List[List[Any]] = []
    cur_start = int(start_ms)
    end_ms = int(end_ms)
    step_ms = INTERVAL_MS.get(interval, INTERVAL_MS["1h"])

    while cur_start <= end_ms and len(out) < 5000:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": cur_start,
            "endTime": end_ms,
            "limit": min(max(limit, 1), 1000),
        }
        resp, err = _get(url, params=params)
        if resp is None:
            raise RuntimeError(f"binance_no_response:{err}")
        if resp.status_code != 200:
            raise RuntimeError(f"binance_status_{resp.status_code}:{resp.text[:250]}")

        data = resp.json() or []
        if not isinstance(data, list) or not data:
            break
        out.extend(data)
        last_open = int(data[-1][0])
        next_start = last_open + step_ms
        if next_start <= cur_start:
            break
        cur_start = next_start
        if len(data) < params["limit"]:
            break

    # Dedup by open time
    uniq = {}
    for row in out:
        try:
            uniq[int(row[0])] = row
        except Exception:
            continue
    return [uniq[k] for k in sorted(uniq.keys())]


def fetch_price_on_date(symbol: str, date_obj: datetime, window_days: int = 1) -> Dict[str, Any]:
    target_dt = datetime(date_obj.year, date_obj.month, date_obj.day, 12, 0, tzinfo=timezone.utc)
    target_ms = int(target_dt.timestamp() * 1000)

    window_days = max(0, min(int(window_days), 30))
    if window_days == 0:
        start_ms = int(datetime(date_obj.year, date_obj.month, date_obj.day, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
        end_ms = int(datetime(date_obj.year, date_obj.month, date_obj.day, 23, 59, tzinfo=timezone.utc).timestamp() * 1000)
    else:
        start_ms = int((target_dt - timedelta(days=window_days)).timestamp() * 1000)
        end_ms = int((target_dt + timedelta(days=window_days)).timestamp() * 1000)

    interval = "1h" if window_days <= 14 else "1d"
    try:
        rows = fetch_klines(symbol, interval, start_ms, end_ms)
    except Exception as exc:
        return {"price": None, "raw": {"error": str(exc), "source": "binance_klines"}, "status_code": 0}

    points = []
    for r in rows:
        try:
            open_time = int(r[0])
            close_price = float(r[4])
            points.append({"time": open_time, "priceUsd": close_price, "date": datetime.fromtimestamp(open_time / 1000, tz=timezone.utc).isoformat()})
        except Exception:
            continue

    closest = _closest_point(points, target_ms)
    if closest is None:
        return {"price": None, "raw": {"error": "no_points", "source": "binance_klines"}, "status_code": 200}

    ts_ms, price = closest
    return {
        "price": price,
        "raw": {"chosen": [ts_ms, price], "data": points, "source": "binance_klines", "interval": interval, "symbol": symbol},
        "status_code": 200,
    }


def fetch_price_series_in_window(symbol_or_coin: str, tweet_date: str, rel_days: int, interval_hint: Optional[str] = None) -> Dict[str, Any]:
    symbol = resolve_symbol(symbol_or_coin)
    if not symbol:
        return {"success": False, "error": "symbol_not_resolved", "points": []}

    start_date = datetime.fromisoformat(tweet_date).date()
    end_date = start_date + timedelta(days=max(0, int(rel_days or 0)))
    start_dt = datetime(start_date.year, start_date.month, start_date.day, 0, 0, tzinfo=timezone.utc)
    end_dt = datetime(end_date.year, end_date.month, end_date.day, 23, 59, tzinfo=timezone.utc)

    if interval_hint:
        interval = interval_hint
    else:
        days = max(0, int(rel_days or 0))
        interval = "1h" if days <= 14 else ("4h" if days <= 60 else "1d")

    try:
        rows = fetch_klines(symbol, interval, int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000))
    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
            "symbol": symbol,
            "interval": interval,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "points": [],
        }

    points = []
    for r in rows:
        try:
            open_time = int(r[0])
            close_price = float(r[4])
            points.append({"time": open_time, "price_usd": close_price, "date": datetime.fromtimestamp(open_time / 1000, tz=timezone.utc).isoformat()})
        except Exception:
            continue

    return {
        "success": bool(points),
        "error": "" if points else "no_points",
        "symbol": symbol,
        "interval": interval,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "points": points,
    }


def fetch_quote_volume_window(symbol_or_coin: str, tweet_date: str, lookback_minutes: int = 15) -> Dict[str, Any]:
    """
    Proxy for short-term notional flow using quote asset volume from klines.
    Not equal to liquidations, but useful as observable public metric.
    """
    symbol = resolve_symbol(symbol_or_coin)
    if not symbol:
        return {"success": False, "error": "symbol_not_resolved", "candles": []}

    start_date = datetime.fromisoformat(tweet_date).date()
    start_dt = datetime(start_date.year, start_date.month, start_date.day, 0, 0, tzinfo=timezone.utc)
    end_dt = datetime(start_date.year, start_date.month, start_date.day, 23, 59, tzinfo=timezone.utc)

    if int(lookback_minutes or 15) <= 1:
        interval = "1m"
    elif int(lookback_minutes or 15) <= 5:
        interval = "5m"
    elif int(lookback_minutes or 15) <= 15:
        interval = "15m"
    else:
        interval = "1h"

    try:
        rows = fetch_klines(symbol, interval, int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000))
    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
            "symbol": symbol,
            "interval": interval,
            "candles": [],
        }

    candles = []
    for r in rows:
        try:
            open_time = int(r[0])
            close_price = float(r[4])
            quote_volume = float(r[7])
            candles.append(
                {
                    "time": open_time,
                    "date": datetime.fromtimestamp(open_time / 1000, tz=timezone.utc).isoformat(),
                    "close_price_usd": close_price,
                    "quote_volume_usd": quote_volume,
                }
            )
        except Exception:
            continue

    if not candles:
        return {
            "success": False,
            "error": "no_candles",
            "symbol": symbol,
            "interval": interval,
            "candles": [],
        }

    max_candle = max(candles, key=lambda c: float(c.get("quote_volume_usd") or 0.0))
    return {
        "success": True,
        "symbol": symbol,
        "interval": interval,
        "start_date": start_date.isoformat(),
        "end_date": start_date.isoformat(),
        "candles_count": len(candles),
        "max_quote_volume_usd": float(max_candle.get("quote_volume_usd") or 0.0),
        "max_quote_volume_date": max_candle.get("date"),
        "candles": candles,
    }


def fetch_quote_volume_multi_intervals(
    symbol_or_coin: str,
    tweet_date: str,
    intervals: Optional[List[str]] = None,
) -> Dict[str, Any]:
    symbol = resolve_symbol(symbol_or_coin)
    if not symbol:
        return {"success": False, "error": "symbol_not_resolved", "trials": []}

    start_date = datetime.fromisoformat(tweet_date).date()
    start_dt = datetime(start_date.year, start_date.month, start_date.day, 0, 0, tzinfo=timezone.utc)
    end_dt = datetime(start_date.year, start_date.month, start_date.day, 23, 59, tzinfo=timezone.utc)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    trial_intervals = intervals or ["1m", "5m", "15m", "30m", "1h"]
    trials: List[Dict[str, Any]] = []

    for interval in trial_intervals:
        try:
            rows = fetch_klines(symbol, interval, start_ms, end_ms)
        except Exception as exc:
            trials.append(
                {
                    "interval": interval,
                    "success": False,
                    "error": str(exc),
                    "candles_count": 0,
                    "max_quote_volume_usd": None,
                    "max_quote_volume_date": None,
                }
            )
            continue

        candles = []
        for r in rows:
            try:
                open_time = int(r[0])
                quote_volume = float(r[7])
                candles.append(
                    {
                        "time": open_time,
                        "date": datetime.fromtimestamp(open_time / 1000, tz=timezone.utc).isoformat(),
                        "quote_volume_usd": quote_volume,
                    }
                )
            except Exception:
                continue

        if not candles:
            trials.append(
                {
                    "interval": interval,
                    "success": False,
                    "error": "no_candles",
                    "candles_count": 0,
                    "max_quote_volume_usd": None,
                    "max_quote_volume_date": None,
                }
            )
            continue

        mx = max(candles, key=lambda x: float(x.get("quote_volume_usd") or 0.0))
        trials.append(
            {
                "interval": interval,
                "success": True,
                "error": "",
                "candles_count": len(candles),
                "max_quote_volume_usd": float(mx.get("quote_volume_usd") or 0.0),
                "max_quote_volume_date": mx.get("date"),
            }
        )

    return {
        "success": any(bool(t.get("success")) for t in trials),
        "symbol": symbol,
        "start_date": start_date.isoformat(),
        "end_date": start_date.isoformat(),
        "trials": trials,
    }


def resolve_target_date(item: Dict[str, Any]) -> datetime:
    if "target_date" in item and item["target_date"]:
        return datetime.fromisoformat(item["target_date"])
    if "tweet_date" in item and item["tweet_date"]:
        base = datetime.fromisoformat(item["tweet_date"])
        offset = int(item.get("relative_days", 0))
        return base + timedelta(days=offset)
    raise ValueError("No tweet_date or target_date in query")


def handle_single(item: Dict[str, Any], _api_key: str = "") -> Dict[str, Any]:
    coin_raw = item.get("coin") or item.get("symbol") or item.get("ticker")
    symbol = resolve_symbol(coin_raw)
    try:
        target_dt = resolve_target_date(item)
    except Exception as exc:
        return {"requested": item, "success": False, "error": str(exc)}

    if not symbol:
        return {"requested": item, "success": False, "error": "Trading symbol not resolved for Binance"}

    target_date_only = target_dt.date()
    rel_days = abs(int(item.get("relative_days", 0) or 0))
    if item.get("search_window_days") is not None:
        try:
            window_days = int(item.get("search_window_days"))
        except Exception:
            window_days = 1
    else:
        window_days = 7 if rel_days <= 7 else (2 if rel_days <= 30 else 1)

    result = fetch_price_on_date(symbol, datetime.combine(target_date_only, datetime.min.time()), window_days=window_days)
    out = {
        "requested": item,
        "asset_id": symbol,
        "target_date": target_date_only.isoformat(),
        "price_usd": result.get("price"),
        "raw_api": result.get("raw"),
        "status_code": result.get("status_code"),
        "success": result.get("price") is not None,
    }
    if not out["success"]:
        out["error"] = f"Failed to get price (status {out['status_code']})"
    return out


def process_input(data: Any, api_key: str = "") -> List[Dict[str, Any]]:
    queries: List[Dict[str, Any]] = []
    if isinstance(data, dict):
        if "queries" in data and isinstance(data["queries"], list):
            queries = data["queries"]
        else:
            queries = [data]
    elif isinstance(data, list):
        queries = data
    else:
        raise ValueError("Unsupported input JSON structure")

    results: List[Dict[str, Any]] = []
    for q in queries:
        try:
            results.append(handle_single(q, api_key))
        except Exception as exc:
            results.append({"requested": q, "success": False, "error": str(exc)})
    return results


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Query Binance historical prices by JSON input")
    p.add_argument("--input", "-i", required=True)
    p.add_argument("--output", "-o", required=True)
    args = p.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        payload = json.load(f)
    res = process_input(payload, "")
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
