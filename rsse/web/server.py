"""`rsse serve`: the local web UI's HTTP server (spec/09-WEB.md §8).

Standard library only. Everything that decides *what* a response says lives in
`api.py`; this module decides only how requests reach it: routing, the Host
check, one read-only connection per request, the query deadline, the
concurrency cap, and cancellation.
"""

from __future__ import annotations

import ipaddress
import json
import re
import secrets
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from ..query import connect
from . import api

#: The only files served from `static/`. A fixed list rather than a directory
#: walk, so no path from a request ever reaches the filesystem.
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}

#: Sent with every response. The page loads nothing from elsewhere (§1), so
#: the policy can say so, and a script injected through source text -- which
#: the page never inserts as HTML anyway (§8) -- would have nowhere to send
#: anything.
SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data:;"
                                " frame-ancestors 'none'; base-uri 'none';"
                                " form-action 'none'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}

#: How long a search waits for a slot under the concurrency cap (§6).
QUEUE_WAIT = 30.0
#: How long an issued request id stays valid without being used.
ID_TTL = 300.0
#: SQLite VM instructions between deadline checks. Small enough that a
#: cancel lands within milliseconds, large enough to cost nothing measurable.
PROGRESS_STEP = 10_000


def static_bytes(name: str) -> bytes:
    """A packaged static file. Through `importlib.resources`, as
    `curated_tags.json` is read, so an installed wheel serves the same page."""
    return (resources.files("rsse.web") / "static" / name).read_bytes()


@dataclass
class _Request:
    """One issued search id: the connection it runs on, once it runs."""

    issued: float
    conn: sqlite3.Connection | None = None
    cancelled: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass
class Config:
    database: Path
    archive: Path | None = None
    timeout: float = 60.0
    max_queries: int = 2
    allowed_hosts: frozenset = frozenset()
    attribution: str = ""


class App:
    """State shared across handler threads."""

    def __init__(self, config: Config):
        self.config = config
        self.slots = threading.BoundedSemaphore(max(1, config.max_queries))
        self.requests: dict[str, _Request] = {}
        self.lock = threading.Lock()
        self.waiting = 0

    # -- connections -------------------------------------------------------

    def open(self, deadline: float | None, request: _Request | None = None):
        """A read-only connection, with the archive attached if present."""
        conn = connect(str(self.config.database))
        if self.config.archive and self.config.archive.exists():
            conn.execute("ATTACH DATABASE ? AS arc",
                         (f"file:{self.config.archive}?mode=ro",))
        if deadline is not None:
            def check():
                if request is not None and request.cancelled:
                    return 1
                return 1 if time.monotonic() > deadline else 0
            conn.set_progress_handler(check, PROGRESS_STEP)
        return conn

    # -- request ids (§6) --------------------------------------------------

    def issue(self) -> str:
        now = time.monotonic()
        with self.lock:
            for rid in [k for k, r in self.requests.items()
                        if r.conn is None and now - r.issued > ID_TTL]:
                del self.requests[rid]
            rid = secrets.token_urlsafe(16)
            self.requests[rid] = _Request(issued=now)
        return rid

    def claim(self, rid: str) -> _Request:
        with self.lock:
            req = self.requests.get(rid)
        if req is None or req.conn is not None:
            raise api.ApiError(404, "unknown or already used request id")
        return req

    def release(self, rid: str) -> None:
        with self.lock:
            self.requests.pop(rid, None)

    def cancel(self, rid: str) -> None:
        with self.lock:
            req = self.requests.get(rid)
        if req is None:
            raise api.ApiError(404, "unknown request id")
        with req.lock:
            req.cancelled = True
            if req.conn is not None:
                req.conn.interrupt()

    # -- searches ----------------------------------------------------------

    def run_search(self, rid: str | None, fn):
        """Run ``fn(conn)`` under the cap, the deadline and cancellation.

        Never returns partial rows: an interrupted statement raises, and the
        response is an error, not whatever had been fetched so far (§6).
        """
        req = self.claim(rid) if rid is not None else None
        with self.lock:
            self.waiting += 1
            ahead = self.waiting - 1
        try:
            got = self.slots.acquire(timeout=QUEUE_WAIT)
        finally:
            with self.lock:
                self.waiting -= 1
        if not got:
            if rid:
                self.release(rid)
            raise api.ApiError(503, "the server is busy with other searches;"
                                    " try again when they finish",
                               ahead=ahead)
        started = time.monotonic()
        conn = None
        try:
            conn = self.open(started + self.config.timeout, req)
            if req is not None:
                with req.lock:
                    if req.cancelled:
                        raise api.ApiError(499, "cancelled")
                    req.conn = conn
            return fn(conn)
        except sqlite3.OperationalError as exc:
            if "interrupted" not in str(exc):
                raise
            elapsed = round((time.monotonic() - started) * 1000)
            if req is not None and req.cancelled:
                raise api.ApiError(499, "cancelled", elapsed_ms=elapsed)
            raise api.ApiError(
                504, f"the query exceeded the {self.config.timeout:g} s"
                     " limit and was stopped; no partial results are shown."
                     " Explain shows its plan without running it.",
                elapsed_ms=elapsed) from None
        finally:
            if conn is not None:
                conn.close()
            if rid is not None:
                self.release(rid)
            self.slots.release()

    def browse(self, fn):
        """Browse pages: deadline yes, cap no -- they are index lookups and
        must not queue behind a scan (§6)."""
        started = time.monotonic()
        conn = self.open(started + self.config.timeout)
        try:
            return fn(conn)
        except sqlite3.OperationalError as exc:
            if "interrupted" not in str(exc):
                raise
            raise api.ApiError(504, "the page took longer than the"
                                    f" {self.config.timeout:g} s limit")
        finally:
            conn.close()


