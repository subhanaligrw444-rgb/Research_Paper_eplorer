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

# Sorting state: the custom algorithm sorts a copy, never the source list in-place.
active_sorted_records = None
active_sort_info = None

SORT_METADATA = {
    "bubble": {"label": "Bubble Sort", "best": "O(n)", "average": "O(n²)", "worst": "O(n²)", "stable": True, "numeric_only": False},
    "selection": {"label": "Selection Sort", "best": "O(n²)", "average": "O(n²)", "worst": "O(n²)", "stable": False, "numeric_only": False},
    "insertion": {"label": "Insertion Sort", "best": "O(n)", "average": "O(n²)", "worst": "O(n²)", "stable": True, "numeric_only": False},
    "merge": {"label": "Merge Sort", "best": "O(n log n)", "average": "O(n log n)", "worst": "O(n log n)", "stable": True, "numeric_only": False},
    "quick": {"label": "Quick Sort", "best": "O(n log n)", "average": "O(n log n)", "worst": "O(n²)", "stable": False, "numeric_only": False},
    "radix": {"label": "Radix Sort", "best": "O(d(n+b))", "average": "O(d(n+b))", "worst": "O(d(n+b))", "stable": True, "numeric_only": True},
    "bucket": {"label": "Bucket Sort", "best": "O(n+b)", "average": "O(n+b)", "worst": "O(n²)", "stable": True, "numeric_only": True},
    "heap": {"label": "Heap Sort", "best": "O(n log n)", "average": "O(n log n)", "worst": "O(n log n)", "stable": False, "numeric_only": False},
    "shell": {"label": "Shell Sort", "best": "O(n log n)", "average": "depends on gap sequence", "worst": "O(n²)", "stable": False, "numeric_only": False},
    "hybrid": {"label": "Hybrid Merge-Insertion Sort", "best": "O(n log n)", "average": "O(n log n)", "worst": "O(n log n)", "stable": True, "numeric_only": False},
}

NUMERIC_KEYS = {
    "Publication Year", "References Count", "Cited-by Count",
    "Author Count", "Title Word Count"
}

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
def advanced_value_matches(value, operator, query):
    """Check whether one field matches an advanced-search condition."""
    value = ("" if value is None else str(value)).casefold()
    query = ("" if query is None else str(query)).casefold()

    if operator == "Contains":
        return query in value
    elif operator == "Starts With":
        return value.startswith(query)
    elif operator == "Ends With":
        return value.endswith(query)
    elif operator == "Equals":
        return value == query

    return False

def advanced_search_records(conditions, logic):
    """Apply column conditions to the complete dataset."""
    if logic not in {"AND", "OR", "NOT"}:
        raise ValueError("Logic must be AND, OR, or NOT.")

    if not conditions:
        raise ValueError("Add at least one search condition.")

    for condition in conditions:
        if not isinstance(condition, dict):
            raise ValueError("Each condition must be an object.")

        column = condition.get("column")
        operator = condition.get("operator")
        query = str(condition.get("value", "")).strip()

        if column not in EXPECTED_COLUMNS:
            raise ValueError(f"Unknown column: {column}")

        if operator not in {
            "Contains", "Starts With", "Ends With", "Equals"
        }:
            raise ValueError(f"Unknown operator: {operator}")

        if not query:
            raise ValueError("Condition values cannot be empty.")

    with data_lock:
        source = [dict(record) for record in records]

    results = []

    for record in source:
        matches = [
            advanced_value_matches(
                record.get(condition["column"], ""),
                condition["operator"],
                condition["value"]
            )
            for condition in conditions
        ]

        if logic == "AND":
            include = all(matches)
        elif logic == "OR":
            include = any(matches)
        else:
            include = not any(matches)

        if include:
            results.append(record)

    return results


def filter_records_for_scope(source, filters=None):
    """Apply ordinary publication filters to a copied record list."""
    filters = filters or {}
    query = str(filters.get("q", "")).strip().casefold()
    year = str(filters.get("year", "")).strip()
    pub_type = str(filters.get("type", "")).strip().casefold()
    filtered = []
    for record in source:
        searchable = " ".join(str(record.get(k, "")) for k in (
            "Title", "Authors", "DOI", "Publisher",
            "Journal / Container Title", "Publication Type"
        )).casefold()
        if query and query not in searchable:
            continue
        if year and str(record.get("Publication Year", "")) != year:
            continue
        if pub_type and str(record.get("Publication Type", "")).casefold() != pub_type:
            continue
        filtered.append(record)
    return filtered


