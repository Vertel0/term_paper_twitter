import os
import random
from dataclasses import dataclass
from typing import List

import numpy as np
import pandas as pd
from datasets import Dataset, DatasetDict
from sklearn.metrics import precision_recall_fscore_support
from transformers import (
    AutoModelForSequenceClassification,
    AutoTokenizer,
    Trainer,
    TrainingArguments,
    DataCollatorWithPadding,
)

# Paths
POS_PATH = r"c:\Users\Professional\Desktop\kursach\crypto_twitter_dataset2.csv"
NEG_PATH = r"c:\Users\Professional\Desktop\kursach\non_crypto_tweets.csv"
MODEL_NAME = os.getenv("BASE_MODEL", "prajjwal1/bert-mini")
OUT_DIR = os.getenv(
    "OUT_DIR", r"c:\Users\Professional\Desktop\kursach\models\crypto-detector-crypto2-mini"
)

# Fixed sample sizes: 3000 train (balanced) + 700 test (balanced) per class
TRAIN_SAMPLES_PER_CLASS = int(os.getenv("TRAIN_SAMPLES_PER_CLASS", 3000))
TEST_SAMPLES_PER_CLASS = int(os.getenv("TEST_SAMPLES_PER_CLASS", 700))
EPOCHS = float(os.getenv("EPOCHS", 5))
RANDOM_SEED = int(os.getenv("SEED", 42))

TOTAL_NEEDED = TRAIN_SAMPLES_PER_CLASS + TEST_SAMPLES_PER_CLASS

random.seed(RANDOM_SEED)
np.random.seed(RANDOM_SEED)


def load_positive(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, sep=";")
    text_col = None
    for cand in ["tweet_text", "clean_text", "full_text"]:
        if cand in df.columns:
            text_col = cand
            break
    if text_col is None:
        raise ValueError("No text column found in positive dataset")
    df = df[[text_col]].rename(columns={text_col: "text"})
    df = df.dropna(subset=["text"])
    df["text"] = df["text"].astype(str).str.strip()
    df = df[df["text"].ne("")]
    df["label"] = 1
    return df


def load_negative(path: str) -> pd.DataFrame:
    df_raw = pd.read_csv(path, sep=";")
    text_col = "full_text"
    if text_col not in df_raw.columns:
        raise ValueError("full_text column missing in negative dataset")
    df_full = df_raw[[text_col]].rename(columns={text_col: "text"})
    df_full = df_full.dropna(subset=["text"])
    df_full["text"] = df_full["text"].astype(str).str.strip()
    df_full = df_full[df_full["text"].ne("")]
    df = df_full.copy()
    # drop potential crypto noise by simple keyword filter
    keywords = [
        "bitcoin",
        "btc",
        "eth",
        "ethereum",
        "crypto",
        "blockchain",
        "binance",
        "bnb",
        "defi",
        "nft",
        "web3",
        "sol",
        "solana",
        "cardano",
        "ada",
        "xrp",
        "doge",
        "dogecoin",
        "matic",
        "l2",
        "airdrop",
        "staking",
        "dex",
        "cex",
        "altcoin",
        "$",
    ]
    low = df["text"].str.lower()
    mask = ~low.str.contains("|".join([k.replace("$", "\\$") for k in keywords]), regex=True)
    df = df[mask]
    if len(df) < TOTAL_NEEDED:
        print(f"Warning: filtered negatives {len(df)} < needed {TOTAL_NEEDED}; using unfiltered negatives")
        df = df_full
    df["label"] = 0
    return df


@dataclass
class EncodedDataset:
    dataset: DatasetDict
    tokenizer: AutoTokenizer


def prepare_dataset() -> EncodedDataset:
    pos = load_positive(POS_PATH)
    neg = load_negative(NEG_PATH)

    total_needed = TOTAL_NEEDED
    if len(pos) < total_needed or len(neg) < total_needed:
        raise ValueError("Not enough data for requested train/test sizes")

    pos_sample = pos.sample(total_needed, random_state=RANDOM_SEED).reset_index(drop=True)
    neg_sample = neg.sample(total_needed, random_state=RANDOM_SEED).reset_index(drop=True)

    pos_train = pos_sample.iloc[:TRAIN_SAMPLES_PER_CLASS]
    pos_test = pos_sample.iloc[TRAIN_SAMPLES_PER_CLASS:total_needed]
    neg_train = neg_sample.iloc[:TRAIN_SAMPLES_PER_CLASS]
    neg_test = neg_sample.iloc[TRAIN_SAMPLES_PER_CLASS:total_needed]

    train_df = pd.concat([pos_train, neg_train], ignore_index=True).sample(
        frac=1, random_state=RANDOM_SEED
    ).reset_index(drop=True)
    test_df = pd.concat([pos_test, neg_test], ignore_index=True).sample(
        frac=1, random_state=RANDOM_SEED
    ).reset_index(drop=True)
    ds = DatasetDict(
        {
            "train": Dataset.from_pandas(train_df.reset_index(drop=True)),
            "test": Dataset.from_pandas(test_df.reset_index(drop=True)),
        }
    )
    tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)

    def tokenize(batch):
        return tokenizer(batch["text"], truncation=True, max_length=96)

    ds = ds.map(tokenize, batched=True)
    return EncodedDataset(dataset=ds, tokenizer=tokenizer)


def main():
    enc = prepare_dataset()
    data_collator = DataCollatorWithPadding(tokenizer=enc.tokenizer)

    print(f"Train size: {len(enc.dataset['train'])}, Eval size: {len(enc.dataset['test'])}")
    print(f"Using base model: {MODEL_NAME}")
    print(f"Epochs: {EPOCHS}, batch size: 8")

    model = AutoModelForSequenceClassification.from_pretrained(MODEL_NAME, num_labels=2)

    args = TrainingArguments(
        output_dir=OUT_DIR,
        eval_strategy="epoch",
        save_strategy="epoch",
        num_train_epochs=EPOCHS,
        max_steps=int(os.getenv("MAX_STEPS", 1200)),
        per_device_train_batch_size=8,
        per_device_eval_batch_size=8,
        learning_rate=2e-5,
        weight_decay=0.01,
        logging_steps=50,
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        save_total_limit=1,
        report_to=[],
    )

    def compute_metrics(eval_pred):
        logits, labels = eval_pred
        preds = np.argmax(logits, axis=-1)
        precision, recall, f1, _ = precision_recall_fscore_support(
            labels, preds, average="binary", zero_division=0
        )
        acc = (preds == labels).mean()
        return {
            "accuracy": acc,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }

    trainer = Trainer(
        model=model,
        args=args,
        train_dataset=enc.dataset["train"],
        eval_dataset=enc.dataset["test"],
        tokenizer=enc.tokenizer,
        data_collator=data_collator,
        compute_metrics=compute_metrics,
    )

    print("Starting training...")
    trainer.train()

    metrics = trainer.evaluate()
    print(
        {
            "eval_accuracy": metrics.get("eval_accuracy"),
            "eval_precision": metrics.get("eval_precision"),
            "eval_recall": metrics.get("eval_recall"),
            "eval_f1": metrics.get("eval_f1"),
            "eval_loss": metrics.get("eval_loss"),
        }
    )

    os.makedirs(OUT_DIR, exist_ok=True)
    trainer.save_model(OUT_DIR)
    enc.tokenizer.save_pretrained(OUT_DIR)
    print(f"Model saved to {OUT_DIR}")


if __name__ == "__main__":
    main()
