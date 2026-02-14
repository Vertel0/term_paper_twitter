import json
import os

from coingecko_query import process_input


def main():
    sample = {
        "queries": [
            {
                "coin": "btc",
                "tweet_date": "2023-01-01",
                "relative_days": 0,
                "vs_currency": "usd",
            }
        ]
    }
    api_key = os.getenv("COINCAP_API_KEY", "")
    if not api_key:
        raise SystemExit("Set COINCAP_API_KEY before running demo.")
    results = process_input(sample, api_key)
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
