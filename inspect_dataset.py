
import pandas as pd
from pathlib import Path

# Find the dataset in the current project folder
csv_path = Path("crossref_dataset.csv")

if not csv_path.exists():
    print("ERROR: crossref_dataset.csv was not found.")
    print("Make sure this file is in your project folder.")
else:
    # Read the CSV without modifying it
    df = pd.read_csv(csv_path, encoding="utf-8")

    print("\n========== DATASET REPORT ==========")
    print("Total records:", len(df))
    print("Total columns:", len(df.columns))

    print("\nColumn names:")
    for number, column in enumerate(df.columns, start=1):
        print(f"{number}. {column}")

    print("\nFirst 3 records:")
    print(df.head(3).to_string(index=False))

    print("\nMissing values per column:")
    print(df.isnull().sum().to_string())

    print("\n========== INSPECTION COMPLETE ==========")
