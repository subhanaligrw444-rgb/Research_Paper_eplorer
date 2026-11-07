import json
import threading
import time
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.request import Request, urlopen

import pandas as pd

# -------------------- Project configuration --------------------
BASE_DIR = Path(__file__).resolve().parent
CSV_PATH = BASE_DIR / "crossref_dataset.csv"
HOST = "127.0.0.1"
PORT = 8000
PAGE_SIZE = 50
CROSSREF_API = "https://api.crossref.org/works"
CROSSREF_ROWS = 100  # Keep each request moderate and predictable.
MAX_SCRAPE_LIMIT = 5000
REQUEST_DELAY_SECONDS = 1.0  # Polite pacing between Crossref requests.
USER_AGENT = "ResearchPaperExplorer/1.0 (local academic project)"

if not CSV_PATH.exists():
    raise FileNotFoundError(
        f"Dataset not found: {CSV_PATH}\nPlace crossref_dataset.csv beside app.py."
    )

# The current dataset remains in memory while the server runs.
df = pd.read_csv(CSV_PATH, encoding="utf-8").fillna("")
EXPECTED_COLUMNS = list(df.columns)
records = df.to_dict(orient="records")
records_by_doi = {
    str(record.get("DOI", "")).strip().casefold(): record
    for record in records
    if str(record.get("DOI", "")).strip()
}
data_lock = threading.RLock()

# Scraper controls shared by the HTTP handler and the worker thread.
scraper_lock = threading.RLock()
pause_event = threading.Event()
pause_event.set()  # set = allowed to run; cleared = paused
stop_event = threading.Event()
scraper_thread = None
scraper_status = {
    "state": "idle",
    "target": 0,
    "processed": 0,
    "added": 0,
    "duplicates": 0,
    "errors": 0,
    "progress": 0,
    "message": "Ready to collect records from Crossref.",
    "started_at": None,
    "updated_at": None,
}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_value(value):
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def safe_status_update(**changes):
    with scraper_lock:
        scraper_status.update(changes)
        scraper_status["updated_at"] = now_iso()
        target = int(scraper_status.get("target") or 0)
        processed = int(scraper_status.get("processed") or 0)
        scraper_status["progress"] = min(100, round(processed * 100 / target)) if target else 0


def crossref_to_record(item):
    """Map Crossref work metadata into this project's existing 11-column schema."""
    title_list = item.get("title") or []
    title = str(title_list[0]).strip() if title_list else ""

    author_names = []
    for author in item.get("author") or []:
        given = str(author.get("given") or "").strip()
        family = str(author.get("family") or "").strip()
        name = " ".join(part for part in (given, family) if part)
        if not name:
            name = str(author.get("name") or "").strip()
        if name:
            author_names.append(name)

    date_parts = (item.get("published-print") or item.get("published-online") or
                  item.get("issued") or {}).get("date-parts") or []
    year = ""
    if date_parts and date_parts[0]:
        try:
            year = int(date_parts[0][0])
        except (TypeError, ValueError, IndexError):
            year = ""

    types = {
        "journal-article": "Journal Article",
        "proceedings-article": "Conference Paper",
        "book-chapter": "Book Chapter",
        "book": "Book",
        "posted-content": "Preprint",
        "dissertation": "Dissertation",
        "report": "Report",
        "review": "Review",
    }
    raw_type = str(item.get("type") or "")
    pub_type = types.get(raw_type, raw_type.replace("-", " ").title() or "Unknown")
    containers = item.get("container-title") or []
    journal = str(containers[0]).strip() if containers else ""
    doi = str(item.get("DOI") or "").strip().casefold()
    references = item.get("references-count", "")
    citations = item.get("is-referenced-by-count", "")

    return {
        "DOI": doi,
        "Title": title,
        "Authors": "; ".join(author_names),
        "Publisher": str(item.get("publisher") or "").strip(),
        "Publication Year": year,
        "Publication Type": pub_type,
        "Journal / Container Title": journal,
        "References Count": references if references is not None else "",
        "Cited-by Count": citations if citations is not None else "",
        "Author Count": len(author_names),
        "Title Word Count": len(title.split()),
    }


