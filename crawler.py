#!/usr/bin/env python3
"""
Link Crawler - crawl a website recursively and collect every link with its title.

    python crawler.py https://example.com
    python crawler.py https://example.com --max-depth 2 --output links.csv
    python crawler.py example.com --max-pages 0 --workers 8 --delay 0.2

Run ``python crawler.py --help`` to see every option, or ``python gui.py``
for the desktop app.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import posixpath
import re
import sys
import threading
import time
import warnings
from collections import deque
from collections.abc import Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests
import urllib3
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:  # lxml is faster and copes better with broken HTML, but it is optional.
    import lxml.etree as _lxml_etree
except ImportError:  # pragma: no cover - depends on what is installed
    _lxml_etree = None
HTML_PARSER = "lxml" if _lxml_etree is not None else "html.parser"

try:  # Hide BeautifulSoup's harmless "this looks like XML / a URL" warnings.
    from bs4 import MarkupResemblesLocatorWarning, XMLParsedAsHTMLWarning

    warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except ImportError:  # pragma: no cover - older BeautifulSoup versions
    pass

__version__ = "1.1.0"

log = logging.getLogger("crawler")

ROBOTS_AGENT = "LinkCrawler"  # name matched against "User-agent:" lines in robots.txt
DEFAULT_USER_AGENT = f"Mozilla/5.0 (compatible; {ROBOTS_AGENT}/{__version__})"
ROBOTS_BLOCKED = "blocked by robots.txt"
CANCELLED = "cancelled"
MAX_PAGE_BYTES = 5 * 1024 * 1024  # never download more than 5 MB of a single page
HTML_CONTENT_TYPES = {"", "text/html", "application/xhtml+xml"}

# Links to files like these are recorded in the results but never downloaded.
SKIP_EXTENSIONS = frozenset(
    """
    .7z .apk .avi .bmp .bz2 .css .csv .dmg .doc .docx .eot .epub .exe .flv .gif .gz
    .ico .iso .jpeg .jpg .js .json .m4a .m4v .mkv .mov .mp3 .mp4 .mpeg .msi .odt .ods
    .ogg .otf .pdf .png .ppt .pptx .rar .rss .svg .tar .tgz .tif .tiff .ttf .txt .wav
    .webm .webp .wmv .woff .woff2 .xls .xlsx .xml .xz .zip
    """.split()
)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class LinkRecord:
    """One unique URL found during the crawl (one row in the output file)."""

    url: str
    title: str = ""            # page <title>, or the link text if the page wasn't fetched
    title_source: str = ""     # "page", "anchor" or "" (no title found)
    status: int | None = None  # HTTP status (the first response, if it was a redirect)
    internal: bool = True      # same website as the start URL?
    fetched: bool = False      # did the crawler actually request this URL?
    depth: int = 0             # number of clicks away from the start URL
    found_on: str = ""         # first page that linked to this URL
    redirected_to: str = ""    # final URL, when the request was redirected
    content_type: str = ""     # e.g. text/html, application/pdf
    error: str = ""            # network error, "blocked by robots.txt", ...

    @property
    def broken(self) -> bool:
        """True if the link returned an HTTP error (4xx/5xx) or could not be reached."""
        if (self.status or 0) >= 400:
            return True
        return bool(self.error) and self.error not in (ROBOTS_BLOCKED, CANCELLED)


@dataclass
class FetchResult:
    """What a worker thread brings back after requesting one URL."""

    url: str
    final_url: str = ""                 # set only when the request was redirected
    status: int | None = None           # status of the final response
    redirect_status: int | None = None  # status of the first response (301, 302, ...)
    content_type: str = ""
    title: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)  # (url, link text)
    error: str = ""
    skipped: bool = False               # not requested (robots.txt or shutting down)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def clean_text(text: str | None) -> str:
    """Collapse runs of whitespace (newlines, tabs, &nbsp;) into single spaces."""
    return " ".join(text.split()) if text else ""


def strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_url(url: str, drop_query: bool = False) -> str:
    """Return a canonical form of *url* so the same page is never visited twice.

    Removes the #fragment, lower-cases scheme and host, removes default ports
    (:80 / :443), uses "/" for an empty path and optionally drops the ?query.
    Raises ValueError for malformed URLs (for example an invalid port).
    """
    url, _fragment = urldefrag(url.strip())
    parts = urlparse(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if ":" in host:  # IPv6 address
        host = f"[{host}]"
    port = parts.port
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    query = "" if drop_query else parts.query
    return urlunparse((scheme, host, parts.path or "/", parts.params, query, ""))


def has_skipped_extension(url: str) -> bool:
    """True for links to images, PDFs, archives and other non-HTML files."""
    return posixpath.splitext(urlparse(url).path)[1].lower() in SKIP_EXTENSIONS


def describe_error(exc: Exception) -> str:
    """Turn a requests exception into a short, readable message."""
    if isinstance(exc, requests.exceptions.SSLError):
        kind = "SSL error (self-signed certificate? use --insecure / 'Skip SSL certificate check')"
    elif isinstance(exc, requests.exceptions.Timeout):
        kind = "timeout"
    elif isinstance(exc, requests.exceptions.ConnectionError):
        kind = "connection error"
    elif isinstance(exc, requests.exceptions.TooManyRedirects):
        kind = "too many redirects"
    else:
        kind = type(exc).__name__
    return f"{kind}: {exc}"[:300]


def read_limited(response: requests.Response, limit: int) -> bytes:
    """Read at most *limit* bytes of a streamed response body."""
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_content(chunk_size=65536):
        chunks.append(chunk)
        size += len(chunk)
        if size >= limit:
            log.debug("%s is larger than %d bytes; only the first part is parsed.", response.url, limit)
            break
    return b"".join(chunks)


def extract_title(soup: BeautifulSoup) -> str:
    """Page title: <title>, then og:title / twitter:title, then the first <h1>."""
    title_tag = (soup.head or soup).find("title")
    if title_tag:
        title = clean_text(title_tag.get_text())
        if title:
            return title
    for attrs in ({"property": "og:title"}, {"name": "twitter:title"}):
        meta = soup.find("meta", attrs=attrs)
        if meta and meta.get("content"):
            title = clean_text(meta["content"])
            if title:
                return title
    h1 = soup.find("h1")
    return clean_text(h1.get_text(" ")) if h1 else ""


def link_text(tag) -> str:
    """Visible text of a link, falling back to title/aria-label/alt attributes."""
    text = clean_text(tag.get_text(" "))
    if not text:
        text = clean_text(tag.get("title") or tag.get("aria-label") or tag.get("alt"))
    if not text:
        img = tag.find("img", alt=True)
        if img:
            text = clean_text(img["alt"])
    return text


def extract_links(soup: BeautifulSoup, page_url: str, drop_query: bool = False) -> list[tuple[str, str]]:
    """Return every unique http(s) link on the page as (absolute_url, link_text)."""
    base = page_url
    base_tag = soup.find("base", href=True)
    if base_tag:
        base = urljoin(page_url, base_tag["href"].strip())

    found: dict[str, str] = {}
    for tag in soup.find_all(["a", "area"], href=True):
        href = tag["href"].strip()
        if not href or href.startswith("#"):
            continue
        try:
            absolute = urljoin(base, href)
            if urlparse(absolute).scheme not in ("http", "https"):
                continue  # mailto:, tel:, javascript:, data:, ...
            url = normalize_url(absolute, drop_query)
        except ValueError:
            continue  # malformed URL
        text = link_text(tag)
        if url not in found or (text and not found[url]):
            found[url] = text
    return list(found.items())


def build_session(user_agent: str, retries: int, verify_ssl: bool) -> requests.Session:
    """A requests Session that retries temporary failures with back-off."""
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
    )
    session.verify = verify_ssl
    return session


class PoliteDelay:
    """Keeps at least `delay` seconds between requests to the same host,
    no matter how many worker threads are running."""

    def __init__(self, delay: float):
        self.delay = delay
        self._next_slot: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, url: str) -> None:
        if self.delay <= 0:
            return
        host = urlparse(url).netloc
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot.get(host, 0.0))
            self._next_slot[host] = slot + self.delay
        if slot > now:
            time.sleep(slot - now)


class RobotsCache:
    """Downloads robots.txt once per site and answers "may I fetch this URL?"."""

    def __init__(self, agent: str, timeout: float):
        self.agent = agent
        self.timeout = timeout
        self._parsers: dict[str, RobotFileParser] = {}
        self._origin_locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    def allowed(self, url: str, session: requests.Session) -> bool:
        return self._parser_for(url, session).can_fetch(self.agent, url)

    def crawl_delay(self, url: str, session: requests.Session) -> float | None:
        delay = self._parser_for(url, session).crawl_delay(self.agent)
        return float(delay) if delay else None

    def _parser_for(self, url: str, session: requests.Session) -> RobotFileParser:
        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            parser = self._parsers.get(origin)
            if parser is not None:
                return parser
            origin_lock = self._origin_locks.setdefault(origin, threading.Lock())
        with origin_lock:  # only one thread downloads a given robots.txt
            with self._lock:
                parser = self._parsers.get(origin)
            if parser is None:
                parser = self._download(origin, session)
                with self._lock:
                    self._parsers[origin] = parser
        return parser

    def _download(self, origin: str, session: requests.Session) -> RobotFileParser:
        parser = RobotFileParser(origin + "/robots.txt")
        try:
            response = session.get(origin + "/robots.txt", timeout=self.timeout)
        except requests.RequestException:
            parser.allow_all = True  # unreachable: the page request will report the real error
            return parser
        # Follows RFC 9309: 4xx means "no rules", 5xx means "assume everything is disallowed".
        if response.status_code >= 500:
            log.warning("robots.txt on %s returned HTTP %d; treating the site as disallowed.",
                        origin, response.status_code)
            parser.disallow_all = True
        elif response.status_code >= 400:
            parser.allow_all = True
        else:
            parser.parse(response.text.splitlines())
        return parser


# --------------------------------------------------------------------------- #
# Crawler
# --------------------------------------------------------------------------- #
class Crawler:
    """Breadth-first website crawler.

    Example:
        crawler = Crawler("https://example.com", max_depth=2)
        records = crawler.run()
        save_results("links.csv", records, crawler.summary())
    """

    def __init__(
        self,
        start_url: str,
        *,
        max_depth: int | None = None,
        max_pages: int | None = 1000,
        workers: int = 5,
        delay: float = 0.5,
        timeout: float = 15.0,
        retries: int = 2,
        user_agent: str = DEFAULT_USER_AGENT,
        include_subdomains: bool = False,
        include: Sequence[str | re.Pattern[str]] = (),
        exclude: Sequence[str | re.Pattern[str]] = (),
        ignore_query: bool = False,
        respect_robots: bool = True,
        external_titles: bool = False,
        verify_ssl: bool = True,
    ):
        start_url = start_url.strip()
        if "://" not in start_url:
            start_url = "https://" + start_url
        try:
            self.start_url = normalize_url(start_url, ignore_query)
        except ValueError as exc:
            raise ValueError(f"Invalid start URL {start_url!r}: {exc}") from None
        parts = urlparse(self.start_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"Invalid start URL {start_url!r}: expected http(s)://host/...")
        if workers < 1:
            raise ValueError("workers must be at least 1")

        self.base_host = strip_www(parts.hostname)
        self.max_depth = max_depth
        self.max_pages = max_pages or None  # 0 or None means "no limit"
        self.workers = workers
        self.timeout = timeout
        self.retries = retries
        self.user_agent = user_agent
        self.include_subdomains = include_subdomains
        self.include = [re.compile(p) if isinstance(p, str) else p for p in include]
        self.exclude = [re.compile(p) if isinstance(p, str) else p for p in exclude]
        self.ignore_query = ignore_query
        self.external_titles = external_titles
        self.verify_ssl = verify_ssl

        self.limiter = PoliteDelay(delay)
        self.robots = RobotsCache(ROBOTS_AGENT, timeout) if respect_robots else None

        self.records: dict[str, LinkRecord] = {}  # url -> record, in discovery order
        self.pages_fetched = 0
        self.interrupted = False
        self.started_at = ""
        self.finished_at = ""
        self._scheduled: set[str] = set()  # URLs already queued for crawling
        self._stop = threading.Event()
        self._local = threading.local()

    # ---- public API ------------------------------------------------------ #
    def run(self) -> list[LinkRecord]:
        """Crawl the site and return every unique link found, in discovery order.

        A running crawl can be ended early with stop() (from another thread) or
        Ctrl+C; the links collected up to that point are still returned.
        """
        self.started_at = utc_now()
        pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="crawler")
        try:
            self._check_robots()
            if not self._stop.is_set():
                self._crawl(pool)
            if self.external_titles and not self._stop.is_set():
                self._fetch_external_titles(pool)
        except KeyboardInterrupt:
            self._stop.set()
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
            self.finished_at = utc_now()
        if self._stop.is_set():
            self.interrupted = True
            log.warning("Stopped - keeping the %d links collected so far.", len(self.records))
        return list(self.records.values())

    def stop(self) -> None:
        """Ask a running crawl to stop as soon as possible. Safe to call from any thread."""
        self._stop.set()

    @property
    def links_found(self) -> int:
        """Number of unique links collected so far (handy for progress displays)."""
        return len(self.records)

    def is_internal(self, url: str) -> bool:
        """Does *url* belong to the website being crawled?"""
        host = strip_www((urlparse(url).hostname or "").lower())
        if host == self.base_host:
            return True
        return self.include_subdomains and host.endswith("." + self.base_host)

    def summary(self) -> dict:
        records = list(self.records.values())
        return {
            "start_url": self.start_url,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "pages_fetched": self.pages_fetched,
            "total_links": len(records),
            "internal_links": sum(1 for r in records if r.internal),
            "external_links": sum(1 for r in records if not r.internal),
            "broken_links": sum(1 for r in records if r.broken),
            "interrupted": self.interrupted,
            "crawler_version": __version__,
        }

    # ---- crawl loop -------------------------------------------------------- #
    def _check_robots(self) -> None:
        if self.robots is None:
            return
        session = self._session()
        delay = self.robots.crawl_delay(self.start_url, session)
        if delay and delay > self.limiter.delay:
            log.info("robots.txt asks for %.1fs between requests - using that.", delay)
            self.limiter.delay = delay
        if not self.robots.allowed(self.start_url, session):
            log.warning("robots.txt does not allow crawling %s. Turn off robots.txt checking only "
                        "if you own the site or have permission.", self.start_url)

    def _crawl(self, pool: ThreadPoolExecutor) -> None:
        start = self.start_url
        self.records[start] = LinkRecord(url=start, internal=True)
        self._scheduled.add(start)
        queue: deque[tuple[str, int]] = deque([(start, 0)])
        in_flight: dict[Future, tuple[str, int]] = {}

        while (queue or in_flight) and not self._stop.is_set():
            # Keep every worker busy, without going over the page limit.
            while queue and len(in_flight) < self.workers and not self._page_limit_reached(len(in_flight)):
                url, depth = queue.popleft()
                if self.records[url].fetched:  # already fetched as a redirect target
                    continue
                in_flight[pool.submit(self._safe_fetch, url, True)] = (url, depth)
            if not in_flight:
                break
            # The short timeout keeps Ctrl+C responsive (especially on Windows).
            done, _ = wait(in_flight, timeout=0.5, return_when=FIRST_COMPLETED)
            for future in done:
                _url, depth = in_flight.pop(future)
                self._handle_page(future.result(), depth, queue)

        if queue and not self._stop.is_set():
            log.info("Reached the page limit (%d); %d queued pages were not fetched.",
                     self.max_pages, len(queue))

    def _page_limit_reached(self, in_flight: int = 0) -> bool:
        return self.max_pages is not None and self.pages_fetched + in_flight >= self.max_pages

    def _handle_page(self, result: FetchResult, depth: int, queue: deque) -> None:
        """Record what a worker fetched and queue the new links it found."""
        record = self.records[result.url]
        if result.skipped:
            record.error = result.error
            log.debug("Skipped %s (%s)", result.url, result.error)
            return

        self.pages_fetched += 1
        self._apply(record, result)
        page_url = result.url

        if result.final_url and result.final_url != result.url:
            if depth == 0 and not self.is_internal(result.final_url):
                # e.g. example.com -> example.org: treat the new host as "the site".
                self.base_host = strip_www(urlparse(result.final_url).hostname or "")
                log.info("Start URL redirects to %s - crawling that site instead.", self.base_host)
            if not self.is_internal(result.final_url):
                self._log_page(record, depth)
                return  # redirected to another site: don't follow its links
            target = self.records.get(result.final_url)
            if target is None:
                target = LinkRecord(url=result.final_url, internal=True, depth=depth, found_on=result.url)
                self.records[result.final_url] = target
            if not target.fetched:
                self._apply(target, result, final=True)
            self._scheduled.add(result.final_url)
            page_url = result.final_url

        self._log_page(record, depth)
        if result.error or result.status is None or result.status >= 400:
            if depth == 0:
                log.warning("Could not fetch the start URL %s (%s).", result.url,
                            result.error or f"HTTP {result.status}")
            return

        can_go_deeper = self.max_depth is None or depth < self.max_depth
        for url, text in result.links:
            internal = self.is_internal(url)
            link = self.records.get(url)
            if link is None:
                link = LinkRecord(url=url, internal=internal, depth=depth + 1, found_on=page_url)
                self.records[url] = link
            if text and not link.title:
                link.title, link.title_source = text, "anchor"
            if internal and can_go_deeper and url not in self._scheduled and self._should_crawl(url):
                self._scheduled.add(url)
                queue.append((url, depth + 1))

    def _fetch_external_titles(self, pool: ThreadPoolExecutor) -> None:
        """Request each external link once to learn its real title and status."""
        targets = [
            r for r in self.records.values()
            if not r.internal and not r.fetched and not has_skipped_extension(r.url)
        ]
        if not targets:
            return
        log.info("Fetching titles of %d external links...", len(targets))
        futures = {pool.submit(self._safe_fetch, r.url, False): r for r in targets}
        pending = set(futures)
        finished = 0
        while pending and not self._stop.is_set():
            done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
            for future in done:
                finished += 1
                record, result = futures[future], future.result()
                if result.skipped:
                    record.error = result.error
                    continue
                self._apply(record, result)
                detail = record.title if record.title_source == "page" else record.error
                log.info("[ext %d/%d] %s  %s%s", finished, len(targets),
                         record.status if record.status is not None else "ERR",
                         record.url, f"  | {detail}" if detail else "")

    @staticmethod
    def _apply(record: LinkRecord, result: FetchResult, final: bool = False) -> None:
        """Copy what was learned from an HTTP request onto a record."""
        record.fetched = True
        record.error = result.error
        record.content_type = result.content_type
        redirected = bool(result.final_url) and result.final_url != result.url
        if final or not redirected:
            record.status = result.status
        else:
            record.status = result.redirect_status
            record.redirected_to = result.final_url
        if result.title:
            record.title, record.title_source = result.title, "page"

    def _should_crawl(self, url: str) -> bool:
        if has_skipped_extension(url):
            return False
        if self.include and not any(p.search(url) for p in self.include):
            return False
        return not any(p.search(url) for p in self.exclude)

    def _log_page(self, record: LinkRecord, depth: int) -> None:
        limit = f"/{self.max_pages}" if self.max_pages else ""
        status = record.status if record.status is not None else "ERR"
        redirect = f" -> {record.redirected_to}" if record.redirected_to else ""
        detail = record.title if record.title_source == "page" else record.error
        log.info("[%d%s] depth %d  %s  %s%s%s", self.pages_fetched, limit, depth, status,
                 record.url, redirect, f"  | {detail}" if detail else "")

    # ---- HTTP (runs in worker threads) ------------------------------------ #
    def _session(self) -> requests.Session:
        """Each thread gets its own Session, because Sessions aren't thread-safe."""
        session = getattr(self._local, "session", None)
        if session is None:
            session = build_session(self.user_agent, self.retries, self.verify_ssl)
            self._local.session = session
        return session

    def _safe_fetch(self, url: str, parse_links: bool) -> FetchResult:
        try:
            return self._fetch(url, parse_links)
        except Exception as exc:  # one bad page must never stop the whole crawl
            log.debug("Unexpected error on %s", url, exc_info=True)
            return FetchResult(url=url, error=f"unexpected error: {exc}"[:300])

    def _fetch(self, url: str, parse_links: bool) -> FetchResult:
        result = FetchResult(url=url)
        if self._stop.is_set():
            result.skipped, result.error = True, CANCELLED
            return result

        session = self._session()
        if self.robots is not None and not self.robots.allowed(url, session):
            result.skipped, result.error = True, ROBOTS_BLOCKED
            return result

        self.limiter.wait(url)
        try:
            response = session.get(url, timeout=self.timeout, stream=True)
        except requests.RequestException as exc:
            result.error = describe_error(exc)
            return result

        with response:
            result.status = response.status_code
            if response.history:  # the request was redirected
                result.redirect_status = response.history[0].status_code
                result.final_url = normalize_url(response.url, self.ignore_query)
            content_type = response.headers.get("Content-Type", "")
            result.content_type = content_type.split(";", 1)[0].strip().lower()
            if response.status_code >= 400 or result.content_type not in HTML_CONTENT_TYPES:
                return result  # nothing to parse, so don't download the body
            try:
                body = read_limited(response, MAX_PAGE_BYTES)
            except requests.RequestException as exc:
                result.error = describe_error(exc)
                return result

        # Use the charset from the HTTP header if there is one; otherwise let
        # BeautifulSoup detect it from <meta charset> or the bytes themselves.
        encoding = response.encoding if "charset=" in content_type.lower() else None
        soup = BeautifulSoup(body, HTML_PARSER, from_encoding=encoding)
        result.title = extract_title(soup)
        if parse_links:
            result.links = extract_links(soup, response.url, self.ignore_query)
        return result


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def save_results(path: str | Path, records: list[LinkRecord], meta: dict | None = None) -> Path:
    """Write the records to a .csv or .json file (chosen by the file extension)."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in (".csv", ".json"):
        raise ValueError("Output file must end in .csv or .json")
    path.parent.mkdir(parents=True, exist_ok=True)

    if suffix == ".csv":
        columns = [f.name for f in fields(LinkRecord)]
        # utf-8-sig adds a BOM so Excel displays non-Latin titles (Bengali, Arabic, ...) correctly.
        with path.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns)
            writer.writeheader()
            writer.writerows(asdict(r) for r in records)
    else:
        payload = {**(meta or {}), "links": [asdict(r) for r in records]}
        with path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return number


def _non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return number


def _non_negative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return number


def _positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


def _regex(value: str) -> re.Pattern[str]:
    try:
        return re.compile(value)
    except re.error as exc:
        raise argparse.ArgumentTypeError(f"invalid regular expression: {exc}") from None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="crawler.py",
        description="Crawl a website recursively and collect every link with its title.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python crawler.py https://example.com\n"
            "  python crawler.py https://example.com -d 2 -o links.csv\n"
            "  python crawler.py https://example.com --exclude /tag/ --exclude /page/\n"
            "  python crawler.py https://example.com -n 0 --external-titles -o all.json\n"
        ),
    )
    parser.add_argument("url", help="start URL, e.g. https://example.com")
    parser.add_argument("-o", "--output", default="crawl_results.json", metavar="FILE",
                        help="output file, .json or .csv (default: %(default)s)")
    parser.add_argument("-d", "--max-depth", type=_non_negative_int, default=None, metavar="N",
                        help="follow links at most N clicks away from the start page "
                             "(0 = start page only; default: no limit)")
    parser.add_argument("-n", "--max-pages", type=_non_negative_int, default=1000, metavar="N",
                        help="stop after fetching N pages, 0 = no limit (default: %(default)s)")
    parser.add_argument("-w", "--workers", type=_positive_int, default=5, metavar="N",
                        help="parallel requests (default: %(default)s)")
    parser.add_argument("--delay", type=_non_negative_float, default=0.5, metavar="SECONDS",
                        help="minimum pause between requests to the same host (default: %(default)s)")
    parser.add_argument("--timeout", type=_positive_float, default=15.0, metavar="SECONDS",
                        help="per-request timeout (default: %(default)s)")
    parser.add_argument("--retries", type=_non_negative_int, default=2, metavar="N",
                        help="retries for timeouts, 429 and 5xx responses (default: %(default)s)")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, metavar="STRING",
                        help="User-Agent header to send")
    parser.add_argument("--include-subdomains", action="store_true",
                        help="also crawl subdomains (e.g. blog.example.com when starting at example.com)")
    parser.add_argument("--include", action="append", type=_regex, default=[], metavar="REGEX",
                        help="only crawl URLs matching this regex; repeatable")
    parser.add_argument("--exclude", action="append", type=_regex, default=[], metavar="REGEX",
                        help="never crawl URLs matching this regex; repeatable")
    parser.add_argument("--ignore-query", action="store_true",
                        help="drop ?query strings (avoids endless calendar/filter/sort pages)")
    parser.add_argument("--ignore-robots", action="store_true",
                        help="don't obey robots.txt (only for sites you own or may crawl)")
    parser.add_argument("--external-titles", action="store_true",
                        help="also request each external link once to get its real title and status")
    parser.add_argument("--insecure", action="store_true",
                        help="skip TLS certificate checks (e.g. intranet sites with self-signed certs)")
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument("-v", "--verbose", action="store_true", help="show debug output")
    verbosity.add_argument("-q", "--quiet", action="store_true", help="only show warnings and errors")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def setup_logging(verbose: bool, quiet: bool) -> None:
    for stream in (sys.stdout, sys.stderr):  # never crash on characters the console can't show
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    level = logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    # Retries and failures are reported by the crawler itself, so keep libraries quiet.
    for noisy in ("urllib3", "charset_normalizer", "chardet"):
        logging.getLogger(noisy).setLevel(logging.ERROR)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose, args.quiet)

    output = Path(args.output)
    if output.suffix.lower() not in (".json", ".csv"):
        log.error("--output must end in .json or .csv (got %s)", output.name)
        return 2
    if args.insecure:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    try:
        crawler = Crawler(
            args.url,
            max_depth=args.max_depth,
            max_pages=args.max_pages,
            workers=args.workers,
            delay=args.delay,
            timeout=args.timeout,
            retries=args.retries,
            user_agent=args.user_agent,
            include_subdomains=args.include_subdomains,
            include=args.include,
            exclude=args.exclude,
            ignore_query=args.ignore_query,
            respect_robots=not args.ignore_robots,
            external_titles=args.external_titles,
            verify_ssl=not args.insecure,
        )
    except ValueError as exc:
        log.error("%s", exc)
        return 2

    log.info("Crawling %s  (max depth: %s, max pages: %s, workers: %d)",
             crawler.start_url,
             "unlimited" if args.max_depth is None else args.max_depth,
             args.max_pages or "unlimited", args.workers)

    started = time.monotonic()
    records = crawler.run()
    meta = crawler.summary()
    meta["elapsed_seconds"] = round(time.monotonic() - started, 1)

    try:
        save_results(output, records, meta)
    except OSError as exc:
        log.error("Could not write %s: %s", output, exc)
        return 1

    log.info("Done in %.1fs: %d pages fetched, %d unique links (%d internal, %d external, %d broken).",
             meta["elapsed_seconds"], meta["pages_fetched"], meta["total_links"],
             meta["internal_links"], meta["external_links"], meta["broken_links"])
    log.info("Results saved to %s", output.resolve())

    if crawler.interrupted:
        return 130
    start = crawler.records.get(crawler.start_url)
    if start is None or not start.fetched or start.error or (start.status or 0) >= 400:
        return 1  # the start page itself could not be crawled
    return 0


if __name__ == "__main__":
    sys.exit(main())
