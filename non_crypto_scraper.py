#!/usr/bin/env python
# -*- coding: utf-8 -*-
import csv
import html
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from itertools import cycle
from typing import Dict, List, Set, Tuple

from dateutil import parser
import requests

# Output paths
NON_CRYPTO_CSV = os.getenv(
    "NON_CRYPTO_CSV",
    r"c:\Users\Professional\Desktop\kursach\non_crypto_tweets.csv",
)
CRYPTO_CSV = os.getenv(
    "CRYPTO_CSV",
    r"c:\Users\Professional\Desktop\kursach\crypto_twitter_dataset2.csv",
)
TRASHED_CRYPTO_CSV = os.getenv(
    "TRASHED_CRYPTO_CSV",
    r"c:\Users\Professional\Desktop\kursach\trashed_crypto_dataset.csv",
)
COMBINED_CRYPTO_CSV = os.getenv(
    "COMBINED_CRYPTO_CSV",
    r"c:\Users\Professional\Desktop\kursach\crypto_dataset_combined.csv",
)
COOKIES_FILE = os.getenv("TW_COOKIE_FILE", "cookies.json")
QIDS_FILE = os.getenv("TW_QIDS_FILE", "query_ids.json")

# QueryId for HomeTimeline (may change; override via env TW_QID_HOME)
DEFAULT_HOME_QID = os.getenv("TW_QID_HOME", "qIWNRQfRx-Rq2ybMont8rQ")

