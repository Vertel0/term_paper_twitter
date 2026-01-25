import os
import sys
from typing import Tuple

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

MODEL_DIR = os.getenv(
    "MODEL_DIR", r"c:\Users\Professional\Desktop\kursach\models\crypto-detector-crypto2-mini"
)


def load_model() -> Tuple[AutoModelForSequenceClassification, AutoTokenizer]:
    tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR)
    model = AutoModelForSequenceClassification.from_pretrained(MODEL_DIR)
    model.eval()
    return model, tokenizer


def classify(text: str, model, tokenizer):
    inputs = tokenizer(text, return_tensors="pt", truncation=True, max_length=128)
    with torch.no_grad():
        outputs = model(**inputs)
        probs = torch.softmax(outputs.logits, dim=-1).squeeze()
    prob_crypto = probs[1].item()
    prob_non = probs[0].item()
    label = "crypto" if prob_crypto >= prob_non else "non-crypto"
    return label, prob_crypto, prob_non


def main():
    model, tokenizer = load_model()
    print("Loaded model from", MODEL_DIR)
    print("Enter tweet text (empty line to exit):")
    for line in sys.stdin:
        text = line.strip()
        if not text:
            break
        label, prob_crypto, prob_non = classify(text, model, tokenizer)
        print(f"label={label} | crypto_prob={prob_crypto:.4f} | non_crypto_prob={prob_non:.4f}")
    print("Done.")


if __name__ == "__main__":
    main()
