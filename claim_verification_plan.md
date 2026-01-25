# Claim Verification for Crypto Tweets — Project Plan

## Objectives
- Detect crypto-related tweets and extract verifiable claims (price target, direction, horizon, asset).
- Fetch corresponding market data (CoinGecko/Binance) for the claimed horizon.
- Verify claims against historical prices; compute truthfulness/error metrics and produce a per-claim verdict.
- Aggregate per-author reliability scores over time.

## Data Scopes
- **Positive (crypto)**: `dataset_52-person-from-2021-02-05_2023-06-12_21-34-17-266_with_sentiment.csv` (tweets from 52 crypto influencers, 2021–2023). Use as in-domain crypto text for:
  - Crypto/not-crypto detector training (positive class).
  - Claim-pattern mining (price mentions, directions, horizons).
- **Negative (non-crypto)**: to be collected (random Twitter samples without crypto triggers) for crypto/not-crypto balancing.
- **Evaluation subset**: Manually labeled set of tweets with explicit claims (price/direction/horizon) and ground-truth outcomes from market data.

## Target Tasks
1) **Crypto Detector (Model-0)**
   - Input: tweet text.
   - Output: binary crypto/not-crypto.
   - Models: `vinai/bertweet-base` or `cardiffnlp/twitter-roberta-base`; fine-tune with positive (crypto) vs negative (general).

2) **Claim Extraction (Model-1)**
   - Inputs: crypto tweet.
   - Outputs (slots): asset/ticker, claim type (price_target, directional_call, timeframe_call, news_claim/opinion), numeric target (if any), direction (up/down), horizon (N days), reference date (tweet time).
   - Approach: sequence labeling (NER) + pattern heuristics for numbers/percent/horizons; small LM fine-tune or spaCy custom NER.

3) **Claim Verification (Logic + Data)**
   - Fetch OHLCV for asset for [t0, t0+H] from CoinGecko/Binance.
   - Metrics:
     - Price target error: APE/SMAPE vs target; success if within tolerance or hit target.
     - Directional: sign(close_{t0+H} - close_{t0}).
   - Verdict: true / partially_true / false / unverifiable.

4) **Author Scoring**
   - Aggregate per-author: hit-rate (directional), MAE/SMAPE (price targets), share unverifiable, decay over time.
   - Output: reliability score 0–1.

## Data Preparation
- Clean text: remove URLs/mentions, keep tickers/hashtags, normalize whitespace.
- Dedup tweets; drop non-English optionally.
- Positive set: all crypto influencer tweets (use as crypto class; also for mining claim patterns).
- Negative set: random/general tweets filtered to have no crypto triggers (dictionary-based prefilter).
- Labeling for Model-1: annotate ~1–2k tweets with slots (asset, target, direction, horizon, claim_type). Possible bootstrap via regex to preselect candidates, then human label.

## Feature/Heuristic Aids
- Regex for tickers: `\$[A-Z]{2,10}`; hashtags for coins; dictionary of symbols/aliases.
- Numbers + units: price (`$?\d+[\.\d+]*`), percent (`\d+%`), horizon (`in \d+ (days|weeks|months)`, `by Q\d`, `this week`, `next month`).
- Direction cues: "to", "reach", "break", "above", "below", "pump", "dump", "moon", "bearish", "bullish".

## Modeling Options
- **Crypto Detector**: fine-tune binary classifier; export to ONNX/int8 for speed.
- **Claim Extraction**: 
  - Option A: spaCy NER with custom labels (ASSET, TARGET_VALUE, TARGET_TIME, DIRECTION).
  - Option B: HF token classification on `bertweet-base` with BIO tags.
  - Hybrid: regex+heuristic pre-extract numbers/tickers, model for claim_type & horizon.

## Verification Logic
- Align tweet timestamp -> UTC.
- Determine horizon H (default 30d if missing for price targets; shorter for intraday if specified).
- Fetch historical prices; if missing, mark unverifiable.
- Compute:
  - Target hit: min/max in window vs target.
  - Direction success: sign of delta over H.
  - Error metrics: MAE/SMAPE.
- Return verdict + evidence (prices used, window).

## Evaluation Plan
- Create labeled claim set (few hundred) with outcomes from market data.
- Metrics:
  - Detector: F1 (macro) on crypto vs non.
  - Extraction: slot F1 (asset, number, horizon, direction), claim_type accuracy.
  - Verification: accuracy on direction, target-hit rate, calibration of scores.
  - End-to-end: percentage correctly classified as true/false on labeled set.

## Serving/Integration
- Pipeline:
  1) Detect crypto.
  2) Extract claims (slots + type).
  3) Fetch market data; verify; score.
  4) Aggregate per-author; expose via FastAPI.
- Caching: market data by (asset, date window); rate-limit external APIs.

## Next Steps
- Extract columns schema from `Column_Description.docx` to confirm available fields (id, author, timestamp, text, sentiment, etc.).
- Build dataset prep script: load influencer CSV, clean text, export positives parquet; placeholder for negatives.
- Draft labeling guide for claim slots and verdicts.
- Train Model-0 (crypto detector) on positive vs collected negative.
- Prototype regex+NER for claim extraction on a sampled subset.
