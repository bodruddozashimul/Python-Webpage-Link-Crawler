#!/usr/bin/env python3
"""
Link Crawler - desktop app.

    python gui.py                        # Windows: pythonw gui.py hides the console window
    python gui.py https://example.com    # opens with the URL already filled in

1. Type the website address.
2. Change the options if you like.
3. Press "Start crawl" and choose where to save the results and the file name.

The crawling is done by crawler.py, which must be in the same folder.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlparse

try:
    import urllib3

    from crawler import ROBOTS_BLOCKED, Crawler, LinkRecord, __version__, save_results
except ImportError as exc:  # shown in a dialog by main(): a double-clicked app has no console
    IMPORT_ERROR: ImportError | None = exc
else:
    IMPORT_ERROR = None

APP_NAME = "Link Crawler"
POLL_MS = 100          # how often the window checks for messages from the crawl thread
TICK_MS = 500          # how often the progress line is refreshed
MAX_LOG_LINES = 5000   # older log lines are dropped to keep the window responsive

log = logging.getLogger("crawler.gui")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class QueueLogHandler(logging.Handler):
    """Passes log messages from any thread to the window through a queue."""

    def __init__(self, events: queue.Queue):
        super().__init__(level=logging.INFO)
        self.events = events
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.events.put(("log", record.levelno, self.format(record)))
        except Exception:  # pragma: no cover - logging must never crash the app
            self.handleError(record)


def parse_number(text: str, label: str, cast=int, minimum: float = 0,
                 maximum: float | None = None, allow_blank: bool = False):
    """Read a number from an input box, with a friendly error message."""
    text = text.strip()
    if not text:
        if allow_blank:
            return None
        raise ValueError(f"Please enter a value for '{label}'.")
    try:
        value = cast(text)
    except ValueError:
        kind = "a whole number" if cast is int else "a number"
        raise ValueError(f"'{label}' must be {kind}.") from None
    if value < minimum or (maximum is not None and value > maximum):
        limit = f"between {minimum} and {maximum}" if maximum is not None else f"{minimum} or more"
        raise ValueError(f"'{label}' must be {limit}.")
    return value


def parse_regex(text: str, label: str) -> list[re.Pattern[str]]:
    text = text.strip()
    if not text:
        return []
    try:
        return [re.compile(text)]
    except re.error as exc:
        raise ValueError(f"'{label}' is not a valid regular expression:\n{exc}") from None


def default_folder() -> Path:
    documents = Path.home() / "Documents"
    return documents if documents.is_dir() else Path.home()


def suggested_filename(start_url: str, extension: str) -> str:
    """e.g. example.com_20261030-2003.csv"""
    host = re.sub(r"[^A-Za-z0-9._-]+", "_", urlparse(start_url).netloc) or "crawl"
    return f"{host}_{datetime.now():%Y%m%d-%H%M}{extension}"


def check_writable(path: Path) -> str | None:
    """Return a problem description if *path* can't be written (e.g. it's open in Excel)."""
    existed = path.exists()
    try:
        with open(path, "a", encoding="utf-8"):
            pass
    except OSError as exc:
        return exc.strerror or str(exc)
    if not existed:
        try:
            path.unlink()
        except OSError:
            pass
    return None


def open_with_system(path: Path) -> None:
    """Open a file or folder with the default app (Excel, a text editor, Explorer, ...)."""
    if sys.platform.startswith("win"):
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def show_in_folder(path: Path) -> None:
    """Open the folder that contains *path*, with the file selected where possible."""
    if sys.platform.startswith("win"):
        subprocess.Popen(f'explorer /select,"{path}"')
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    else:
        open_with_system(path.parent)


def format_elapsed(seconds: float) -> str:
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def status_text(record: LinkRecord) -> str:
    if record.status is not None:
        return str(record.status)
    if record.error == ROBOTS_BLOCKED:
        return "robots.txt"
    if record.error:
        return "error"
    return ""


def enable_windows_dpi_awareness() -> None:
    """Sharp text on high-resolution Windows screens instead of blurry, scaled-up text."""
    if sys.platform.startswith("win"):
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# The window
# --------------------------------------------------------------------------- #
class CrawlerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.crawler: Crawler | None = None
        self.output_path: Path | None = None
        self.last_folder = default_folder()
        self.started = 0.0
        self.running = False
        self.stopping = False
        self.closing = False
        self.destroyed = False
        self._after_ids: dict[str, str] = {}

        root.title(f"{APP_NAME} {__version__}")
        self._setup_style()
        self._create_variables()
        self._build_ui()
        self._setup_logging()
        self._fit_to_screen()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._after_ids["poll"] = root.after(POLL_MS, self._poll_events)
        self.url_entry.focus_set()

    # ---- setup ------------------------------------------------------------ #
    def _setup_style(self) -> None:
        style = ttk.Style(self.root)
        if style.theme_use() == "default" and "clam" in style.theme_names():
            style.theme_use("clam")  # the Linux default theme looks dated
            for widget_style in ("TEntry", "TSpinbox"):  # make disabled fields look disabled
                style.map(widget_style, fieldbackground=[("disabled", "#ececec"), ("readonly", "#f4f4f4")])
            style.configure("Horizontal.TProgressbar", background="#3b82f6")  # clam's grey bar is hard to see
        base = tkfont.nametofont("TkDefaultFont")
        self.bold_font = base.copy()
        self.bold_font.configure(weight="bold")
        style.configure("Start.TButton", font=self.bold_font, padding=(14, 4))
        style.configure("Hint.TLabel", foreground="#6b7280")
        style.configure("Treeview", rowheight=base.metrics("linespace") + 8)

    def _create_variables(self) -> None:
        self.url_var = tk.StringVar()
        self.depth_var = tk.StringVar(value="")
        self.pages_var = tk.StringVar(value="1000")
        self.workers_var = tk.StringVar(value="5")
        self.delay_var = tk.StringVar(value="0.5")
        self.timeout_var = tk.StringVar(value="15")
        self.include_var = tk.StringVar()
        self.exclude_var = tk.StringVar()
        self.robots_var = tk.BooleanVar(value=True)
        self.subdomains_var = tk.BooleanVar(value=False)
        self.ignore_query_var = tk.BooleanVar(value=False)
        self.external_var = tk.BooleanVar(value=False)
        self.insecure_var = tk.BooleanVar(value=False)
        self.format_var = tk.StringVar(value=".csv")
        self.status_var = tk.StringVar(value="Ready.")
        self.output_var = tk.StringVar(value="You'll choose the folder and file name when you start a crawl.")

    def _fit_to_screen(self) -> None:
        """A comfortable starting size that still fits small or scaled (125-150%) screens."""
        root = self.root
        root.update_idletasks()
        screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
        max_height = int(screen_h * 0.85)
        if root.winfo_reqheight() > max_height:
            self.toggle_options(show=False)  # small screen: give the log and results the room
            root.update_idletasks()
        width = min(max(root.winfo_reqwidth(), 980), int(screen_w * 0.9))
        height = min(max(root.winfo_reqheight(), 780), max_height)
        root.geometry(f"{width}x{height}")
        root.minsize(min(720, width), min(480, height))

    def toggle_options(self, show: bool | None = None) -> None:
        """Show or hide the Options panel (hidden by default on small screens)."""
        if show is None:
            show = not self.options_frame.winfo_manager()
        if show:
            self.options_frame.grid()
        else:
            self.options_frame.grid_remove()
        self.options_button.configure(text="Hide options" if show else "Show options")

    def _setup_logging(self) -> None:
        crawler_log = logging.getLogger("crawler")  # crawler.py and this file log here
        crawler_log.setLevel(logging.INFO)
        crawler_log.addHandler(QueueLogHandler(self.events))
        for noisy in ("urllib3", "charset_normalizer", "chardet"):
            logging.getLogger(noisy).setLevel(logging.ERROR)

    def _build_ui(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(4, weight=1)

        # ---- URL ---------------------------------------------------------- #
        top = ttk.Frame(root, padding=(12, 12, 12, 0))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)
        ttk.Label(top, text="Website URL:", font=self.bold_font).grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.url_entry = ttk.Entry(top, textvariable=self.url_var)
        self.url_entry.grid(row=0, column=1, sticky="ew", ipady=2)
        self.url_entry.bind("<Return>", lambda _event: self.start())
        ttk.Label(top, text="For example https://example.com - press Enter or \"Start crawl\" to begin.",
                  style="Hint.TLabel").grid(row=1, column=1, sticky="w", pady=(2, 0))

        # ---- options ------------------------------------------------------ #
        opts = ttk.LabelFrame(root, text="Options", padding=(12, 6, 12, 8))
        opts.grid(row=1, column=0, sticky="ew", padx=12, pady=(10, 0))
        opts.columnconfigure(6, weight=1)
        self.options_frame = opts

        def field(row: int, col: int, label: str, widget: ttk.Widget) -> ttk.Widget:
            ttk.Label(opts, text=label).grid(row=row, column=col, sticky="w",
                                             padx=(0 if col == 0 else 18, 6), pady=3)
            widget.grid(row=row, column=col + 1, sticky="w", pady=3)
            return widget

        depth = field(0, 0, "Max depth:", ttk.Entry(opts, textvariable=self.depth_var, width=8))
        pages = field(0, 2, "Max pages:", ttk.Entry(opts, textvariable=self.pages_var, width=8))
        workers = field(0, 4, "Parallel requests:",
                        ttk.Spinbox(opts, from_=1, to=20, textvariable=self.workers_var, width=6))
        delay = field(1, 0, "Delay (seconds):", ttk.Entry(opts, textvariable=self.delay_var, width=8))
        timeout = field(1, 2, "Timeout (seconds):", ttk.Entry(opts, textvariable=self.timeout_var, width=8))
        ttk.Label(opts, text="Max depth: blank = no limit, 0 = start page only.   Max pages: 0 = no limit.",
                  style="Hint.TLabel").grid(row=2, column=0, columnspan=7, sticky="w", pady=(0, 6))

        ttk.Label(opts, text="Only crawl URLs matching:").grid(row=3, column=0, sticky="w", padx=(0, 6), pady=3)
        include = ttk.Entry(opts, textvariable=self.include_var)
        include.grid(row=3, column=1, columnspan=6, sticky="ew", pady=3)
        ttk.Label(opts, text="Skip URLs matching:").grid(row=4, column=0, sticky="w", padx=(0, 6), pady=3)
        exclude = ttk.Entry(opts, textvariable=self.exclude_var)
        exclude.grid(row=4, column=1, columnspan=6, sticky="ew", pady=3)
        ttk.Label(opts, text=r"Optional regular expressions, e.g.  /blog/   or   /tag/|/page/\d+",
                  style="Hint.TLabel").grid(row=5, column=1, columnspan=6, sticky="w", pady=(0, 4))

        checks = ttk.Frame(opts)
        checks.grid(row=6, column=0, columnspan=7, sticky="w", pady=(2, 0))
        check_buttons = []
        for i, (text, var) in enumerate((
            ("Obey robots.txt", self.robots_var),
            ("Include subdomains", self.subdomains_var),
            ("Ignore ?query strings", self.ignore_query_var),
            ("Fetch titles of external links", self.external_var),
            ("Skip SSL certificate check", self.insecure_var),
        )):
            button = ttk.Checkbutton(checks, text=text, variable=var)
            button.grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 24), pady=2)
            check_buttons.append(button)

        # ---- actions ------------------------------------------------------ #
        actions = ttk.Frame(root, padding=(12, 10, 12, 0))
        actions.grid(row=2, column=0, sticky="ew")
        actions.columnconfigure(3, weight=1)
        ttk.Label(actions, text="Save results as:").grid(row=0, column=0, padx=(0, 8))
        csv_radio = ttk.Radiobutton(actions, text="CSV (opens in Excel)", value=".csv", variable=self.format_var)
        json_radio = ttk.Radiobutton(actions, text="JSON", value=".json", variable=self.format_var)
        csv_radio.grid(row=0, column=1, padx=(0, 12))
        json_radio.grid(row=0, column=2)
        self.options_button = ttk.Button(actions, text="Hide options", command=self.toggle_options)
        self.options_button.grid(row=0, column=3, sticky="e", padx=(0, 8))
        self.start_button = ttk.Button(actions, text="Start crawl", style="Start.TButton", command=self.start)
        self.start_button.grid(row=0, column=4, padx=(0, 8))
        self.stop_button = ttk.Button(actions, text="Stop", command=self.stop, state="disabled")
        self.stop_button.grid(row=0, column=5)

        # ---- progress ----------------------------------------------------- #
        progress = ttk.Frame(root, padding=(12, 10, 12, 6))
        progress.grid(row=3, column=0, sticky="ew")
        progress.columnconfigure(1, weight=1)
        self.progress_bar = ttk.Progressbar(progress, mode="determinate", length=170, maximum=100)
        self.progress_bar.grid(row=0, column=0, padx=(0, 10))
        ttk.Label(progress, textvariable=self.status_var).grid(row=0, column=1, sticky="w")

        # ---- log / results tabs ------------------------------------------- #
        self.notebook = ttk.Notebook(root)
        self.notebook.grid(row=4, column=0, sticky="nsew", padx=12)
        self.log_tab = ttk.Frame(self.notebook, padding=4)
        self.results_tab = ttk.Frame(self.notebook, padding=4)
        self.notebook.add(self.log_tab, text="Log")
        self.notebook.add(self.results_tab, text="Results")
        self._build_log(self.log_tab)
        self._build_results(self.results_tab)

        # ---- output file -------------------------------------------------- #
        out = ttk.Frame(root, padding=(12, 8, 12, 12))
        out.grid(row=5, column=0, sticky="ew")
        out.columnconfigure(1, weight=1)
        ttk.Label(out, text="Output file:").grid(row=0, column=0, padx=(0, 8))
        ttk.Entry(out, textvariable=self.output_var, state="readonly").grid(row=0, column=1, sticky="ew")
        self.open_button = ttk.Button(out, text="Open file", command=self.open_output, state="disabled")
        self.open_button.grid(row=0, column=2, padx=(8, 0))
        self.folder_button = ttk.Button(out, text="Show in folder", command=self.show_output_folder,
                                        state="disabled")
        self.folder_button.grid(row=0, column=3, padx=(8, 0))

        # Disabled while a crawl is running.
        self.input_widgets: list[ttk.Widget] = [
            self.url_entry, depth, pages, workers, delay, timeout, include, exclude,
            csv_radio, json_radio, *check_buttons,
        ]

    def _build_log(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        self.log_text = tk.Text(parent, height=8, wrap="none", state="disabled", font="TkFixedFont",
                                relief="flat", borderwidth=0, padx=6, pady=4)
        y_scroll = ttk.Scrollbar(parent, orient="vertical", command=self.log_text.yview)
        x_scroll = ttk.Scrollbar(parent, orient="horizontal", command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        self.log_text.tag_configure("warning", foreground="#b45309")
        self.log_text.tag_configure("error", foreground="#b91c1c")

    def _build_results(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        ttk.Label(parent, text="Double-click a row to open the link.  Click a column header to sort.  "
                               "Right-click or Ctrl+C to copy URLs.",
                  style="Hint.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))

        columns = (
            # id, heading, width, stretch, anchor
            ("title", "Title", 280, True, "w"),
            ("url", "URL", 380, True, "w"),
            ("status", "Status", 80, False, "center"),
            ("type", "Type", 80, False, "center"),
            ("depth", "Depth", 60, False, "center"),
        )
        self.tree = ttk.Treeview(parent, columns=[c[0] for c in columns], show="headings", height=8)
        for col, heading, width, stretch, anchor in columns:
            self.tree.heading(col, text=heading, command=lambda c=col: self._sort_by(c, False))
            self.tree.column(col, width=width, minwidth=50, stretch=stretch, anchor=anchor)
        y_scroll = ttk.Scrollbar(parent, orient="vertical", command=self.tree.yview)
        x_scroll = ttk.Scrollbar(parent, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        y_scroll.grid(row=1, column=1, sticky="ns")
        x_scroll.grid(row=2, column=0, sticky="ew")
        self.tree.tag_configure("broken", foreground="#b91c1c")

        self.menu = tk.Menu(self.tree, tearoff=False)
        self.menu.add_command(label="Open link in browser", command=self._open_selected)
        self.menu.add_command(label="Copy URL", command=self._copy_selected)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-2>" if sys.platform == "darwin" else "<Button-3>", self._show_menu)
        self.tree.bind("<Control-c>", lambda _event: self._copy_selected())
        self.tree.bind("<Command-c>" if sys.platform == "darwin" else "<Control-C>",
                       lambda _event: self._copy_selected())

    # ---- start / stop ----------------------------------------------------- #
    def _read_settings(self) -> dict:
        """Turn the option fields into Crawler() arguments (raises ValueError on bad input)."""
        return {
            "max_depth": parse_number(self.depth_var.get(), "Max depth", allow_blank=True),
            "max_pages": parse_number(self.pages_var.get(), "Max pages"),
            "workers": parse_number(self.workers_var.get(), "Parallel requests", minimum=1, maximum=20),
            "delay": parse_number(self.delay_var.get(), "Delay", cast=float),
            "timeout": parse_number(self.timeout_var.get(), "Timeout", cast=float, minimum=1),
            "include": parse_regex(self.include_var.get(), "Only crawl URLs matching"),
            "exclude": parse_regex(self.exclude_var.get(), "Skip URLs matching"),
            "respect_robots": self.robots_var.get(),
            "include_subdomains": self.subdomains_var.get(),
            "ignore_query": self.ignore_query_var.get(),
            "external_titles": self.external_var.get(),
            "verify_ssl": not self.insecure_var.get(),
        }

    def start(self) -> None:
        if self.running:
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning(APP_NAME, "Please enter the address of the website to crawl.", parent=self.root)
            self.url_entry.focus_set()
            return
        try:
            crawler = Crawler(url, **self._read_settings())
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self.root)
            return

        path = self._ask_output_path(crawler.start_url)
        if path is None:
            return  # the user cancelled the save dialog
        problem = check_writable(path)
        if problem:
            messagebox.showerror(APP_NAME, f"Can't save to\n{path}\n\n{problem}\n\n"
                                 "If the file is open in Excel, close it and try again.", parent=self.root)
            return

        self.crawler = crawler
        self.output_path = path
        self.last_folder = path.parent
        self.format_var.set(path.suffix.lower())
        self.output_var.set(str(path))
        self._clear_log()
        self._clear_results()
        self.notebook.select(self.log_tab)
        self._set_running(True)
        if not crawler.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        log.info("Crawling %s", crawler.start_url)
        log.info("Results will be saved to %s", path)
        self.started = time.monotonic()
        threading.Thread(target=self._crawl_in_background, args=(crawler, path),
                         name="gui-crawl", daemon=True).start()
        self._tick()

    def _ask_output_path(self, start_url: str) -> Path | None:
        """Ask where to save the results and under which file name."""
        extension = self.format_var.get()
        file_types = [("CSV file (opens in Excel)", "*.csv"), ("JSON file", "*.json")]
        if extension == ".json":
            file_types.reverse()
        filename = filedialog.asksaveasfilename(
            parent=self.root,
            title="Where should the results be saved?",
            initialdir=str(self.last_folder),
            initialfile=suggested_filename(start_url, extension),
            defaultextension=extension,
            filetypes=file_types,
        )
        if not filename:
            return None
        path = Path(filename)
        if path.suffix.lower() not in (".csv", ".json"):
            path = path.with_name(path.name + extension)
            if path.exists() and not messagebox.askyesno(
                    APP_NAME, f"{path.name} already exists.\nDo you want to replace it?", parent=self.root):
                return None
        return path

    def stop(self) -> None:
        if self.running and self.crawler is not None and not self.stopping:
            self.stopping = True
            self.crawler.stop()
            self.stop_button.state(["disabled"])
            self.status_var.set("Stopping - saving what was collected so far...")

    def _crawl_in_background(self, crawler: Crawler, path: Path) -> None:
        """Runs in a worker thread; reports back to the window through self.events."""
        try:
            records = crawler.run()
            meta = crawler.summary()
            meta["elapsed_seconds"] = round(time.monotonic() - self.started, 1)
        except Exception as exc:  # pragma: no cover - crawler.run() already handles page errors
            logging.getLogger("crawler").exception("The crawl failed")
            self.events.put(("failed", str(exc)))
            return
        save_error = ""
        try:
            save_results(path, records, meta)
        except OSError as exc:
            save_error = exc.strerror or str(exc)
        self.events.put(("finished", records, meta, path, save_error))

    # ---- updates from the crawl thread ------------------------------------ #
    def _poll_events(self) -> None:
        lines: list[tuple[int, str]] = []
        try:
            while len(lines) < 1000:
                event = self.events.get_nowait()
                if event[0] == "log":
                    lines.append((event[1], event[2]))
                    continue
                self._append_log(lines)
                lines = []
                if event[0] == "finished":
                    self._on_finished(*event[1:])
                elif event[0] == "failed":
                    self._on_failed(event[1])
                if self.destroyed:
                    return
        except queue.Empty:
            pass
        self._append_log(lines)
        self._after_ids["poll"] = self.root.after(POLL_MS, self._poll_events)

    def _tick(self) -> None:
        if not self.running or self.destroyed:
            return
        if not self.stopping and self.crawler is not None:
            crawler = self.crawler
            limit = f" of {crawler.max_pages}" if crawler.max_pages else ""
            self.status_var.set(
                f"Crawling...  {crawler.pages_fetched}{limit} pages fetched  ·  "
                f"{crawler.links_found} links found  ·  {format_elapsed(time.monotonic() - self.started)}"
            )
        self._after_ids["tick"] = self.root.after(TICK_MS, self._tick)

    def _on_finished(self, records: list[LinkRecord], meta: dict, path: Path, save_error: str) -> None:
        stopped = meta["interrupted"]
        self._set_running(False)
        self._fill_results(records)
        self.notebook.select(self.results_tab)

        elapsed = format_elapsed(meta["elapsed_seconds"])
        if stopped:
            self.status_var.set(f"Stopped after {elapsed}  ·  {meta['pages_fetched']} pages fetched  ·  "
                                f"{meta['total_links']} links (partial results).")
        else:
            self.status_var.set(f"Finished in {elapsed}  ·  {meta['pages_fetched']} pages fetched  ·  "
                                f"{meta['total_links']} links ({meta['internal_links']} internal, "
                                f"{meta['external_links']} external, {meta['broken_links']} broken).")

        if self.closing:  # the window was closed during the crawl
            self._destroy()
            return

        if save_error:
            self.output_var.set(f"Not saved - {save_error}")
            log.error("Could not save %s: %s", path, save_error)
            if messagebox.askyesno(APP_NAME, f"Could not save the results to\n{path}\n\n{save_error}\n\n"
                                   "If the file is open in Excel, close it first.\n\n"
                                   "Choose another location?", icon="warning", parent=self.root):
                self._save_elsewhere(records, meta)
            return

        log.info("Results saved to %s", path)
        self._show_saved(path)
        problem = self._start_page_problem()
        if problem:
            messagebox.showwarning(APP_NAME, problem, parent=self.root)
            return
        what = "Stopped - partial results" if stopped else "Done!"
        if messagebox.askyesno(APP_NAME, f"{what} {meta['total_links']} links saved to:\n{path}\n\n"
                               "Open the file now?", parent=self.root):
            self.open_output()

    def _on_failed(self, message: str) -> None:
        self._set_running(False)
        self.status_var.set("The crawl failed - see the log.")
        if self.closing:
            self._destroy()
            return
        messagebox.showerror(APP_NAME, f"The crawl failed:\n{message}", parent=self.root)

    def _start_page_problem(self) -> str:
        """Explain why the start page couldn't be crawled ("" if it was fine)."""
        crawler = self.crawler
        record = crawler.records.get(crawler.start_url) if crawler else None
        if record is None or (record.fetched and not record.error and (record.status or 0) < 400):
            return ""
        if record.error == ROBOTS_BLOCKED:
            return (f"{crawler.start_url} is blocked by the site's robots.txt.\n\n"
                    "Untick 'Obey robots.txt' only if you own the site or have permission to crawl it.")
        if record.error:
            reason = record.error
        elif record.status:
            reason = f"The server answered HTTP {record.status}."
        else:
            return ""  # stopped before the first page was fetched
        return (f"Could not crawl {crawler.start_url}\n\n{reason}\n\n"
                "Check the address and your internet connection.")

    def _save_elsewhere(self, records: list[LinkRecord], meta: dict) -> None:
        while True:
            path = self._ask_output_path(meta["start_url"])
            if path is None:
                return
            try:
                save_results(path, records, meta)
            except OSError as exc:
                if messagebox.askretrycancel(APP_NAME, f"Could not save to\n{path}\n\n{exc.strerror or exc}",
                                             parent=self.root):
                    continue
                return
            log.info("Results saved to %s", path)
            self.last_folder = path.parent
            self._show_saved(path)
            return

    # ---- window helpers --------------------------------------------------- #
    def _set_running(self, running: bool) -> None:
        self.running = running
        self.stopping = False
        for widget in self.input_widgets:
            widget.state(["disabled"] if running else ["!disabled"])
        self.start_button.state(["disabled"] if running else ["!disabled"])
        self.stop_button.state(["!disabled"] if running else ["disabled"])
        for button in (self.open_button, self.folder_button):
            button.state(["disabled"])
        if running:
            self.progress_bar.configure(mode="indeterminate", value=0)
            self.progress_bar.start(12)
        else:  # a full bar when a crawl has finished
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate", value=100 if self.crawler else 0)

    def _show_saved(self, path: Path) -> None:
        self.output_path = path
        self.output_var.set(str(path))
        for button in (self.open_button, self.folder_button):
            button.state(["!disabled"])

    def _append_log(self, lines: list[tuple[int, str]]) -> None:
        if not lines or self.destroyed:
            return
        self.log_text.configure(state="normal")
        for level, text in lines:
            tag = "error" if level >= logging.ERROR else "warning" if level >= logging.WARNING else ""
            self.log_text.insert("end", text + "\n", tag)
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > MAX_LOG_LINES:
            self.log_text.delete("1.0", f"{line_count - MAX_LOG_LINES}.0")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _clear_results(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.notebook.tab(self.results_tab, text="Results")

    def _fill_results(self, records: list[LinkRecord]) -> None:
        self._clear_results()
        for record in records:
            self.tree.insert("", "end", values=(
                record.title, record.url, status_text(record),
                "Internal" if record.internal else "External", record.depth,
            ), tags=("broken",) if record.broken else ())
        self.notebook.tab(self.results_tab, text=f"Results ({len(records)})")

    def _sort_by(self, column: str, descending: bool) -> None:
        def key(item: tuple[str, str]):
            value = item[0]
            return (0, int(value), "") if value.isdigit() else (1, 0, value.lower())

        rows = [(self.tree.set(iid, column), iid) for iid in self.tree.get_children("")]
        rows.sort(key=key, reverse=descending)
        for index, (_value, iid) in enumerate(rows):
            self.tree.move(iid, "", index)
        self.tree.heading(column, command=lambda: self._sort_by(column, not descending))

    def _selected_urls(self) -> list[str]:
        return [self.tree.set(iid, "url") for iid in self.tree.selection()]

    def _on_double_click(self, event: tk.Event) -> None:
        if self.tree.identify_region(event.x, event.y) == "cell":
            self._open_selected()

    def _open_selected(self) -> None:
        urls = self._selected_urls()
        if urls:
            webbrowser.open(urls[0])

    def _copy_selected(self) -> None:
        urls = self._selected_urls()
        if urls:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(urls))

    def _show_menu(self, event: tk.Event) -> None:
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if row not in self.tree.selection():
            self.tree.selection_set(row)
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def open_output(self) -> None:
        if self.output_path is not None:
            self._open(lambda: open_with_system(self.output_path))

    def show_output_folder(self) -> None:
        if self.output_path is not None:
            self._open(lambda: show_in_folder(self.output_path))

    def _open(self, action) -> None:
        try:
            action()
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Could not open it:\n{exc}", parent=self.root)

    def on_close(self) -> None:
        if not self.running or self.closing:  # idle, or closed twice: don't wait any longer
            self._destroy()
        elif messagebox.askyesno(APP_NAME, "A crawl is still running.\n\n"
                                 "Stop it, save what was collected so far and quit?", parent=self.root):
            self.closing = True
            self.stop()  # the window closes once the results are saved

    def _destroy(self) -> None:
        self.destroyed = True
        for after_id in self._after_ids.values():
            try:
                self.root.after_cancel(after_id)
            except tk.TclError:
                pass
        self.root.destroy()


def main() -> int:
    enable_windows_dpi_awareness()
    root = tk.Tk()
    if IMPORT_ERROR is not None:
        root.withdraw()
        messagebox.showerror(APP_NAME, f"A required package is missing:\n{IMPORT_ERROR}\n\n"
                             "Install the requirements first:\n    pip install -r requirements.txt\n\n"
                             "(crawler.py must also be in the same folder as gui.py.)")
        return 1
    app = CrawlerApp(root)
    if len(sys.argv) > 1:
        app.url_var.set(sys.argv[1])
    root.mainloop()
    # After a Stop, requests that were already under way finish in the background.
    # Don't make the user wait for them (up to the timeout) after the window closes.
    if any(t.name.startswith("crawler") and t.is_alive() for t in threading.enumerate()):
        logging.shutdown()
        os._exit(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
