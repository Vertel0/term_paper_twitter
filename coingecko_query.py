"""
coingecko_query.py

Usage:
    python coingecko_query.py --input input.json --output output.json

Input JSON formats supported (single object or list):

1) Simple historical relative query:
{
  "coin": "btc",                # coin symbol or CoinCap slug
  "tweet_date": "2023-01-01",  # I
  SO date (YYYY-MM-DD)
  "relative_days": 5            # integer days to add (can be negative)
}

2) Batch queries:
{
  "queries": [ <objects like above> ]
}

Output: JSON list of result objects with fields: requested, asset_id, target_date, price_usd, raw_api, success, error

Note: This script uses CoinCap API v3 with Bearer auth.
"""

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

COINCAP_BASE = os.getenv("COINCAP_BASE", "https://rest.coincap.io/v3")
COINCAP_PROXY = os.getenv("COINCAP_PROXY", "").strip()
COINCAP_FORCE_HISTORY = os.getenv("COINCAP_FORCE_HISTORY", "").strip().lower() in {"1", "true", "yes"}

# Basic mapping from common ticker symbols to CoinCap slugs
SYMBOL_TO_SLUG = {
    "btc": "bitcoin",
    "bitcoin": "bitcoin",
    "eth": "ethereum",
    "ethereum": "ethereum",
    "bnb": "binance-coin",
    "sol": "solana",
    "ada": "cardano",
    "xrp": "xrp",
}

MIN_REQUEST_DELAY_SEC = 0.6
_LAST_REQUEST_TS = 0.0


def _sleep_if_needed():
    global _LAST_REQUEST_TS
    now = time.time()
    delta = now - _LAST_REQUEST_TS
    if delta < MIN_REQUEST_DELAY_SEC:
        time.sleep(MIN_REQUEST_DELAY_SEC - delta)
    _LAST_REQUEST_TS = time.time()


