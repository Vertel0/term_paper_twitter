import os
import csv
import pandas as pd

INPUT_PATH = r"c:\Users\Professional\Desktop\kursach\Database of influencers tweets in cryptocurrency (2021-2023)\dataset_52-person-from-2021-02-05_2023-06-12_21-34-17-266_with_sentiment.csv"
OUTPUT_PATH = r"c:\Users\Professional\Desktop\kursach\Database of influencers tweets in cryptocurrency (2021-2023)\dataset_52_clean.csv"

REQUIRED_COLS = ["created_at", "full_text"]


def main() -> None:
    if not os.path.isfile(INPUT_PATH):
        raise FileNotFoundError(f"Input file not found: {INPUT_PATH}")

    df = pd.read_csv(INPUT_PATH)

    # Drop index-like unnamed columns
    drop_cols = [c for c in df.columns if c.strip() == "" or c.lower().startswith("unnamed")]
    if drop_cols:
        df = df.drop(columns=drop_cols)

    # Drop fully empty rows
    df = df.dropna(how="all")

    # Drop rows with missing required fields or empty full_text
    df = df.dropna(subset=REQUIRED_COLS)
    df = df[df["full_text"].astype(str).str.strip().ne("")]

    # Save with semicolon delimiter
    df.to_csv(OUTPUT_PATH, sep=";", index=False, quoting=csv.QUOTE_MINIMAL)
    print(f"Saved cleaned dataset to {OUTPUT_PATH}. Rows: {len(df)}; Columns: {len(df.columns)}")


if __name__ == "__main__":
    main()
