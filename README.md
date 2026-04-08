# Web Technology Scanner

An automated tool that scans websites and detects the technology stack they use. Starting from a list of domains, the script crawls pages and identifies technologies using multiple detection engines running in parallel.

---

## Overview

- Reads domains from a `.parquet` database file
- Optionally crawls each domain to discover additional internal URLs
- Skips already-scanned domains to allow resuming interrupted runs
- Runs up to **100 concurrent async workers** to scan domains
- Uses **4 independent detection engines** and merges their results
- Saves output incrementally to a `.jsonl` file

---

## Project Structure

```
.
├── main.py              # Main script (this file)
├── get_output.py        # Post-processing of results
├── db.parquet           # Input: list of root domains
├── domains.txt          # Discovered URLs (crawling output)
├── domains.jsonl        # Per-domain crawl maps
└── output.jsonl         # Scan results (one JSON entry per line)
```

---

## How It Works

### Step 1 — Loading Domains

Domains are read from a Parquet file and prefixed with `https://`. The list is shuffled randomly before processing to avoid hammering the same servers sequentially.

```python
doc = pd.read_parquet('db.parquet', engine='pyarrow')
domains = [row.root_domain for row in doc.itertuples()]
domains = ["https://" + domain for domain in domains]
shuffle(domains)
```

---

### Step 2 — Optional Crawling

The user is asked whether to discover new URLs by crawling. If yes, each base domain is crawled recursively (up to 100 pages), following only internal links and skipping media files.

```python
skip_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.svg', '.pdf', '.zip', '.mp4', '.webp'}
```

New URLs are appended to `domains.txt` in real time and protected by a mutex to avoid race conditions across threads:

```python
with mutex:
    if full_url not in existing_domains_set:
        existing_domains_set.add(full_url)
        with open('domains.txt', 'a', encoding='utf-8') as f:
            f.write(f'{full_url}\n')
```

Crawling runs with **30 threads in parallel** using `ThreadPoolExecutor`.

---

### Step 3 — Deduplication / Resume Support

Before scanning, the script reads `output.jsonl` and collects all already-processed URLs. These are excluded from the current run, so interrupted scans can be safely resumed.

```python
with open(OUTPUT_FILENAME_JSONL, 'r', encoding='utf-8') as f:
    for line in f:
        try:
            entry = json.loads(line.strip())
            if isinstance(entry, dict):
                already_used_urls.update(entry.keys())
        except Exception:
            continue

domains_set = domains_set - already_used_urls
```

---

### Step 4 — Async Scanning (100 workers)

Each domain is scanned as an async task. A `Semaphore` limits concurrency to 100 simultaneous requests.

```python
semaphore = asyncio.Semaphore(NUMBER_OF_WORKERS)
tasks = [scan_domain(session, domain, semaphore) for domain in domains_set]
result_list = await tqdm.gather(*tasks, desc="Scanning domains")
```

Each scan fetches the page HTML and passes it to all 4 detection engines.

---

### Step 5 — Technology Detection (4 Engines)

Each engine runs independently and results are merged into a single dictionary. If a technology is already found by a previous engine, it is not overwritten.

#### Engine 1 — BuiltWith

Uses the `builtwith` Python library. Parses the HTML directly for known technology signatures.

```python
tech_found = builtwith.parse(url, html=html_content)
for category, technology in tech_found.items():
    for t in technology:
        if t not in result['technologies']:
            result['technologies'][t] = category
```

#### Engine 2 — WhatWeb

Runs `whatweb` as a Linux subprocess with aggression level 3, outputs a JSON file to `/tmp/`, reads it, then deletes it. Certain generic plugins (like `html5`, `cookies`, `redirect`) are filtered out.

```python
result = subprocess.run(
    ['whatweb', f'--log-json={file_path}', '-a', '3', url, '--quiet'],
    capture_output=True,
    timeout=1_000_000
)
```

WhatWeb subprocess calls run in a **dedicated executor** with 20 workers to avoid blocking the async event loop:

```python
subprocess_executor = ThreadPoolExecutor(max_workers=20)

whatweb_techs = await loop.run_in_executor(
    subprocess_executor,
    partial(detect_with_whatweb, url)
)
```

#### Engine 3 — Wappalyzer

Uses the `python-Wappalyzer` library. Fetches the page itself and analyzes headers, HTML, and scripts.

```python
wapp = Wappalyzer.latest()
webpage = WebPage.new_from_url(url, verify=False)
technologies = wapp.analyze_with_categories(webpage)
```

#### Engine 4 — WebTech

Uses the `webtech` Python library as a fourth independent source of detection.

```python
wt = webtech.WebTech(options={'urls': [url], 'json': True})
wt.start()
```

---

### Step 6 — Output

Each domain result is written immediately to `output.jsonl` after scanning (without waiting for all tasks to finish). A file lock ensures no concurrent writes corrupt the file.

```python
def write_file(filename, domain, result):
    with file_lock:
        entry = { domain: result }
        with open(filename, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry) + "\n")
```

Each line in `output.jsonl` looks like:

```json
{
  "https://example.com": {
    "technologies": {
      "WordPress": "CMS",
      "jQuery": "JavaScript frameworks",
      "Nginx": "Web servers"
    },
    "proofs": [
      "[WordPress] Found category 'CMS' in Builtwith library",
      "[jQuery] Found with WhatWeb Linux subprocess",
      "[Nginx] Found in Wappalyzer"
    ],
    "count": 3
  }
}
```

After all domains are scanned, `get_output()` is called for final post-processing.

---

### Cleanup

On exit (including `CTRL+C`), any leftover WhatWeb temp files in `/tmp/ww_result_*.json` are automatically deleted:

```python
signal.signal(signal.SIGINT, cleanup_whatweb_files_signal)
```

---

## Configuration

| Constant | Default | Description |
|---|---|---|
| `NUMBER_OF_WORKERS` | `100` | Max concurrent async domain scans |
| `TIMEOUT_SECONDS` | `300` | HTTP request timeout per domain |
| `OUTPUT_FILENAME_JSONL` | `output.jsonl` | Incremental results file |

---

## Requirements

- Python 3.8+
- `whatweb` installed and available in `PATH`
- Python packages: `aiohttp`, `builtwith`, `pandas`, `pyarrow`, `beautifulsoup4`, `python-Wappalyzer`, `webtech`, `tqdm`, `requests`