# -- routing ----------------------------------------------------------------

_SEG = r"([^/]+)"

GET_ROUTES = [
    (r"/api/schema", lambda app, q: app.browse(api.schema)),
    (r"/api/about", lambda app, q: app.browse(
        lambda c: api.about(c, app.config.attribution))),
    (r"/api/tags", lambda app, q: app.browse(api.tags)),
    (r"/api/seasons", lambda app, q: app.browse(api.seasons)),
    (r"/api/names", lambda app, q: app.browse(
        lambda c: api.names(c, q.get("q", [""])[0]))),
    (rf"/api/play/{_SEG}", lambda app, q, i: app.browse(
        lambda c: api.play(c, i))),
    (rf"/api/game/id/{_SEG}", lambda app, q, i: app.browse(
        lambda c: api.game_by_id(c, i))),
    (rf"/api/game/{_SEG}", lambda app, q, i: app.browse(
        lambda c: api.game(c, i))),
    (rf"/api/player/{_SEG}", lambda app, q, i: app.browse(
        lambda c: api.player(c, i))),
    (rf"/api/team/{_SEG}/{_SEG}", lambda app, q, i, y: app.browse(
        lambda c: api.team_season(c, i, y))),
    (rf"/api/team/{_SEG}", lambda app, q, i: app.browse(
        lambda c: api.team(c, i))),
    (rf"/api/park/{_SEG}/{_SEG}", lambda app, q, i, y: app.browse(
        lambda c: api.park_games(c, i, y))),
    (rf"/api/park/{_SEG}", lambda app, q, i: app.browse(
        lambda c: api.park(c, i))),
    (r"/api/date/(\d{4}-\d{2}-\d{2})", lambda app, q, d: app.browse(
        lambda c: api.date(c, d))),
    (r"/api/season/(\d{4})", lambda app, q, y: app.browse(
        lambda c: api.season(c, y))),
]


def _export(app, q):
    try:
        body = json.loads(q.get("q", ["{}"])[0])
    except json.JSONDecodeError:
        raise api.ApiError(400, "q must be a JSON query body")
    fmt = q.get("format", ["csv"])[0]
    return app.run_search(None, lambda c: api.export(
        c, body, fmt, app.config.attribution))


POST_ROUTES = [
    (r"/api/query/start", lambda app, body: {"id": app.issue()}),
    (rf"/api/query/{_SEG}", lambda app, body, rid: app.run_search(
        rid, lambda c: api.query(c, body, app.config.attribution))),
    (r"/api/explain", lambda app, body: app.browse(
        lambda c: api.explain(c, body))),
    (rf"/api/cancel/{_SEG}", lambda app, body, rid: app.cancel(rid)),
]


