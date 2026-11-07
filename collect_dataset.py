
import time
import requests
import pandas as pd
from pathlib import Path

API_URL = "https://api.crossref.org/works"
EMAIL = "subhanaligrw444@gmail.com"

TARGET_RECORDS = 15000
BATCH_SIZE = 1000
OUTPUT_FILE = Path("crossref_dataset.csv")

headers = {
    "User-Agent": f"ResearchPaperExplorer/1.0 (mailto:{EMAIL})"
}

params = {
    "rows": BATCH_SIZE,
    "cursor": "*",
    "mailto": EMAIL,
    "select": (
        "DOI,title,author,publisher,type,issued,"
        "published-print,published-online,"
        "container-title,references-count,"
        "is-referenced-by-count"
    )
}


def get_publication_year(record):
    for field in ["issued", "published-print", "published-online"]:
        date_info = record.get(field) or {}
        date_parts = date_info.get("date-parts") or []

        if date_parts and date_parts[0]:
            return date_parts[0][0]

    return None


def prepare_record(record):
    titles = record.get("title") or []
    title = titles[0] if titles else ""

    authors = record.get("author") or []
    author_names = []

    for author in authors:
        name = " ".join(
            part for part in [
                author.get("given", ""),
                author.get("family", "")
            ]
            if part
        )

        if name:
            author_names.append(name)

    containers = record.get("container-title") or []

    return {
        "DOI": record.get("DOI", ""),
        "Title": title,
        "Authors": "; ".join(author_names),
        "Publisher": record.get("publisher", ""),
        "Publication Year": get_publication_year(record),
        "Publication Type": record.get("type", ""),
        "Journal / Container Title": (
            containers[0] if containers else ""
        ),
        "References Count": record.get("references-count"),
        "Cited-by Count": record.get("is-referenced-by-count"),
        "Author Count": len(author_names),
        "Title Word Count": len(title.split())
    }


def request_batch(current_params):
    # Retry temporary server/rate-limit errors a few times.
    for attempt in range(1, 4):
        try:
            response = requests.get(
                API_URL,
                params=current_params,
                headers=headers,
                timeout=60
            )

            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 3:
                    wait_seconds = 5 * attempt
                    print(
                        f"Temporary API response "
                        f"{response.status_code}. "
                        f"Waiting {wait_seconds} seconds..."
                    )
                    time.sleep(wait_seconds)
                    continue

            response.raise_for_status()
            return response.json()["message"]

        except requests.RequestException:
            if attempt == 3:
                raise

            time.sleep(5 * attempt)

    return None


try:
    # Load previously collected records, if available.
    saved_records = {}

    if OUTPUT_FILE.exists():
        old_df = pd.read_csv(OUTPUT_FILE, dtype={"DOI": str})

        for record in old_df.to_dict("records"):
            doi = str(record.get("DOI", "")).strip()

            if doi and doi.lower() != "nan":
                saved_records[doi] = record

        print("Existing unique records loaded:", len(saved_records))

    if len(saved_records) >= TARGET_RECORDS:
        print(f"Dataset already has at least {TARGET_RECORDS} records.")
    else:
        current_cursor = "*"
        batch_number = 0

        while len(saved_records) < TARGET_RECORDS:
            current_params = params.copy()
            current_params["cursor"] = current_cursor

            message = request_batch(current_params)

            if not message:
                print("The API returned no message. Stopping.")
                break

            records = message.get("items", [])
            batch_number += 1
            new_count = 0

            for record in records:
                doi = str(record.get("DOI", "")).strip()

                # DOI is our unique identifier for each work.
                if not doi or doi in saved_records:
                    continue

                saved_records[doi] = prepare_record(record)
                new_count += 1

                if len(saved_records) >= TARGET_RECORDS:
                    break

            # Save progress after every batch.
            df = pd.DataFrame(list(saved_records.values()))
            df.to_csv(OUTPUT_FILE, index=False, encoding="utf-8")

            print(
                f"Batch {batch_number}: "
                f"received {len(records)}, "
                f"new unique records {new_count}, "
                f"total saved {len(saved_records)}/{TARGET_RECORDS}"
            )

            next_cursor = message.get("next-cursor")

            if (
                len(saved_records) >= TARGET_RECORDS
                or not records
                or not next_cursor
            ):
                break

            current_cursor = next_cursor
            time.sleep(1)

    # Final verification.
    if OUTPUT_FILE.exists():
        final_df = pd.read_csv(OUTPUT_FILE)

        print("\nCollection finished.")
        print("CSV file:", OUTPUT_FILE.resolve())
        print("Rows saved:", len(final_df))
        print("Columns:", len(final_df.columns))
        print("Unique DOIs:", final_df["DOI"].nunique())

        if len(final_df) >= TARGET_RECORDS:
            print("SUCCESS: The 15,000-record requirement is met.")
        else:
            print(
                f"Dataset currently has {len(final_df)} records. "
                "Check the messages above before trying again."
            )

except requests.RequestException as error:
    print("\nNetwork or API error:", error)
    print("Any records saved in earlier batches remain in the CSV.")

except (ValueError, KeyError, TypeError) as error:
    print("\nUnexpected API or data error:", error)
    print("Check the error before restarting the collector.")
