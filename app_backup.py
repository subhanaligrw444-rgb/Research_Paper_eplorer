
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from pathlib import Path

import pandas as pd

# Project configuration
BASE_DIR = Path(__file__).resolve().parent
print("RUNNING APP FILE:", Path(__file__).resolve())



CSV_PATH = BASE_DIR / "crossref_dataset.csv"
HOST = "127.0.0.1"
PORT = 8000
PAGE_SIZE = 50

# Load the CSV once when the server starts.
if not CSV_PATH.exists():
    raise FileNotFoundError(
        f"Dataset not found: {CSV_PATH}\n"
        "Place crossref_dataset.csv beside app.py."
    )

df = pd.read_csv(CSV_PATH, encoding="utf-8").fillna("")

# Convert CSV records to JSON-friendly dictionaries.
# Keep the original CSV unchanged.
records = df.to_dict(orient="records")

# Create a fast lookup table using DOI as the key
records_by_doi = {
    str(record.get("DOI", "")).strip().casefold(): record
    for record in records
    if str(record.get("DOI", "")).strip()
}


def clean_value(value):
    """Convert values to JSON-safe Python values."""
    if pd.isna(value):
        return None

    if hasattr(value, "item"):
        value = value.item()

    if isinstance(value, (str, int, float, bool)) or value is None:
        return value

    return str(value)


class ResearchHandler(SimpleHTTPRequestHandler):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def send_json(self, data, status=200):
        body = json.dumps(
            data, ensure_ascii=False, allow_nan=False
        ).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        print("REQUESTED PATH:", parsed.path, "| UPDATED APP VERSION")


        if parsed.path == "/api/analytics":
            # Count publications for each year
            year_data = pd.to_numeric(
                df["Publication Year"], errors="coerce"
            ).dropna().astype(int)

            year_counts = (
                year_data.value_counts()
                .sort_index()
            )

            # Count publications for each publication type
            type_data = df["Publication Type"].astype(str).str.strip()
            type_data = type_data[type_data != ""]

            type_counts = type_data.value_counts()

            self.send_json({
                "total_records": len(df),
                "year_counts": [
                    {
                        "year": int(year),
                        "count": int(count)
                    }
                    for year, count in year_counts.items()
                ],
                "type_counts": [
                    {
                        "type": str(pub_type),
                        "count": int(count)
                    }
                    for pub_type, count in type_counts.items()
                ]
            })
            return
        
         # API endpoint: retrieve one paper by DOI
        if parsed.path == "/api/paper":
            params = parse_qs(parsed.query)
            doi = params.get("doi", [""])[0].strip().casefold()

            if not doi:
                self.send_json(
                    {"error": "DOI is required"}, 400
                )
                return

            paper = records_by_doi.get(doi)

            if paper is None:
                self.send_json(
                    {"error": "Paper not found"}, 404
                )
                return

            self.send_json({"paper": paper})
            return


        # API endpoint: dataset summary
        if parsed.path == "/api/stats":
            self.send_json({
                "total_records": len(records),
                "total_columns": len(df.columns),
                "columns": list(df.columns),
                "missing_titles": int(
                    df["Title"].replace("", pd.NA).isna().sum()
                ),
                "missing_authors": int(
                    df["Authors"].replace("", pd.NA).isna().sum()),
                    "years": sorted(
    {
        int(value)
        for value in pd.to_numeric(
            df["Publication Year"], errors="coerce"
        ).dropna()
    },
    reverse=True
),
"types": sorted(
    {
        str(value).strip()
        for value in df["Publication Type"]
        if str(value).strip()
    }
)
                
                  
     })
            return

        # API endpoint: search and paginate publications
        if parsed.path == "/api/publications":
            params = parse_qs(parsed.query)

            query = params.get("q", [""])[0].strip().casefold()
            year = params.get("year", [""])[0].strip()
            pub_type = params.get("type", [""])[0].strip().casefold()

            try:
                page = max(1, int(params.get("page", ["1"])[0]))
            except ValueError:
                self.send_json({"error": "page must be an integer"}, 400)
                return

            # Search across useful text fields.
            results = []

            for record in records:
                title = str(record.get("Title", ""))
                authors = str(record.get("Authors", ""))
                doi = str(record.get("DOI", ""))
                publisher = str(record.get("Publisher", ""))
                journal = str(record.get("Journal / Container Title", ""))
                record_type = str(record.get("Publication Type", ""))
                record_year = str(record.get("Publication Year", ""))

                searchable = " ".join([
                    title, authors, doi, publisher, journal, record_type
                ]).casefold()

                if query and query not in searchable:
                    continue

                if year and record_year != year:
                    continue

                if pub_type and record_type.casefold() != pub_type:
                    continue

                results.append(record)

            total = len(results)
            start = (page - 1) * PAGE_SIZE
            page_records = results[start:start + PAGE_SIZE]

            safe_records = [
                {
                    key: clean_value(value)
                    for key, value in record.items()
                }
                for record in page_records
            ]

            self.send_json({
                "page": page,
                "page_size": PAGE_SIZE,
                "total_records": total,
                "total_pages": (total + PAGE_SIZE - 1) // PAGE_SIZE,
                "results": safe_records
            })
            return

        # Serve the existing HTML, CSS and JavaScript files.
        if parsed.path == "/":
            self.path = "/index.html"

        super().do_GET()


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), ResearchHandler)

    print("=" * 48)
    print("RESEARCH PAPER EXPLORER")
    print("=" * 48)
    print(f"Dataset loaded: {len(records):,} records")
    print(f"Columns: {len(df.columns)}")
    print(f"Website: http://{HOST}:{PORT}")
    print(f"Stats API: http://{HOST}:{PORT}/api/stats")
    print(f"Publications API: http://{HOST}:{PORT}/api/publications")
    print("Press Ctrl+C to stop the server.")
    print("=" * 48)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        server.server_close()
