
import time
import requests

API_URL = "https://api.crossref.org/works"
EMAIL = "subhanaligrw444@gmail.com"

headers = {
    "User-Agent": f"ResearchPaperExplorer/1.0 (mailto:{EMAIL})"
}

params = {
    "rows": 100,
    "cursor": "*",
    "mailto": EMAIL,
    "select": (
        "DOI,title,author,publisher,type,issued,"
        "container-title,references-count,"
        "is-referenced-by-count"
    )
}

seen_dois = set()

try:
    for page in range(1, 4):
        response = requests.get(
            API_URL,
            params=params,
            headers=headers,
            timeout=30
        )
        response.raise_for_status()

        message = response.json()["message"]
        records = message["items"]

        valid_dois = [
            record["DOI"]
            for record in records
            if record.get("DOI")
        ]

        new_dois = set(valid_dois) - seen_dois
        seen_dois.update(valid_dois)

        print(f"\nBatch {page}")
        print("Records received:", len(records))
        print("Records with DOI:", len(valid_dois))
        print("New unique DOIs:", len(new_dois))

        next_cursor = message.get("next-cursor")

        if not records or not next_cursor:
            print("No further batch available.")
            break

        params["cursor"] = next_cursor
        time.sleep(1)

    print("\nPagination test finished.")
    print("Unique DOIs across batches:", len(seen_dois))

except requests.RequestException as error:
    print("Network or API error:", error)

except (ValueError, KeyError, TypeError) as error:
    print("Unexpected response format:", error)
