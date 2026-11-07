
import requests
import pandas as pd

API_URL = "https://api.crossref.org/works"
EMAIL = "subhanaligrw444@gmail.com"

headers = {
    "User-Agent": f"ResearchPaperExplorer/1.0 (mailto:{EMAIL})"
}

params = {
    "rows": 10,
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
    date_fields = [
        "issued",
        "published-print",
        "published-online"
    ]

    for field in date_fields:
        date_info = record.get(field, {})
        date_parts = date_info.get("date-parts", [])

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


try:
    response = requests.get(
        API_URL,
        params=params,
        headers=headers,
        timeout=30
    )

    response.raise_for_status()

    message = response.json()["message"]
    records = message["items"]

    prepared_records = [
        prepare_record(record)
        for record in records
    ]

    df = pd.DataFrame(prepared_records)

    print("Dataset preview successful!")
    print("Rows:", len(df))
    print("Columns:", len(df.columns))

    print("\nDataset columns:")
    print(list(df.columns))

    print("\nMissing values per column:")
    print(df.isna().sum() + (df == "").sum())

    print("\nFirst 5 records:")
    print(df.head().to_string(index=False))

    df.to_csv(
        "crossref_sample.csv",
        index=False,
        encoding="utf-8"
    )

    print("\nSaved sample to crossref_sample.csv")

except requests.RequestException as error:
    print("Network or API error:", error)

except (ValueError, KeyError, TypeError) as error:
    print("Unexpected response format:", error)