def _auth_header(api_key: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _get(url: str, api_key: str, params: Optional[Dict[str, Any]] = None, retries: int = 3, backoff: float = 1.2) -> Tuple[Optional[requests.Response], Optional[str]]:
    last_error = None
    proxies = None
    if COINCAP_PROXY:
        proxies = {"http": COINCAP_PROXY, "https": COINCAP_PROXY}
    for attempt in range(retries):
        try:
            _sleep_if_needed()
            resp = requests.get(url, params=params, timeout=20, headers=_auth_header(api_key), proxies=proxies)
            if resp.status_code == 200:
                return resp, None
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(backoff * (attempt + 1))
                continue
            return resp, None
        except requests.RequestException as e:
            last_error = str(e)
            time.sleep(backoff * (attempt + 1))
            continue
    return None, last_error


def normalize_symbol(symbol: str) -> str:
    return (symbol or "").strip().lower()


def resolve_asset_slug(symbol_or_slug: str, api_key: str) -> Optional[str]:
    if not symbol_or_slug:
        return None
    key = normalize_symbol(symbol_or_slug)
    if key in SYMBOL_TO_SLUG:
        return SYMBOL_TO_SLUG[key]

    # Try direct slug
    direct, _err = _get(f"{COINCAP_BASE}/assets/{key}", api_key)
    if direct is not None and direct.status_code == 200:
        return key

    # Search by symbol or name
    resp, _err = _get(f"{COINCAP_BASE}/assets", api_key, params={"search": symbol_or_slug, "limit": 5, "offset": 0})
    if resp is None or resp.status_code != 200:
        return None
    data = (resp.json() or {}).get("data") or []
    if not data:
        return None

    # Prefer exact symbol match
    for item in data:
        if normalize_symbol(item.get("symbol")) == key:
            return item.get("id")
    return data[0].get("id")


def _closest_point(points: List[Dict[str, Any]], target_ms: int) -> Optional[Tuple[int, float]]:
    if not points:
        return None
    best = None
    best_dist = None
    for p in points:
        ts = p.get("time")
        price = p.get("priceUsd")
        if ts is None or price is None:
            continue
        try:
            price_val = float(price)
        except (TypeError, ValueError):
            continue
        dist = abs(int(ts) - target_ms)
        if best is None or dist < best_dist:
            best = (int(ts), price_val)
            best_dist = dist
    return best


def _range_bounds(points: List[Dict[str, Any]]) -> Optional[Tuple[int, int]]:
    times = [int(p.get("time")) for p in points if p.get("time") is not None]
    if not times:
        return None
    return min(times), max(times)


def _parse_agentfriendly_history(csv_text: str) -> List[Dict[str, Any]]:
    points: List[Dict[str, Any]] = []
    if not csv_text:
        return points
    lines = [ln.strip() for ln in csv_text.strip().splitlines() if ln.strip()]
    for ln in lines:
        if "," not in ln:
            continue
        ts_s, price_s = ln.split(",", 1)
        try:
            ts = int(float(ts_s))
            price = float(price_s)
        except ValueError:
            continue
        points.append({"time": ts, "priceUsd": price})
    return points


def fetch_price_on_date_agentfriendly(coin_slug: str, date_obj: datetime, api_key: str) -> Dict[str, Any]:
    target_dt = datetime(date_obj.year, date_obj.month, date_obj.day, 12, 0, tzinfo=timezone.utc)
    target_ms = int(target_dt.timestamp() * 1000)
    start_ms = int(datetime(date_obj.year, date_obj.month, date_obj.day, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(datetime(date_obj.year, date_obj.month, date_obj.day, 23, 59, tzinfo=timezone.utc).timestamp() * 1000)

    resp, err = _get(
        f"{COINCAP_BASE}/agentFriendly/history/{coin_slug}",
        api_key,
        params={"start": start_ms, "end": end_ms},
    )
    if resp is None:
        return {
            "price": None,
            "raw": {"error": "no_response", "detail": err, "requested_date": date_obj.date().isoformat(), "source": "agentFriendly"},
            "status_code": None,
        }
    if resp.status_code != 200:
        return {"price": None, "raw": {"error": f"status_{resp.status_code}", "body": resp.text[:500], "source": "agentFriendly"}, "status_code": resp.status_code}

    payload = resp.json() or {}
    data = (payload.get("data") or {})
    csv_text = data.get("history") or ""
    points = _parse_agentfriendly_history(csv_text)
    closest = _closest_point(points, target_ms)
    if closest is None:
        return {"price": None, "raw": {"error": "no_points", "data": data, "source": "agentFriendly"}, "status_code": resp.status_code}

    ts_ms, price = closest
    return {"price": price, "raw": {"chosen": [ts_ms, price], "data": data, "source": "agentFriendly"}, "status_code": resp.status_code}


def fetch_price_on_date(coin_slug: str, date_obj: datetime, api_key: str, window_days: int = 1) -> Dict[str, Any]:
    target_dt = datetime(date_obj.year, date_obj.month, date_obj.day, 12, 0, tzinfo=timezone.utc)
    target_ms = int(target_dt.timestamp() * 1000)
    window_days = max(0, min(int(window_days), 14))
    if window_days == 0:
        # Point-in-time mode: still provide a valid non-zero interval for CoinCap.
        start_ms = int((target_dt - timedelta(hours=12)).timestamp() * 1000)
        end_ms = int((target_dt + timedelta(hours=12)).timestamp() * 1000)
    else:
        start_ms = int((target_dt - timedelta(days=window_days)).timestamp() * 1000)
        end_ms = int((target_dt + timedelta(days=window_days)).timestamp() * 1000)
    if end_ms <= start_ms:
        end_ms = start_ms + 60_000

    resp, err = _get(
        f"{COINCAP_BASE}/assets/{coin_slug}/history",
        api_key,
        params={"interval": "d1", "start": start_ms, "end": end_ms},
    )
    if resp is not None and resp.status_code == 400 and "start must be less than end" in (resp.text or ""):
        # Defensive retry with wider bounds.
        start_ms = int((target_dt - timedelta(days=1)).timestamp() * 1000)
        end_ms = int((target_dt + timedelta(days=1)).timestamp() * 1000)
        resp, err = _get(
            f"{COINCAP_BASE}/assets/{coin_slug}/history",
            api_key,
            params={"interval": "d1", "start": start_ms, "end": end_ms},
        )
    if resp is None:
        return {"price": None, "raw": {"error": "no_response", "detail": err, "source": "history_ms"}, "status_code": None}
    if resp.status_code == 404:
        # Some deployments expect seconds instead of milliseconds
        start_s = int(start_ms / 1000)
        end_s = int(end_ms / 1000)
        resp, err = _get(
            f"{COINCAP_BASE}/assets/{coin_slug}/history",
            api_key,
            params={"interval": "d1", "start": start_s, "end": end_s},
        )
        if resp is None:
            return {"price": None, "raw": {"error": "no_response", "detail": err, "source": "history_s"}, "status_code": None}
        if resp.status_code == 404:
            # Try without range (returns recent history)
            resp, err = _get(
                f"{COINCAP_BASE}/assets/{coin_slug}/history",
                api_key,
                params={"interval": "d1"},
            )
            if resp is None:
                return {
                    "price": None,
                    "raw": {"error": "no_response", "detail": err, "requested_date": date_obj.date().isoformat(), "source": "history_none"},
                    "status_code": None,
                }
            if resp.status_code == 404:
                if COINCAP_FORCE_HISTORY:
                    return {"price": None, "raw": {"error": "history_not_found", "requested_date": date_obj.date().isoformat(), "source": "history_none"}, "status_code": 404}
                return fetch_price_on_date_agentfriendly(coin_slug, date_obj, api_key)
    if resp.status_code != 200:
        return {
            "price": None,
            "raw": {"error": f"status_{resp.status_code}", "body": resp.text[:500], "requested_date": date_obj.date().isoformat(), "source": "history"},
            "status_code": resp.status_code,
        }

    data = (resp.json() or {}).get("data") or []
    if not data:
        if COINCAP_FORCE_HISTORY:
            return {"price": None, "raw": {"error": "no_points", "requested_date": date_obj.date().isoformat(), "source": "history"}, "status_code": resp.status_code}
        return fetch_price_on_date_agentfriendly(coin_slug, date_obj, api_key)
    bounds = _range_bounds(data)
    if bounds:
        min_ts, max_ts = bounds
        tolerance_ms = 36 * 3600 * 1000
        if target_ms < min_ts - tolerance_ms or target_ms > max_ts + tolerance_ms:
            return {
                "price": None,
                "raw": {
                    "error": "target_out_of_range",
                    "range": [min_ts, max_ts],
                    "data_points": len(data),
                    "requested_date": date_obj.date().isoformat(),
                    "hint": "Historical data for this date is not available in the current CoinCap response. You may need a higher tier or a different provider for older dates.",
                    "source": "history",
                },
                "status_code": resp.status_code,
            }
    closest = _closest_point(data, target_ms)
    if closest is None:
        if COINCAP_FORCE_HISTORY:
            return {"price": None, "raw": {"error": "no_closest", "requested_date": date_obj.date().isoformat(), "source": "history"}, "status_code": resp.status_code}
        return fetch_price_on_date_agentfriendly(coin_slug, date_obj, api_key)

    ts_ms, price = closest
    return {"price": price, "raw": {"chosen": [ts_ms, price], "data": data, "source": "history"}, "status_code": resp.status_code}


def resolve_target_date(item: Dict[str, Any]) -> datetime:
    # item may include tweet_date and relative_days or direct target_date
    if "target_date" in item and item["target_date"]:
        return datetime.fromisoformat(item["target_date"])  # expect YYYY-MM-DD
    if "tweet_date" in item and item["tweet_date"]:
        base = datetime.fromisoformat(item["tweet_date"])  # date-only ok
        offset = int(item.get("relative_days", 0))
        return base + timedelta(days=offset)
    raise ValueError("No tweet_date or target_date in query")


def handle_single(item: Dict[str, Any], api_key: str) -> Dict[str, Any]:
    coin_raw = item.get("coin") or item.get("symbol") or item.get("ticker")
    coin_id = resolve_asset_slug(coin_raw, api_key)
    try:
        target_dt = resolve_target_date(item)
    except Exception as e:
        return {"requested": item, "success": False, "error": str(e)}

    if not coin_id:
        return {"requested": item, "success": False, "error": "Coin id not resolved"}

    # CoinGecko historic endpoint only supports up to day resolution
    # If time portion exists, we ignore time and use date
    target_date_only = target_dt.date()
    rel_days = abs(int(item.get("relative_days", 0) or 0))
    if item.get("search_window_days") is not None:
        try:
            window_days = int(item.get("search_window_days"))
        except Exception:
            window_days = 1
    else:
        # Короткий горизонт: допускаем широкий поиск по окну,
        # долгий горизонт: целимся в конкретный день.
        if rel_days <= 7:
            window_days = 7
        elif rel_days <= 30:
            window_days = 2
        else:
            window_days = 1

    result = fetch_price_on_date(
        coin_id,
        datetime.combine(target_date_only, datetime.min.time()),
        api_key,
        window_days=window_days,
    )
    out = {
        "requested": item,
        "asset_id": coin_id,
        "target_date": target_date_only.isoformat(),
        "price_usd": result.get("price"),
        "raw_api": result.get("raw"),
        "status_code": result.get("status_code"),
        "success": result.get("price") is not None,
    }
    if not out["success"]:
        out["error"] = f"Failed to get price (status {out['status_code']})"
    return out


def process_input(data: Any, api_key: str) -> List[Dict[str, Any]]:
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

    results = []
    for q in queries:
        try:
            results.append(handle_single(q, api_key))
        except Exception as exc:
            results.append({"requested": q, "success": False, "error": str(exc)})
    return results


def main():
    parser = argparse.ArgumentParser(description="Query CoinGecko for prices based on JSON queries")
    parser.add_argument("--input", "-i", required=True, help="Input JSON file")
    parser.add_argument("--output", "-o", required=True, help="Output JSON file")
    parser.add_argument("--api-key", "-k", default=os.getenv("COINCAP_API_KEY", ""), help="CoinCap API key (or set COINCAP_API_KEY)")
    args = parser.parse_args()

    if not args.api_key:
        raise SystemExit("CoinCap API key is required. Use --api-key or set COINCAP_API_KEY")

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    results = process_input(data, args.api_key)

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)

    print(f"Wrote {len(results)} results to {args.output}")


if __name__ == "__main__":
    main()
