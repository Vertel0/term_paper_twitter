#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict


def _default_model_dir() -> str:
    here = Path(__file__).resolve().parent
    return str(here / "crypto_classifier")


@lru_cache(maxsize=2)
def _load_classifier(model_dir: str):
    try:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "Classifier dependencies are missing. Install torch and transformers."
        ) from exc

    if not os.path.isdir(model_dir):
        raise RuntimeError(f"Classifier model directory not found: {model_dir}")

    tokenizer = AutoTokenizer.from_pretrained(model_dir)
    model = AutoModelForSequenceClassification.from_pretrained(model_dir)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)
    model.eval()
    return model, tokenizer, device, torch


def classify_tweet_text(
    text: str,
    *,
    model_dir: str | None = None,
    threshold: float | None = None,
    positive_label_id: int | None = None,
    max_length: int = 128,
) -> Dict[str, Any]:
    model_dir = model_dir or os.getenv("CRYPTO_CLASSIFIER_DIR", _default_model_dir())
    thr = threshold if threshold is not None else float(os.getenv("CRYPTO_CLASSIFIER_THRESHOLD", "0.5"))
    pos_id = positive_label_id if positive_label_id is not None else int(os.getenv("CRYPTO_CLASSIFIER_POS_LABEL", "1"))

    model, tokenizer, device, torch = _load_classifier(model_dir)
    clean = (text or "").strip()

    inputs = tokenizer(clean, return_tensors="pt", truncation=True, max_length=max_length)
    inputs = {k: v.to(device) for k, v in inputs.items()}

    with torch.no_grad():
        out = model(**inputs)
        probs = torch.softmax(out.logits, dim=-1).squeeze(0).detach().cpu().tolist()

    if not isinstance(probs, list):
        probs = [float(probs)]

    if len(probs) < 2:
        raise RuntimeError("Classifier output must have at least 2 classes")

    if pos_id < 0 or pos_id >= len(probs):
        raise RuntimeError(f"Invalid positive label id {pos_id} for {len(probs)}-class output")

    prob_crypto = float(probs[pos_id])
    non_idx = 0 if pos_id != 0 else 1
    prob_non_crypto = float(probs[non_idx])
    is_crypto = prob_crypto >= max(thr, prob_non_crypto)

    return {
        "source": "local_classifier",
        "model_dir": model_dir,
        "threshold": thr,
        "positive_label_id": pos_id,
        "is_crypto": bool(is_crypto),
        "label": "crypto" if is_crypto else "non_crypto",
        "confidence_crypto": round(prob_crypto, 6),
        "confidence_non_crypto": round(prob_non_crypto, 6),
    }