def persist_records_csv():
    """Save the current dataset atomically so new records survive restart."""
    with data_lock:
        snapshot = [dict(record) for record in records]
    temp_path = CSV_PATH.with_suffix(".tmp.csv")
    pd.DataFrame(snapshot, columns=EXPECTED_COLUMNS).to_csv(
        temp_path, index=False, encoding="utf-8"
    )
    temp_path.replace(CSV_PATH)


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

        if added_now:
            global active_sorted_records, active_sort_info
            with data_lock:
                active_sorted_records = None
                active_sort_info = None
        with scraper_lock:
            scraper_status["processed"] += processed_now
            scraper_status["added"] += added_now
            scraper_status["duplicates"] += duplicates_now
        if added_now:
            try:
                persist_records_csv()
            except OSError as exc:
                with scraper_lock:
                    scraper_status["errors"] += 1
                safe_status_update(message=f"CSV save failed: {exc}")

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


def _key_value(record, key):
    """Return a comparable value; missing values are handled by _compare_records."""
    value = record.get(key, "")
    if value is None or str(value).strip() == "":
        return None
    if key in NUMERIC_KEYS:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return str(value).casefold()


def _compare_records(a, b, key, direction=1, stats=None):
    """Compare two records. Missing values remain last in either direction."""
    if stats is not None:
        stats["comparisons"] += 1
    av, bv = _key_value(a, key), _key_value(b, key)
    if av is None and bv is None:
        return 0
    if av is None:
        return 1
    if bv is None:
        return -1
    result = (av > bv) - (av < bv)
    return result * direction


def _swap(items, i, j, stats):
    if i != j:
        items[i], items[j] = items[j], items[i]
        stats["swaps"] += 1


def bubble_sort(items, key, direction=1):
    stats = {"comparisons": 0, "swaps": 0}
    a = list(items)
    n = len(a)
    for end in range(n - 1, 0, -1):
        changed = False
        for i in range(end):
            if _compare_records(a[i], a[i + 1], key, direction, stats) > 0:
                _swap(a, i, i + 1, stats)
                changed = True
        if not changed:
            break
    return a, stats["comparisons"], stats["swaps"]


def selection_sort(items, key, direction=1):
    stats = {"comparisons": 0, "swaps": 0}
    a = list(items)
    for i in range(len(a) - 1):
        best = i
        for j in range(i + 1, len(a)):
            if _compare_records(a[j], a[best], key, direction, stats) < 0:
                best = j
        _swap(a, i, best, stats)
    return a, stats["comparisons"], stats["swaps"]


def insertion_sort(items, key, direction=1):
    stats = {"comparisons": 0, "swaps": 0}
    a = list(items)
    for i in range(1, len(a)):
        current = a[i]
        j = i - 1
        while j >= 0:
            if _compare_records(a[j], current, key, direction, stats) <= 0:
                break
            a[j + 1] = a[j]
            stats["swaps"] += 1  # Count record moves/shifts for this algorithm.
            j -= 1
        a[j + 1] = current
        stats["swaps"] += 1
    return a, stats["comparisons"], stats["swaps"]


def merge_sort(items, key, direction=1):
    stats = {"comparisons": 0, "swaps": 0}
    def split(a):
        if len(a) <= 1:
            return a
        mid = len(a) // 2
        left, right = split(a[:mid]), split(a[mid:])
        merged, i, j = [], 0, 0
        while i < len(left) and j < len(right):
            if _compare_records(left[i], right[j], key, direction, stats) <= 0:
                merged.append(left[i]); i += 1
            else:
                merged.append(right[j]); j += 1
            stats["swaps"] += 1  # Count writes/moves into the merge output.
        while i < len(left):
            merged.append(left[i]); i += 1; stats["swaps"] += 1
        while j < len(right):
            merged.append(right[j]); j += 1; stats["swaps"] += 1
        return merged
    result = split(list(items))
    return result, stats["comparisons"], stats["swaps"]


