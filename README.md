# CoinCap Query Tool

This script resolves asset slugs and fetches historical USD prices using CoinCap API v3 (Bearer auth).

## Requirements
- Python 3.9+
- Dependencies in `requirements.txt`

## Usage
Input JSON can be a single object, a list, or an object with `queries`.

Example:
```json
{
  "coin": "btc",
  "tweet_date": "2023-01-01",
  "relative_days": 5,
  "vs_currency": "usd"
}
```

Set your API key (or pass `--api-key`):
```
set COINCAP_API_KEY=YOUR_KEY
python coingecko_query.py --input sample_input.json --output sample_output.json
```

Optional overrides:
- COINCAP_PROXY: proxy URL (e.g., http://user:pass@host:port)
- COINCAP_BASE: custom base URL (default https://api.coincap.io/v3)

## Notes
- The script rate-limits requests and retries transient failures.
- If an asset is not found by symbol, the script searches and chooses the top match.

## Web UI (tweet-id -> result page)

1. Install dependencies:

  pip install -r requirements.txt

2. Create env file from template:

  copy .env.example .env

3. Fill `.env` at least with:

  - `COINCAP_API_KEY`
  - `TW_QID_TWEET` (recommended)
  - `AITUNNEL_API_KEY` (optional, for richer NLP + explanation)

4. Run web app:

  python app.py

5. Open browser:

  http://127.0.0.1:5000

Now user inputs only `tweet_id` on the page. API keys are taken automatically from `.env`.