# Authorization bearer
DEFAULT_AUTHORIZATION = (
    "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# Proxy rotation (http/https)
PROXIES = [
    # Replaced with current working proxies (format: http://user:pass@host:port)
    "http://WU0Xh3:TggaxK@190.185.109.9:9370",
    "http://WU0Xh3:TggaxK@161.115.231.94:9116",
]

# Feature flags (reuse from working twitter_scraper)
FEATURES = {
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

# ������� �����/������� ��� ������-������� (�����������)
CRYPTO_KEYWORDS = [
    # Layer1 / Majors
    "bitcoin", "btc", "ethereum", "eth", "binance", "bnb", "solana", "sol", "cardano", "ada",
    "xrp", "polkadot", "dot", "dogecoin", "doge", "shiba", "shib", "avalanche", "avax", "litecoin", "ltc",
    "tron", "trx", "near", "aptos", "sui", "ton", "algo", "algorand", "xlm", "stellar", "icp",
    # Oracles / Infra
    "chainlink", "api3", "band", "pyth",
    # DeFi blue chips / dex
    "uniswap", "uni", "aave", "crv", "curve", "gmx", "balancer", "sushi", "pancake", "pancakeswap",
    "1inch", "paraswap", "dydx", "perp", "perpetual", "synthetix", "snx", "maker", "mkr", "compound", "comp",
    # L2 / rollups
    "layer 2", "l2", "arbitrum", "optimism", "op", "zksync", "starknet", "scroll", "linea", "base", "blast",
    # Bridges / infra terms
    "bridge", "rollup", "zk", "zk rollup", "sequencer",
    # DeFi/NFT/web3 commons
    "defi", "nft", "web3", "blockchain", "metaverse", "gamefi", "play to earn", "p2e", "airdrop", "airdrop hunters",
    "restake", "restaking", "yield", "yield farming", "lp", "liquidity", "staking", "points", "retrodrop",
    # Stablecoins / payments
    "usdt", "tether", "usdc", "dai", "busd", "usde", "fdusd",
    # Wallets / explorers
    "metamask", "phantom", "ledger", "trezor", "walletconnect", "etherscan", "bscscan", "arbiscan", "snowtrace",
    # CEX/DEX brands
    "coinbase", "kraken", "bybit", "okx", "huobi", "mexc", "gate", "kucoin",
    # Memes / trends
    "pepe", "floki", "bonk", "sats", "ordinals",
]

CRYPTO_HASHTAGS = [
    "btc", "eth", "bnb", "sol", "ada", "xrp", "dot", "doge", "shib", "ltc", "trx", "avax",
    "matic", "op", "arb", "base", "zk", "zksync", "starknet", "blast", "linea", "scroll",
    "defi", "nft", "web3", "crypto", "airdrop", "restaking", "points",
]

BRAND_KEYWORDS: Set[str] = {
    "binance", "coinbase", "kraken", "bybit", "okx", "huobi", "bitfinex", "mexc", "gate", "kucoin",
    "pancakeswap", "uniswap", "sushiswap", "curve", "aave", "dydx", "opensea", "blur", "looksrare",
    "metamask", "phantom", "ledger", "trezor", "walletconnect", "arbitrum", "optimism", "zksync", "starknet",
}

EXTENDED_KEYWORDS: Set[str] = {
    "airdrop", "retrodrop", "points", "season", "quest", "galxe", "layer3", "merit", "xp",
    "ido", "ieo", "ico", "launchpad", "token", "presale", "seed", "vesting", "tokenomics",
    "yield", "farming", "yield farming", "liquidity", "lp", "staking", "restake", "restaking",
    "bridge", "l2", "layer 2", "rollup", "zk", "zk rollup", "sequencer",
    "testnet", "mainnet", "devnet", "faucet", "claim", "mint", "minting", "burn", "burned",
    "gas", "wallet", "contract", "smart", "solidity", "audit", "kyc", "airdrop claim",
    "wagmi", "hodl", "degen", "drop",
}

URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)
CASH_TAG_PATTERN = re.compile(r"\$[A-Za-z]{2,10}\b")
PRICE_PATTERN = re.compile(r"\b([A-Z]{2,6})\b\s*[:\-]?\s*\$?\d{1,3}(?:[,\.]\d{3})*(?:\.\d+)?")


def detect_crypto_signals(text: str, description: str = "") -> Tuple[bool, List[str], Set[str]]:
    """��������� (is_crypto, hits, token_hits) ��� ���������� ���������� � �������."""
    # Only inspect tweet text to avoid description-induced false positives.
    combined = f"{text or ''}".strip()
    if not combined:
        return False, [], set()

    low = combined.lower()
    tokens = set(re.findall(r"\b[a-zA-Z][\w\-]{1,}\b", low))

    token_hits = tokens.intersection(set(CRYPTO_KEYWORDS) | BRAND_KEYWORDS | EXTENDED_KEYWORDS)
    hashtag_hits = {h.lower() for h in re.findall(r"#(\w+)", combined)}.intersection(set(CRYPTO_HASHTAGS))
    cashtags_found = CASH_TAG_PATTERN.findall(combined)

    strong_domains = {
        "binance.com", "binance.us", "coinbase.com", "bybit.com", "kraken.com", "okx.com",
        "huobi.com", "gate.io", "kucoin.com", "mexc.com", "uniswap.org", "pancakeswap.finance",
        "dydx.exchange", "etherscan.io", "bscscan.com", "arbiscan.io", "snowtrace.io",
    }
    urls = URL_PATTERN.findall(combined)
    matched_domains = [d for d in strong_domains if any(d in url.lower() for url in urls)]

    strong_keywords = {
        "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "cardano", "ada", "xrp",
        "dogecoin", "doge", "shiba", "shib", "avalanche", "avax", "polkadot", "dot", "chainlink",
        "arbitrum", "optimism", "op", "zksync", "starknet", "linea", "base", "blast",
        "uniswap", "uni", "aave", "gmx", "sushi", "pancakeswap", "pancake", "dydx", "maker", "mkr",
        "compound", "comp", "synthetix", "snx", "balancer", "binance", "coinbase", "kraken", "bybit",
        "okx", "huobi", "mexc", "gate", "kucoin", "defi", "nft", "web3", "airdrop", "staking",
        "restake", "restaking", "yield", "yield farming", "metamask", "phantom", "ledger", "trezor",
        "walletconnect", "etherscan", "bscscan", "arbiscan", "snowtrace",
    }
    strong_hits = tokens.intersection(strong_keywords)
    price_hit = bool(PRICE_PATTERN.search(combined))

    is_crypto = False
    if cashtags_found or hashtag_hits or matched_domains:
        is_crypto = True
    elif strong_hits:
        is_crypto = True
    elif len(token_hits) >= 2:
        is_crypto = True
    elif price_hit and token_hits:
        is_crypto = True

    hits: List[str] = []
    if cashtags_found:
        hits.append(cashtags_found[0])
    if hashtag_hits:
        hits.append("#" + sorted(hashtag_hits)[0])
    if matched_domains:
        hits.append(f"domain:{matched_domains[0]}")
    if strong_hits:
        hits.append(sorted(strong_hits)[0])
    elif token_hits:
        hits.append(sorted(token_hits)[0])
    if price_hit:
        hits.append("price")

    return is_crypto, hits, token_hits


def is_crypto_related(text: str, description: str = "") -> bool:
    return detect_crypto_signals(text, description)[0]


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def calculate_account_age(created_at_str: str) -> int:
    """��������� ������� �������� � ���� (��� 1, ���� ~20 ���)."""
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


def get_all_fieldnames() -> List[str]:
    """����� ����� ����� ��� ������-CSV (��������� � crypto_twitter_dataset2.csv)."""
    return [
        "followers_count",
        "friends_count",
        "followers_friends_ratio",
        "statuses_count",
        "favourites_count",
        "listed_count",
        "media_count",
        "account_age_days",
        "tweets_per_day",
        "has_custom_timelines",
        "verified",
        "protected",
        "has_professional",
        "description_length",
        "tweet_length",
        "has_hashtags",
        "has_mentions",
        "has_urls",
        "has_media",
        "is_retweet",
        "is_reply",
        "language",
        "engagement_rate",
        "reply_ratio",
        "retweet_ratio",
        "tweet_likes",
        "tweet_retweets",
        "tweet_replies",
        "is_media_like",
        "is_influencer_like",
        "is_personal_like",
        "is_bot_like",
        "has_profile_banner",
        "location_present",
        "is_crypto_related",
        "crypto_keywords_count",
        "found_keywords",
        "username",
        "user_display_name",
        "tweet_text",
        "tweet_id",
    ]


def extract_features_from_user(user_data: dict, tweet_legacy: dict, tweet_result: dict) -> Dict[str, object]:
    user_legacy = user_data.get("legacy", {})
    user_core = user_data.get("core", {})

    tweet_id = tweet_result.get("rest_id") or tweet_legacy.get("id_str")
    followers_count = user_legacy.get("followers_count", 0)
    friends_count = max(user_legacy.get("friends_count", 1), 1)
    statuses_count = user_legacy.get("statuses_count", 0)
    favourites_count = user_legacy.get("favourites_count", 0)
    listed_count = user_legacy.get("listed_count", 0)
    media_count = user_legacy.get("media_count", 0)

    account_created_at = user_core.get("created_at", user_legacy.get("created_at", ""))
    account_age_days = calculate_account_age(account_created_at)

    tweets_per_day = safe_float(statuses_count) / max(account_age_days, 1)
    followers_friends_ratio = safe_float(followers_count) / max(safe_float(friends_count), 1)
    followers_friends_ratio = min(followers_friends_ratio, 10000)
    tweets_per_day = min(tweets_per_day, 1000)

    tweet_entities = tweet_legacy.get("entities", {})
    favorite_count = tweet_legacy.get("favorite_count", 0)
    retweet_count = tweet_legacy.get("retweet_count", 0)
    reply_count = tweet_legacy.get("reply_count", 0)
    engagement_rate = safe_float(favorite_count + retweet_count) / max(safe_float(followers_count), 1)

    tweet_text = clean_text(tweet_legacy.get("full_text", ""))
    user_description = clean_text(user_legacy.get("description", ""))
    is_crypto, hits, token_hits = detect_crypto_signals(tweet_text, user_description)

    found_keywords = list(hits)
    # �������� ����� �������� ���������, ����� features ������� �������� �����
    if not found_keywords and token_hits:
        found_keywords = sorted(token_hits)

    features = {
        "followers_count": followers_count,
        "friends_count": friends_count,
        "followers_friends_ratio": round(followers_friends_ratio, 2),
        "statuses_count": statuses_count,
        "favourites_count": favourites_count,
        "listed_count": listed_count,
        "media_count": media_count,
        "account_age_days": account_age_days,
        "tweets_per_day": round(tweets_per_day, 2),
        "has_custom_timelines": int(user_legacy.get("has_custom_timelines", False)),
        "verified": int(user_data.get("is_blue_verified", False)),
        "protected": int(user_legacy.get("protected", False)),
        "has_professional": int("professional" in user_data),
        "description_length": len(user_description),
        "tweet_length": len(tweet_text),
        "has_hashtags": int("hashtags" in tweet_entities),
        "has_mentions": int("user_mentions" in tweet_entities),
        "has_urls": int("urls" in tweet_entities),
        "has_media": int("media" in tweet_entities),
        "is_retweet": int("retweeted_status_result" in tweet_legacy),
        "is_reply": int(tweet_legacy.get("in_reply_to_user_id") is not None),
        "language": tweet_legacy.get("lang", "unknown"),
        "engagement_rate": round(engagement_rate, 4),
        "reply_ratio": round(safe_float(reply_count) / max(statuses_count, 1), 4),
        "retweet_ratio": round(safe_float(retweet_count) / max(statuses_count, 1), 4),
        "tweet_likes": favorite_count,
        "tweet_retweets": retweet_count,
        "tweet_replies": reply_count,
        "is_media_like": int(media_count > statuses_count * 0.3),
        "is_influencer_like": int(followers_count > 10000 and followers_friends_ratio > 10),
        "is_personal_like": int(followers_count < 5000 and statuses_count > 1000),
        "is_bot_like": int(tweets_per_day > 50),
        "has_profile_banner": int("profile_banner_url" in user_legacy),
        "location_present": int(bool(user_data.get("location", {}).get("location"))),
        "is_crypto_related": int(is_crypto),
        "crypto_keywords_count": len(found_keywords),
        "found_keywords": ", ".join(found_keywords[:5]),
        "username": user_core.get("screen_name", "Unknown"),
        "user_display_name": user_core.get("name", "Unknown"),
        "tweet_text": tweet_text,
        "tweet_id": tweet_id,
    }
    return features


def read_existing_ids(path: str) -> Set[str]:
    ids: Set[str] = set()
    if not os.path.isfile(path):
        return ids
    try:
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter=";")
            for row in reader:
                tid = row.get("tweet_id")
                if tid:
                    ids.add(tid)
    except Exception:
        pass
    return ids