def quick_sort(items, key, direction=1):
    """Iterative three-way quicksort avoids recursion-depth failures on large CSVs."""
    stats = {"comparisons": 0, "swaps": 0}
    a = list(items)
    stack = [(0, len(a) - 1)] if len(a) > 1 else []
    while stack:
        lo, hi = stack.pop()
        if lo >= hi:
            continue
        pivot = a[(lo + hi) // 2]
        lt, i, gt = lo, lo, hi
        while i <= gt:
            cmp = _compare_records(a[i], pivot, key, direction, stats)
            if cmp < 0:
                _swap(a, lt, i, stats); lt += 1; i += 1
            elif cmp > 0:
                _swap(a, i, gt, stats); gt -= 1
            else:
                i += 1
        # Push larger partition first so the smaller one is processed next.
        left = (lo, lt - 1)
        right = (gt + 1, hi)
        if left[1] - left[0] > right[1] - right[0]:
            if left[0] < left[1]: stack.append(left)
            if right[0] < right[1]: stack.append(right)
        else:
            if right[0] < right[1]: stack.append(right)
            if left[0] < left[1]: stack.append(left)
    return a, stats["comparisons"], stats["swaps"]


def heap_sort(items, key, direction=1):
    stats = {"comparisons": 0, "swaps": 0}
    a = list(items)
    n = len(a)
    # Build a max-heap according to the requested ordering, then extract.
    def sift_down(root, size):
        while True:
            largest = root
            left, right = 2 * root + 1, 2 * root + 2
            if left < size and _compare_records(a[left], a[largest], key, direction, stats) > 0:
                largest = left
            if right < size and _compare_records(a[right], a[largest], key, direction, stats) > 0:
                largest = right
            if largest == root:
                return
            _swap(a, root, largest, stats)
            root = largest
    for i in range(n // 2 - 1, -1, -1):
        sift_down(i, n)
    for end in range(n - 1, 0, -1):
        _swap(a, 0, end, stats)
        sift_down(0, end)
    return a, stats["comparisons"], stats["swaps"]


def shell_sort(items, key, direction=1):
    stats = {"comparisons": 0, "swaps": 0}
    a = list(items)
    gap = len(a) // 2
    while gap > 0:
        for i in range(gap, len(a)):
            temp = a[i]
            j = i
            while j >= gap:
                if _compare_records(a[j - gap], temp, key, direction, stats) <= 0:
                    break
                a[j] = a[j - gap]
                stats["swaps"] += 1
                j -= gap
            a[j] = temp
            stats["swaps"] += 1
        gap //= 2
    return a, stats["comparisons"], stats["swaps"]


def hybrid_sort(items, key, direction=1):
    """Stable hybrid: insertion sort small runs, then merge the runs."""
    stats = {"comparisons": 0, "swaps": 0}
    a = list(items)
    run_size = 32
    for start in range(0, len(a), run_size):
        end = min(start + run_size, len(a))
        for i in range(start + 1, end):
            current = a[i]
            j = i - 1
            while j >= start:
                if _compare_records(a[j], current, key, direction, stats) <= 0:
                    break
                a[j + 1] = a[j]; stats["swaps"] += 1; j -= 1
            a[j + 1] = current; stats["swaps"] += 1
    width = run_size
    while width < len(a):
        for start in range(0, len(a), 2 * width):
            mid = min(start + width, len(a))
            end = min(start + 2 * width, len(a))
            left, right = a[start:mid], a[mid:end]
            i = j = 0
            for out in range(start, end):
                if i >= len(left):
                    a[out] = right[j]; j += 1
                elif j >= len(right):
                    a[out] = left[i]; i += 1
                elif _compare_records(left[i], right[j], key, direction, stats) <= 0:
                    a[out] = left[i]; i += 1
                else:
                    a[out] = right[j]; j += 1
                stats["swaps"] += 1
        width *= 2
    return a, stats["comparisons"], stats["swaps"]


def _numeric_value(record, key):
    value = record.get(key, "")
    if value is None or str(value).strip() == "":
        return None
    try:
        number = float(value)
        if number.is_integer():
            return int(number)
        return number
    except (TypeError, ValueError):
        return None


def radix_sort(items, key, direction=1):
    """Stable LSD radix sort for integer-valued columns; blanks remain last."""
    values = list(items)
    present, missing = [], []
    for record in values:
        number = _numeric_value(record, key)
        (missing if number is None else present).append((number, record))
    if present:
        nums = [pair[0] for pair in present]
        offset = -min(nums) if min(nums) < 0 else 0
        max_value = max(nums) + offset
        exp = 1
        while max_value // exp > 0:
            buckets = [[] for _ in range(10)]
            for number, record in present:
                digit = ((number + offset) // exp) % 10
                buckets[digit].append((number, record))
            present = [pair for bucket in buckets for pair in bucket]
            exp *= 10
        if direction < 0:
            # Reverse key groups, not individual records, to retain stability for ties.
            groups, i = [], 0
            while i < len(present):
                j = i + 1
                while j < len(present) and present[j][0] == present[i][0]: j += 1
                groups.append(present[i:j]); i = j
            present = [pair for group in reversed(groups) for pair in group]
    result = [record for _, record in present] + [record for _, record in missing]
    moves = len(present) * max(1, len(str(max((abs(p[0]) for p in present), default=0))))
    return result, 0, moves


def bucket_sort(items, key, direction=1):
    """Stable bucket sort for numeric columns; insertion-sorts within each bucket."""
    stats = {"comparisons": 0, "swaps": 0}
    present, missing = [], []
    for record in items:
        number = _numeric_value(record, key)
        (missing if number is None else present).append((number, record))
    if len(present) < 2:
        return [r for _, r in present] + [r for _, r in missing], 0, 0
    nums = [pair[0] for pair in present]
    low, high = min(nums), max(nums)
    count = max(1, len(present))
    buckets = [[] for _ in range(count)]
    span = high - low
    for number, record in present:
        index = 0 if span == 0 else min(count - 1, int((number - low) / span * (count - 1)))
        buckets[index].append((number, record))
    output = []
    for bucket in buckets:
        # Stable insertion sort within each bucket.
        for i in range(1, len(bucket)):
            current = bucket[i]
            j = i - 1
            while j >= 0:
                stats["comparisons"] += 1
                cmp = (bucket[j][0] > current[0]) - (bucket[j][0] < current[0])
                if cmp <= 0: break
                bucket[j + 1] = bucket[j]; stats["swaps"] += 1; j -= 1
            bucket[j + 1] = current; stats["swaps"] += 1
        output.extend(bucket)
    if direction < 0:
        groups, i = [], 0
        while i < len(output):
            j = i + 1
            while j < len(output) and output[j][0] == output[i][0]: j += 1
            groups.append(output[i:j]); i = j
        output = [pair for group in reversed(groups) for pair in group]
    return [record for _, record in output] + [record for _, record in missing], stats["comparisons"], stats["swaps"]


# Two additional algorithms beyond the core course set: Heap and Shell.
SORT_FUNCTIONS = {
    "bubble": bubble_sort,
    "selection": selection_sort,
    "insertion": insertion_sort,
    "merge": merge_sort,
    "quick": quick_sort,
    "radix": radix_sort,
    "bucket": bucket_sort,
    "heap": heap_sort,
    "shell": shell_sort,
    "hybrid": hybrid_sort,
}


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
        global scraper_thread, active_sorted_records, active_sort_info
        path = urlparse(self.path).path
        

        
        if path == "/api/advanced-search":
            try:
                body = self.read_json_body()
                conditions = body.get("conditions", [])
                logic = str(body.get("logic", "AND")).strip().upper()
                if not isinstance(conditions, list) or not 1 <= len(conditions) <= 20:
                    raise ValueError("Provide between 1 and 20 search conditions.")
                results = advanced_search_records(conditions, logic)
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
                return
            self.send_json({
                "logic": logic,
                "total_records": len(results),
                "results": [{k: clean_value(v) for k, v in r.items()} for r in results[:100]],
                "returned_records": min(100, len(results)),
                "message": f"Found {len(results):,} matching records; showing up to 100."
            })
            return

        if path == "/api/multi-sort":
            try:
                body = self.read_json_body()
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
                return
            keys = body.get("keys", [])
            scope = str(body.get("scope", "full")).strip().casefold()
            filters = body.get("filters", {})
            if not isinstance(keys, list) or not 1 <= len(keys) <= len(EXPECTED_COLUMNS):
                self.send_json({"error": "Choose between 1 and 11 sort columns."}, 400)
                return
            if scope not in {"full", "filtered"} or not isinstance(filters, dict):
                self.send_json({"error": "Invalid sort scope or filters."}, 400)
                return
            for item in keys:
                if not isinstance(item, dict) or item.get("key") not in EXPECTED_COLUMNS:
                    self.send_json({"error": "Each sort level must use a valid column."}, 400)
                    return
                if str(item.get("direction", "asc")).casefold() not in {"asc", "desc"}:
                    self.send_json({"error": "Sort direction must be asc or desc."}, 400)
                    return
            with data_lock:
                source_copy = [dict(record) for record in records]
            if scope == "filtered":
                source_copy = filter_records_for_scope(source_copy, filters)
            if not source_copy:
                self.send_json({"error": "No records are available for this sort."}, 400)
                return
            started = time.perf_counter()
            total_comparisons = total_moves = 0
            for item in reversed(keys):
                direction = -1 if str(item.get("direction", "asc")).casefold() == "desc" else 1
                source_copy, comparisons, moves = merge_sort(source_copy, item["key"], direction)
                total_comparisons += comparisons
                total_moves += moves
            elapsed_ms = (time.perf_counter() - started) * 1000
            with data_lock:
                active_sorted_records = source_copy
                active_sort_info = {"multi_level": keys, "scope": scope, "filters": filters}
            self.send_json({
                "message": f"Multi-level sorting completed for {len(source_copy):,} records.",
                "keys": keys, "scope": scope, "total_records": len(source_copy),
                "comparisons": total_comparisons, "swaps": total_moves,
                "time_ms": round(elapsed_ms, 3), "algorithm": "Stable multi-pass Merge Sort"
            })
            return

        if path == "/api/benchmark":
            try:
                body = self.read_json_body()
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
                return
            key = str(body.get("key", "Publication Year")).strip()
            scope = str(body.get("scope", "full")).strip().casefold()
            filters = body.get("filters", {})
            if key not in EXPECTED_COLUMNS:
                self.send_json({"error": "Unknown benchmark column."}, 400)
                return
            if scope not in {"full", "filtered"} or not isinstance(filters, dict):
                self.send_json({"error": "Invalid benchmark scope or filters."}, 400)
                return
            with data_lock:
                source_copy = [dict(record) for record in records]
            if scope == "filtered":
                source_copy = filter_records_for_scope(source_copy, filters)
            if not source_copy:
                self.send_json({"error": "No records are available to benchmark."}, 400)
                return
            results = []
            for name, sort_function in SORT_FUNCTIONS.items():
                if SORT_METADATA[name]["numeric_only"] and key not in NUMERIC_KEYS:
                    results.append({
                        "algorithm": name, "algorithm_label": SORT_METADATA[name]["label"],
                        "status": "skipped", "reason": "Numeric column required",
                        "time_ms": None, "comparisons": None, "swaps": None
                    })
                    continue
                data = [dict(record) for record in source_copy]
                started = time.perf_counter()
                try:
                    _, comparisons, swaps = sort_function(data, key, 1)
                    elapsed = (time.perf_counter() - started) * 1000
                    results.append({
                        "algorithm": name, "algorithm_label": SORT_METADATA[name]["label"],
                        "status": "completed", "time_ms": round(elapsed, 3),
                        "comparisons": comparisons, "swaps": swaps,
                        "complexity": SORT_METADATA[name]["average"],
                        "stable": SORT_METADATA[name]["stable"]
                    })
                except Exception as exc:
                    results.append({
                        "algorithm": name, "algorithm_label": SORT_METADATA[name]["label"],
                        "status": "error", "reason": str(exc),
                        "time_ms": None, "comparisons": None, "swaps": None
                    })
            self.send_json({
                "key": key, "scope": scope, "total_records": len(source_copy),
                "results": results,
                "message": f"Benchmarked {len(results)} algorithms on {len(source_copy):,} records."
            })
            return

        if path == "/api/sort":
            try:
                body = self.read_json_body()
            except ValueError as exc:
                self.send_json({"error": str(exc)}, 400)
                return
            algorithm = str(body.get("algorithm", "quick")).strip().casefold()
            key = str(body.get("key", "Title")).strip()
            direction_text = str(body.get("direction", "asc")).strip().casefold()
            direction = -1 if direction_text in ("desc", "descending", "-1") else 1
            if algorithm not in SORT_FUNCTIONS:
                self.send_json({"error": "Unknown sorting algorithm.", "available_algorithms": list(SORT_FUNCTIONS)}, 400)
                return
            if key not in EXPECTED_COLUMNS:
                self.send_json({"error": "Unknown column.", "available_columns": EXPECTED_COLUMNS}, 400)
                return
            if SORT_METADATA[algorithm]["numeric_only"] and key not in NUMERIC_KEYS:
                self.send_json({"error": f"{SORT_METADATA[algorithm]['label']} supports numeric columns only.", "numeric_columns": sorted(NUMERIC_KEYS)}, 400)
                return
            scope = str(body.get("scope", "full")).strip().casefold()
            filters = body.get("filters", {})
            if scope not in {"full", "filtered"} or not isinstance(filters, dict):
                self.send_json({"error": "Invalid sort scope or filters."}, 400)
                return
            with data_lock:
                source_copy = [dict(record) for record in records]
            if scope == "filtered":
                source_copy = filter_records_for_scope(source_copy, filters)
                if not source_copy:
                    self.send_json({"error": "Current filters match no records to sort."}, 400)
                    return
            started = time.perf_counter()
            sorted_copy, comparisons, swaps = SORT_FUNCTIONS[algorithm](source_copy, key, direction)
            elapsed_ms = (time.perf_counter() - started) * 1000
            with data_lock:
                active_sorted_records = sorted_copy
                active_sort_info = {"algorithm": algorithm, "key": key, "direction": direction_text, "scope": scope, "filters": filters}
            meta = SORT_METADATA[algorithm]
            self.send_json({
                "message": f"Sorted {len(sorted_copy):,} records using {meta['label']}.",
                "algorithm": algorithm,
                "algorithm_label": meta["label"],
                "key": key,
                "direction": direction_text,
                "total_records": len(sorted_copy),
                "scope": scope,
                "comparisons": comparisons,
                "swaps": swaps,
                "time_ms": round(elapsed_ms, 3),
                "complexity": {"best": meta["best"], "average": meta["average"], "worst": meta["worst"]},
                "stable": meta["stable"],
                "numeric_only": meta["numeric_only"],
            })
            return

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

        if path == "/api/export":
            try:
                csv_bytes = CSV_PATH.read_bytes()
            except OSError as exc:
                self.send_json({"error": f"Could not read CSV dataset: {exc}"}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="crossref_dataset.csv"')
            self.send_header("Content-Length", str(len(csv_bytes)))
            self.end_headers()
            self.wfile.write(csv_bytes)
            return

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
            requested_filters = {"q": query, "year": year, "type": pub_type}
            with data_lock:
                use_sorted = active_sorted_records is not None
                if use_sorted and active_sort_info and active_sort_info.get("scope") == "filtered":
                    saved_filters = active_sort_info.get("filters", {})
                    normalized_saved = {
                        "q": str(saved_filters.get("q", "")).strip().casefold(),
                        "year": str(saved_filters.get("year", "")).strip(),
                        "type": str(saved_filters.get("type", "")).strip().casefold()
                    }
                    if normalized_saved != requested_filters:
                        use_sorted = False
                source_records = active_sorted_records if use_sorted else records
                current_records = [dict(r) for r in source_records]
            results = filter_records_for_scope(current_records, requested_filters)
            total = len(results)
            start = (page - 1) * PAGE_SIZE
            page_records = results[start:start + PAGE_SIZE]
            self.send_json({
                "page": page,
                "page_size": PAGE_SIZE,
                "total_records": total,
                "total_pages": (total + PAGE_SIZE - 1) // PAGE_SIZE,
                "server_sorted": use_sorted,
                "results": [{k: clean_value(v) for k, v in r.items()} for r in page_records],
            })
            return

        if path == "/":
            self.path = "/index.html"
        super().do_GET()


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), ResearchHandler)
    print("=" * 56)
    print("RESEARCH PAPER EXPLORER — COMPLETE WORKSPACE")
    print("=" * 56)
    print(f"Dataset loaded: {len(records):,} records")
    print(f"Columns: {len(EXPECTED_COLUMNS)}")
    print(f"Website: http://{HOST}:{PORT}")
    print(f"Scraper status: http://{HOST}:{PORT}/api/scraper/status")
    print(f"Sorting endpoint: http://{HOST}:{PORT}/api/sort")
    print(f"Sorting algorithms: {', '.join(SORT_FUNCTIONS)}")
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
