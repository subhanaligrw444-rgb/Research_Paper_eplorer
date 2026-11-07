
import requests

API_URL = "https://api.crossref.org/works"

EMAIL = "subhanaligrw444@gmail.com"

headers = {
    "User-Agent": f"ResearchPaperExplorer/1.0 (mailto:{EMAIL})"
}

params = {
    "rows": 10,
    "cursor": "*",
    "select": (
        "DOI,title,author,publisher,type,issued,"
        "container-title,references-count,"
        "is-referenced-by-count"
    ),
    "mailto": EMAIL
}

try:
    response = requests.get(
        API_URL,
        params=params,
        headers=headers,
        timeout=30
    )

    response.raise_for_status()

    data = response.json()["message"]
    records = data["items"]

    print("Connection successful!")
    print("Records received:", len(records))
    print("Total matching records:", data.get("total-results"))

    if records:
        print("\nFirst record:")
        print("DOI:", records[0].get("DOI"))
        print("Title:", records[0].get("title"))
        print("Publisher:", records[0].get("publisher"))
        print("Publication type:", records[0].get("type"))
        print("Publication date:", records[0].get("issued"))
        print("References:", records[0].get("references-count"))
        print("Cited-by count:",
              records[0].get("is-referenced-by-count"))

        print("\nAvailable fields:")
        print(sorted(records[0].keys()))

except requests.RequestException as error:
    print("Network or API error:", error)

except (ValueError, KeyError, TypeError) as error:
    print("Unexpected response format:", error)