def _match(routes, path):
    for pattern, fn in routes:
        m = re.fullmatch(pattern, path)
        if m:
            return fn, [unquote(g) for g in m.groups()]
    return None, None


class Handler(BaseHTTPRequestHandler):
    server_version = "rsse"
    app: App            # set on the subclass `make_server` builds

    def log_message(self, fmt, *args):
        sys.stderr.write(f"  {self.command} {self.path[:120]}"
                         f" -> {args[1] if len(args) > 1 else ''}\n")

    # -- response helpers ------------------------------------------------

    def _send(self, status: int, body: bytes, ctype: str, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in {**SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj, default=str).encode(),
                   "application/json")

    def _host_ok(self) -> bool:
        """The DNS-rebinding check (§8). Compared without the port."""
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            host = host[1:].split("]")[0]
        elif host.count(":") == 1:
            host = host.split(":")[0]
        return host in self.app.config.allowed_hosts

    def _guard(self) -> bool:
        if not self._host_ok():
            self._json(421, {"error": "unexpected Host header; start the"
                                      " server with --allowed-host to add"
                                      " a name"})
            return False
        return True

    def _dispatch(self, fn, *args):
        try:
            result = fn(self.app, *args)
        except api.ApiError as exc:
            self._json(exc.status, exc.as_dict())
            return
        except Exception as exc:        # a bug, reported as one
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
            raise
        if result is None:
            self.send_response(204)
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
        elif isinstance(result, tuple):         # (content type, bytes)
            ctype, data = result
            ext = "csv" if ctype.startswith("text/csv") else "json"
            self._send(200, data, ctype, {
                "Content-Disposition":
                    f'attachment; filename="rsse-query.{ext}"'})
        else:
            self._json(200, result)

    # -- verbs -----------------------------------------------------------

    def do_GET(self):
        if not self._guard():
            return
        url = urlsplit(self.path)
        if url.path in STATIC:
            name, ctype = STATIC[url.path]
            self._send(200, static_bytes(name), ctype)
            return
        query = parse_qs(url.query)
        if url.path == "/api/export":
            self._dispatch(lambda app, q: _export(app, q), query)
            return
        fn, groups = _match(GET_ROUTES, url.path)
        if fn is None:
            self._json(404, {"error": f"no such page {url.path}"})
            return
        self._dispatch(fn, query, *groups)

    do_HEAD = do_GET

    def do_POST(self):
        if not self._guard():
            return
        url = urlsplit(self.path)
        fn, groups = _match(POST_ROUTES, url.path)
        if fn is None:
            self._json(404, {"error": f"no such endpoint {url.path}"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            self._json(413, {"error": "request body too large"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"error": "request body must be JSON"})
            return
        if not isinstance(body, dict):
            self._json(400, {"error": "request body must be a JSON object"})
            return
        self._dispatch(fn, body, *groups)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def make_server(config: Config, host: str = "127.0.0.1",
                port: int = 8000) -> ThreadingHTTPServer:
    allowed = {"localhost", "127.0.0.1", "::1", host.lower()}
    allowed |= {h.lower() for h in config.allowed_hosts}
    config.allowed_hosts = frozenset(allowed)
    app = App(config)
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.app = app
    return server


def serve(config: Config, host: str, port: int, open_browser: bool) -> int:
    if not _is_loopback(host):
        print(f"WARNING: listening on {host}, not a loopback address. The"
              " server has no authentication: anyone who can reach this port"
              " can read the whole database. See spec/09-WEB.md §10.1.",
              file=sys.stderr)
    server = make_server(config, host, port)
    shown = "localhost" if _is_loopback(host) else host
    url = f"http://{shown}:{server.server_address[1]}/"
    print(f"rsse serving {config.database} at {url}")
    if config.archive and config.archive.exists():
        print(f"  archive attached read-only: {config.archive}")
    else:
        print("  no archive: game logs and source lines will be unavailable")
    print("  Ctrl-C to stop")
    if open_browser:
        import webbrowser
        threading.Timer(0.5, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0