def crossref_request(params, timeout=25):
    url = CROSSREF_API + "?" + urlencode(params)
    request = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def scraper_worker(limit, query):
    cursor = "*"
    seen_in_run = set()
    consecutive_failures = 0
    safe_status_update(state="running", message="Connecting to Crossref...")

    while True:
        # Pause and stop are checked between network requests.
        while not pause_event.wait(timeout=0.25):
            if stop_event.is_set():
                break
        if stop_event.is_set():
            safe_status_update(state="stopped", message="Scraping stopped by user.")
            return

        with scraper_lock:
            remaining = max(0, limit - scraper_status["processed"])
        if remaining <= 0:
            safe_status_update(state="completed", message="Record limit reached.")
            return

        rows = min(CROSSREF_ROWS, remaining)
        params = {"rows": rows, "cursor": cursor, "select": ",".join([
            "DOI", "title", "author", "publisher", "published-print",
            "published-online", "issued", "type", "container-title",
            "references-count", "is-referenced-by-count"
        ])}
        if query:
            params["query"] = query

        try:
            payload = crossref_request(params)
            message = payload.get("message") or {}
            items = message.get("items") or []
            next_cursor = message.get("next-cursor")
            consecutive_failures = 0
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            consecutive_failures += 1
            with scraper_lock:
                scraper_status["errors"] += 1
            if consecutive_failures >= 5:
                safe_status_update(state="error", message=f"Crossref request failed repeatedly: {type(exc).__name__}: {exc}")
                return
            wait_seconds = min(2 ** consecutive_failures, 16)
            safe_status_update(state="running", message=f"Temporary Crossref error. Retrying in {wait_seconds}s ({consecutive_failures}/5).")
            for _ in range(wait_seconds * 4):
                if stop_event.is_set():
                    safe_status_update(state="stopped", message="Scraping stopped by user.")
                    return
                while not pause_event.wait(timeout=0.25):
                    if stop_event.is_set():
                        safe_status_update(state="stopped", message="Scraping stopped by user.")
                        return
            continue

        if not items:
            safe_status_update(state="completed", message="Crossref returned no more records.")
            return

        added_now = 0
        duplicates_now = 0
        processed_now = 0
        with data_lock:
            for item in items:
                if processed_now >= remaining:
                    break
                record = crossref_to_record(item)
                doi = record["DOI"]
                if not doi:
                    # A record without a DOI cannot be reliably deduplicated.
                    with scraper_lock:
                        scraper_status["errors"] += 1
                    processed_now += 1
                    continue
                if doi in seen_in_run or doi in records_by_doi:
                    duplicates_now += 1
                else:
                    records.append(record)
                    records_by_doi[doi] = record
                    added_now += 1
                seen_in_run.add(doi)
                processed_now += 1

        with scraper_lock:
            scraper_status["processed"] += processed_now
            scraper_status["added"] += added_now
            scraper_status["duplicates"] += duplicates_now

        with scraper_lock:
            processed_total = scraper_status["processed"]
        if stop_event.is_set():
            safe_status_update(state="stopped", message="Scraping stopped by user.")
            return
        if not pause_event.is_set():
            safe_status_update(state="paused", message="Scraping paused.")
        else:
            safe_status_update(
                state="running",
                message=f"Processed {processed_total:,} of {limit:,} requested records."
            )

        if processed_total >= limit:
            safe_status_update(state="completed", message="Record limit reached.")
            return
        if not next_cursor or next_cursor == cursor:
            safe_status_update(state="completed", message="Crossref pagination finished.")
            return
        cursor = next_cursor

        # Be polite to Crossref. Pause/stop controls remain responsive during delay.
        for _ in range(int(REQUEST_DELAY_SECONDS * 4)):
            if stop_event.is_set():
                safe_status_update(state="stopped", message="Scraping stopped by user.")
                return
            while not pause_event.wait(timeout=0.25):
                if stop_event.is_set():
                    safe_status_update(state="stopped", message="Scraping stopped by user.")
                    return