def save_non_crypto_rows(path: str, rows: List[Dict[str, str]], existing_ids: Set[str]) -> int:
    if not rows:
        return 0
    file_exists = os.path.isfile(path)
    fieldnames = [
        "tweet_id",
        "created_at",
        "full_text",
        "favorite_count",
        "retweet_count",
        "reply_count",
        "lang",
    ]
    mode = "a" if file_exists else "w"
    attempts = 0
    written = 0
    while True:
        try:
            with open(path, mode, newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
                if not file_exists:
                    writer.writeheader()
                for row in rows:
                    tid = row.get("tweet_id")
                    if tid and tid not in existing_ids:
                        writer.writerow(row)
                        existing_ids.add(tid)
                        written += 1
            break
        except PermissionError:
            attempts += 1
            wait = 10
            print(f"Permission denied for {path}. Retry {attempts} in {wait}s (close file if open)...")
            time.sleep(wait)
    return written


def save_crypto_rows(path: str, rows: List[Dict[str, object]], existing_ids: Set[str]) -> int:
    if not rows:
        return 0
    file_exists = os.path.isfile(path)
    fieldnames = get_all_fieldnames()
    mode = "a" if file_exists else "w"
    attempts = 0
    written = 0
    while True:
        try:
            with open(path, mode, newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
                if not file_exists:
                    writer.writeheader()
                for row in rows:
                    tid = row.get("tweet_id")
                    if tid and tid not in existing_ids:
                        writer.writerow(row)
                        existing_ids.add(tid)
                        written += 1
            break
        except PermissionError:
            attempts += 1
            wait = 10
            print(f"Permission denied for {path}. Retry {attempts} in {wait}s (close file if open)...")
            time.sleep(wait)
    return written


def load_home_qid() -> str:
    if os.path.isfile(QIDS_FILE):
        try:
            with open(QIDS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if "home_timeline" in data:
                return data["home_timeline"]
        except Exception:
            pass
    return DEFAULT_HOME_QID


def load_cookies() -> Dict[str, str]:
    if not os.path.isfile(COOKIES_FILE):
        raise FileNotFoundError(f"cookies file not found: {COOKIES_FILE}")
    with open(COOKIES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    cookies: Dict[str, str] = {}
    for item in data:
        name = item.get("name")
        if name in ("auth_token", "ct0"):
            cookies[name] = item.get("value")
    if "auth_token" not in cookies or "ct0" not in cookies:
        raise RuntimeError("auth_token or ct0 missing in cookies")
    return cookies


def build_session(cookies: Dict[str, str]) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "authorization": os.getenv("AUTHORIZATION", DEFAULT_AUTHORIZATION),
            "x-csrf-token": cookies["ct0"],
            "x-twitter-auth-type": "OAuth2Session",
            "x-twitter-active-user": "yes",
            "content-type": "application/json",
            "user-agent": "Mozilla/5.0",
        }
    )
    session.cookies.update({"auth_token": cookies["auth_token"], "ct0": cookies["ct0"]})
    return session


class ProxyRotator:
    def __init__(self, proxies: List[str]):
        if not proxies:
            raise ValueError("Proxy list is empty")
        self._cycle = cycle(proxies)

    def next(self) -> Dict[str, str]:
        p = next(self._cycle)
        return {"http": p, "https": p}


def fetch_home(session: requests.Session, qid: str, proxy: Dict[str, str], cursor: str | None = None) -> dict:
    variables = {
        "count": 50,
        "includePromotedContent": True,
        "latestControlAvailable": True,
        "requestContext": "launch",
        "withCommunity": True,
    }
    if cursor:
        variables["cursor"] = cursor
    params = {
        "variables": json.dumps(variables, separators=(",", ":")),
        "features": json.dumps(FEATURES, separators=(",", ":")),
    }
    url = f"https://x.com/i/api/graphql/{qid}/HomeTimeline"
    resp = session.get(url, params=params, timeout=20, proxies=proxy)
    if not resp.ok:
        print(f"HomeTimeline {resp.status_code}: {resp.text[:300]} ...")
    resp.raise_for_status()
    return resp.json()


def extract_cursor(data: dict) -> str | None:
    try:
        instructions = data["data"]["home"]["home_timeline_urt"]["instructions"]
        for instr in instructions:
            if instr.get("type") == "TimelineAddEntries":
                for entry in instr.get("entries", []):
                    if entry.get("entryId", "").startswith("cursor-bottom-"):
                        return entry["content"]["value"]
    except Exception:
        return None
    return None


def split_crypto_and_non(data: dict) -> tuple[list[dict], list[dict]]:
    """��������� (crypto_rows, non_crypto_rows) ��� ������� ��������."""
    crypto_rows: list[dict] = []
    non_rows: list[dict] = []
    try:
        instructions = data["data"]["home"]["home_timeline_urt"]["instructions"]
        for instr in instructions:
            if instr.get("type") != "TimelineAddEntries":
                continue
            for entry in instr.get("entries", []):
                if not entry.get("entryId", "").startswith("tweet-"):
                    continue
                try:
                    content = entry["content"]["itemContent"]["tweet_results"]["result"]
                    legacy = content.get("legacy", {})
                    user_data = content.get("core", {}).get("user_results", {}).get("result", {})
                    tid = legacy.get("id_str")
                    text = clean_text(legacy.get("full_text", ""))
                    if not tid or not text:
                        continue
                    description = clean_text(user_data.get("legacy", {}).get("description", ""))

                    is_crypto, hits, token_hits = detect_crypto_signals(text, description)
                    if is_crypto:
                        print(f"[CRYPTO] {tid} hit={hits[0] if hits else 'n/a'} text={text[:120]}")
                        features = extract_features_from_user(user_data, legacy, content)
                        if features.get("tweet_id"):
                            crypto_rows.append(features)
                    elif not token_hits:
                        non_rows.append(
                            {
                                "tweet_id": tid,
                                "created_at": legacy.get("created_at"),
                                "full_text": text,
                                "favorite_count": legacy.get("favorite_count", 0),
                                "retweet_count": legacy.get("retweet_count", 0),
                                "reply_count": legacy.get("reply_count", 0),
                                "lang": legacy.get("lang", ""),
                            }
                        )
                    else:
                        print(f"[SKIP NON] {tid} single_crypto_word={sorted(token_hits)[0]} text={text[:120]}")
                except Exception as exc:  # noqa: BLE001
                    print(f"Parse tweet entry failed: {exc}")
                    continue
    except Exception as exc:  # noqa: BLE001
        print(f"Split failed: {exc}")
        return crypto_rows, non_rows
    return crypto_rows, non_rows


def main() -> None:
    cookies = load_cookies()
    session = build_session(cookies)
    home_qid = load_home_qid()
    proxy_rotator = ProxyRotator(PROXIES)

    existing_non = read_existing_ids(NON_CRYPTO_CSV)
    existing_crypto = read_existing_ids(CRYPTO_CSV)
    existing_trashed = read_existing_ids(TRASHED_CRYPTO_CSV)
    existing_combined = read_existing_ids(COMBINED_CRYPTO_CSV)
    print(
        f"Existing non-crypto: {len(existing_non)} > {NON_CRYPTO_CSV}\n"
        f"Existing crypto: {len(existing_crypto)} > {CRYPTO_CSV}\n"
        f"Existing trashed crypto: {len(existing_trashed)} > {TRASHED_CRYPTO_CSV}\n"
        f"Existing combined crypto: {len(existing_combined)} > {COMBINED_CRYPTO_CSV}"
    )

    cursor = None
    batch = 1
    try:
        while True:
            data = None
            for attempt in range(len(PROXIES) * 2):
                proxy = proxy_rotator.next()
                try:
                    data = fetch_home(session, home_qid, proxy, cursor)
                    break
                except Exception as e:  # noqa: BLE001
                    wait = 5
                    print(f"Fetch failed (attempt {attempt+1}): {e}. Proxy {proxy['http']}. Retry in {wait}s...")
                    time.sleep(wait)
            if data is None:
                print("All retries failed. Cooling down 30s before next loop...")
                time.sleep(30)
                continue

            crypto_rows, non_rows = split_crypto_and_non(data)

            added_crypto = save_crypto_rows(CRYPTO_CSV, crypto_rows, existing_crypto)
            # also save crypto rows to trashed and combined datasets
            added_trashed = save_crypto_rows(TRASHED_CRYPTO_CSV, crypto_rows, existing_trashed)
            added_combined = save_crypto_rows(COMBINED_CRYPTO_CSV, crypto_rows, existing_combined)
            added_non = save_non_crypto_rows(NON_CRYPTO_CSV, non_rows, existing_non)

            print(
                f"Batch {batch}: crypto fetched {len(crypto_rows)}, added {added_crypto} (main) / {added_trashed} (trashed) / {added_combined} (combined), total crypto-main {len(existing_crypto)} | "
                f"non-crypto fetched {len(non_rows)}, added {added_non}, total non-crypto {len(existing_non)}"
            )

            cursor = extract_cursor(data)
            if not cursor:
                print("No cursor, restarting from top in next loop")

            sleep_time = 30 + random.uniform(0, 10)
            time.sleep(sleep_time)
            batch += 1
    except KeyboardInterrupt:
        print("Stopped by user. Progress saved.")
        sys.exit(0)


if __name__ == "__main__":
    main()
