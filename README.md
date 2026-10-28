# Link Crawler

A fast, polite Python crawler that walks a website recursively and collects **every link it finds together with its title**. Results are saved as **CSV** (opens straight in Excel) or **JSON**.

```text
$ python crawler.py https://example.com -o links.csv
10:15:02 INFO    Crawling https://example.com/  (max depth: unlimited, max pages: 1000, workers: 5)
10:15:03 INFO    [1/1000] depth 0  200  https://example.com/  | Home
10:15:03 INFO    [2/1000] depth 1  200  https://example.com/about  | About Us
10:15:04 INFO    [3/1000] depth 1  301  https://example.com/blog -> https://example.com/blog/  | Blog
...
10:15:41 INFO    Done in 38.6s: 74 pages fetched, 312 unique links (241 internal, 71 external, 2 broken).
```

## Features

- **Recursive crawl** of a whole site (breadth-first), with optional depth and page limits
- **Collects every link** – internal pages, external links and files (PDFs, images, documents …)
- **Titles for everything** – the real `<title>` of every crawled page; the link text for links that weren't fetched. Optionally fetch external pages too, to get their real titles
- **Fast but polite** – parallel workers plus a per-host delay between requests
- **Respects `robots.txt`** by default, including `Crawl-delay`
- **Robust** – retries with back-off on timeouts, 429 and 5xx; follows and records redirects; skips huge pages
- **Any language** – detects page encodings correctly; the CSV is written as UTF-8 with BOM so Excel shows non-Latin titles properly
- **Filters** – include/exclude URLs by regex, crawl subdomains, ignore query strings
- **Ctrl+C friendly** – stops cleanly and still saves everything collected so far
- Works as a **command-line tool** or as an **importable Python module**

## Requirements

- Python **3.9+**
- Dependencies in [`requirements.txt`](requirements.txt): `requests`, `beautifulsoup4`, `lxml` (optional)

## Installation

```bash
git clone https://github.com/<your-username>/<your-repo>.git
cd <your-repo>

python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

## Usage

```bash
# Crawl a whole site (up to 1000 pages) and save to crawl_results.json
python crawler.py https://example.com

# Only 2 clicks deep, save as CSV
python crawler.py https://example.com --max-depth 2 --output links.csv

# No page limit, more workers, shorter delay (only for sites that can take it)
python crawler.py https://example.com --max-pages 0 --workers 10 --delay 0.1

# Only crawl the /docs/ section, skip tag and pagination pages
python crawler.py https://example.com --include /docs/ --exclude /tag/ --exclude "page=\d+"

# Also visit every external link once to get its real title and HTTP status
python crawler.py https://example.com --external-titles -o all_links.csv

# Intranet site with a self-signed certificate
python crawler.py https://intranet.local --insecure
```

The scheme is optional: `python crawler.py example.com` is treated as `https://example.com`.

### Options

| Option | Default | Description |
|---|---|---|
| `url` | – | Start URL |
| `-o`, `--output FILE` | `crawl_results.json` | Output file. Format is chosen by extension: `.json` or `.csv` |
| `-d`, `--max-depth N` | no limit | Follow links at most N clicks from the start page (`0` = start page only) |
| `-n`, `--max-pages N` | `1000` | Stop after fetching N pages (`0` = no limit) |
| `-w`, `--workers N` | `5` | Number of parallel requests |
| `--delay SECONDS` | `0.5` | Minimum pause between two requests to the same host |
| `--timeout SECONDS` | `15` | Timeout per request |
| `--retries N` | `2` | Retries for timeouts, 429 and 5xx responses |
| `--user-agent STRING` | `Mozilla/5.0 (compatible; LinkCrawler/1.0.0)` | User-Agent header |
| `--include-subdomains` | off | Also crawl subdomains (e.g. `blog.example.com`) |
| `--include REGEX` | – | Only crawl URLs matching this regex (repeatable) |
| `--exclude REGEX` | – | Never crawl URLs matching this regex (repeatable) |
| `--ignore-query` | off | Drop `?query` strings – stops endless calendar/filter/sort pages |
| `--ignore-robots` | off | Don't obey `robots.txt` – only for sites you own or may crawl |
| `--external-titles` | off | Request each external link once to get its real title and status |
| `--insecure` | off | Skip TLS certificate verification |
| `-v`, `--verbose` / `-q`, `--quiet` | – | More / less console output |