def get_scraper_status():
    with scraper_lock:
        result = dict(scraper_status)
    with data_lock:
        result["dataset_records"] = len(records)
    return result


class ResearchHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > 100_000:
                raise ValueError("Request body is too large.")
            raw = self.rfile.read(length) if length else b"{}"
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("JSON body must be an object.")
            return value
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid JSON request: {exc}") from exc

    def do_POST(self):
        global scraper_thread
        path = urlparse(self.path).path
        if path.startswith("/api/scraper/"):
            try:
                body = self.read_json_body() if path.endswith("/start") else {}
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
                return

            if path == "/api/scraper/start":
                try:
                    limit = int(body.get("limit", 100))
                except (TypeError, ValueError):
                    self.send_json({"error": "Record limit must be a whole number."}, 400)
                    return
                if not 1 <= limit <= MAX_SCRAPE_LIMIT:
                    self.send_json({"error": f"Record limit must be between 1 and {MAX_SCRAPE_LIMIT}."}, 400)
                    return
                query = str(body.get("query", "")).strip()
                if len(query) > 200:
                    self.send_json({"error": "Search query must be 200 characters or fewer."}, 400)
                    return
                with scraper_lock:
                    if scraper_thread and scraper_thread.is_alive():
                        self.send_json({"error": "A scraping job is already active."}, 409)
                        return
                    stop_event.clear()
                    pause_event.set()
                    scraper_status.update({
                        "state": "starting", "target": limit, "processed": 0,
                        "added": 0, "duplicates": 0, "errors": 0, "progress": 0,
                        "message": "Scraping job starting.", "started_at": now_iso(),
                        "updated_at": now_iso(),
                    })
                    scraper_thread = threading.Thread(
                        target=scraper_worker, args=(limit, query), daemon=True
                    )
                    scraper_thread.start()
                self.send_json({"message": "Scraping started.", "status": get_scraper_status()}, 202)
                return

            if path == "/api/scraper/pause":
                with scraper_lock:
                    if scraper_status["state"] != "running":
                        self.send_json({"error": "Only a running job can be paused."}, 409)
                        return
                    pause_event.clear()
                    scraper_status["state"] = "paused"
                    scraper_status["message"] = "Scraping paused."
                    scraper_status["updated_at"] = now_iso()
                self.send_json({"message": "Scraping paused.", "status": get_scraper_status()})
                return

            if path == "/api/scraper/resume":
                with scraper_lock:
                    if scraper_status["state"] != "paused":
                        self.send_json({"error": "Only a paused job can be resumed."}, 409)
                        return
                    pause_event.set()
                    scraper_status["state"] = "running"
                    scraper_status["message"] = "Scraping resumed."
                    scraper_status["updated_at"] = now_iso()
                self.send_json({"message": "Scraping resumed.", "status": get_scraper_status()})
                return

            if path == "/api/scraper/stop":
                with scraper_lock:
                    if scraper_status["state"] not in ("starting", "running", "paused"):
                        self.send_json({"error": "There is no active scraping job to stop."}, 409)
                        return
                    stop_event.set()
                    pause_event.set()
                    scraper_status["message"] = "Stopping scraping after the current request..."
                    scraper_status["updated_at"] = now_iso()
                self.send_json({"message": "Stop requested.", "status": get_scraper_status()})
                return

            self.send_json({"error": "Unknown scraper action."}, 404)
            return

        self.send_json({"error": "Unknown API endpoint."}, 404)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/scraper/status":
            self.send_json(get_scraper_status())
            return

        if path == "/api/analytics":
            with data_lock:
                current_df = pd.DataFrame(records, columns=EXPECTED_COLUMNS)
            year_data = pd.to_numeric(current_df["Publication Year"], errors="coerce").dropna().astype(int)
            year_counts = year_data.value_counts().sort_index()
            type_data = current_df["Publication Type"].astype(str).str.strip()
            type_data = type_data[type_data != ""]
            type_counts = type_data.value_counts()
            self.send_json({
                "total_records": len(current_df),
                "year_counts": [{"year": int(y), "count": int(c)} for y, c in year_counts.items()],
                "type_counts": [{"type": str(t), "count": int(c)} for t, c in type_counts.items()],
            })
            return

        if path == "/api/paper":
            doi = parse_qs(parsed.query).get("doi", [""])[0].strip().casefold()
            if not doi:
                self.send_json({"error": "DOI is required"}, 400)
                return
            with data_lock:
                paper = records_by_doi.get(doi)
                if paper is not None:
                    paper = dict(paper)
            if paper is None:
                self.send_json({"error": "Paper not found"}, 404)
                return
            self.send_json({"paper": {k: clean_value(v) for k, v in paper.items()}})
            return

        if path == "/api/stats":
            with data_lock:
                current_df = pd.DataFrame(records, columns=EXPECTED_COLUMNS).fillna("")
            self.send_json({
                "total_records": len(current_df),
                "total_columns": len(current_df.columns),
                "columns": list(current_df.columns),
                "missing_titles": int(current_df["Title"].replace("", pd.NA).isna().sum()),
                "missing_authors": int(current_df["Authors"].replace("", pd.NA).isna().sum()),
                "years": sorted({int(v) for v in pd.to_numeric(current_df["Publication Year"], errors="coerce").dropna()}, reverse=True),
                "types": sorted({str(v).strip() for v in current_df["Publication Type"] if str(v).strip()}),
            })
            return

        if path == "/api/publications":
            params = parse_qs(parsed.query)
            query = params.get("q", [""])[0].strip().casefold()
            year = params.get("year", [""])[0].strip()
            pub_type = params.get("type", [""])[0].strip().casefold()
            try:
                page = max(1, int(params.get("page", ["1"])[0]))
            except ValueError:
                self.send_json({"error": "page must be an integer"}, 400)
                return
            with data_lock:
                current_records = [dict(r) for r in records]
            results = []
            for record in current_records:
                searchable = " ".join(str(record.get(k, "")) for k in (
                    "Title", "Authors", "DOI", "Publisher", "Journal / Container Title", "Publication Type"
                )).casefold()
                if query and query not in searchable:
                    continue
                if year and str(record.get("Publication Year", "")) != year:
                    continue
                if pub_type and str(record.get("Publication Type", "")).casefold() != pub_type:
                    continue
                results.append(record)
            total = len(results)
            start = (page - 1) * PAGE_SIZE
            page_records = results[start:start + PAGE_SIZE]
            self.send_json({
                "page": page,
                "page_size": PAGE_SIZE,
                "total_records": total,
                "total_pages": (total + PAGE_SIZE - 1) // PAGE_SIZE,
                "results": [{k: clean_value(v) for k, v in r.items()} for r in page_records],
            })
            return

        if path == "/":
            self.path = "/index.html"
        super().do_GET()


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), ResearchHandler)
    print("=" * 56)
    print("RESEARCH PAPER EXPLORER — MODULE 1 SCRAPING CONTROLS")
    print("=" * 56)
    print(f"Dataset loaded: {len(records):,} records")
    print(f"Columns: {len(EXPECTED_COLUMNS)}")
    print(f"Website: http://{HOST}:{PORT}")
    print(f"Scraper status: http://{HOST}:{PORT}/api/scraper/status")
    print(f"Maximum records per scraping job: {MAX_SCRAPE_LIMIT:,}")
    print("Press Ctrl+C to stop the server.")
    print("=" * 56)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
    finally:
        stop_event.set()
        pause_event.set()
        server.server_close()
