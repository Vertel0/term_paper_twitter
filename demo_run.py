import json
import os

from binance_query import process_input


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
    api_key = os.getenv("BINANCE_API_KEY", "")
    results = process_input(sample, api_key)
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