Notes:

- `www.example.com` and `example.com` are treated as the same site.
- `--include` / `--exclude` decide which pages are **crawled**. Every link is still **recorded** in the results.
- Links deeper than `--max-depth` are recorded (with their link text as title) but not fetched.
- Exit codes: `0` success, `1` the start page couldn't be crawled, `2` invalid arguments, `130` stopped with Ctrl+C (results are still saved).

## Output

Each unique URL appears once, in the order it was discovered.

| Column | Meaning |
|---|---|
| `url` | The link (normalised: no `#fragment`, lower-case host, no default port) |
| `title` | Page `<title>` if the page was fetched, otherwise the link text |
| `title_source` | `page`, `anchor` (link text) or empty |
| `status` | HTTP status code; for redirects, the redirect code (301, 302 …) |
| `internal` | `True` if the link is on the crawled site |
| `fetched` | `True` if the crawler requested this URL |
| `depth` | Clicks away from the start page |
| `found_on` | First page that linked to this URL |
| `redirected_to` | Where the URL redirected to, if it did |
| `content_type` | e.g. `text/html`, `application/pdf` |
| `error` | Network error, `blocked by robots.txt`, … |

JSON output adds a summary at the top:

```json
{
  "start_url": "https://example.com/",
  "started_at": "2026-10-29T04:15:02+00:00",
  "finished_at": "2026-10-29T04:15:41+00:00",
  "pages_fetched": 74,
  "total_links": 312,
  "internal_links": 241,
  "external_links": 71,
  "broken_links": 2,
  "interrupted": false,
  "crawler_version": "1.0.0",
  "elapsed_seconds": 38.6,
  "links": [
    {
      "url": "https://example.com/about",
      "title": "About Us",
      "title_source": "page",
      "status": 200,
      "internal": true,
      "fetched": true,
      "depth": 1,
      "found_on": "https://example.com/",
      "redirected_to": "",
      "content_type": "text/html",
      "error": ""
    }
  ]
}
```

## Use it from Python

```python
from crawler import Crawler, save_results

crawler = Crawler("https://example.com", max_depth=2, max_pages=200, workers=4)
records = crawler.run()

for link in records:
    if link.internal and link.status == 200:
        print(link.url, "-", link.title)

save_results("links.csv", records, crawler.summary())
```

## How it works

1. Starts at the given URL and fetches pages **breadth-first** (nearest pages first), using a thread pool.
2. For each HTML page it reads the title (`<title>` → `og:title` → first `<h1>`) and every `<a href>` / `<area href>`.
3. Links are made absolute (respecting `<base href>`), normalised and de-duplicated.
4. Links on the same site are queued for crawling; external links and file links are recorded only.
5. Redirects, broken links and errors are recorded instead of stopping the crawl.

## Crawl responsibly

- Only crawl sites you are allowed to crawl, and check their terms of service.
- Keep `robots.txt` enabled and the delay reasonable – a crawler that hammers a server can get your IP blocked.
- Use `--max-pages` and `--max-depth` to keep crawls small while testing.

## Limitations

- Pages that build their links with JavaScript (many React/Vue/Angular single-page apps) show few or no links, because the crawler reads the HTML the server sends and doesn't run JavaScript. A headless browser (e.g. Playwright) is needed for those.
- Pages behind a login are not crawled.
- Only the first 5 MB of a page is parsed.

## Project structure

```text
.
├── crawler.py        # the crawler (CLI + importable module)
├── requirements.txt  # Python dependencies
├── README.md
└── .gitignore
```
