"""Local, read-only inspection UI for the agent-msg journal.

The server binds to 127.0.0.1 only, on a random port by default, and every
API request must carry a per-launch random bearer token. The startup URL puts
that token in a fragment, never in cookies or request URLs. Static assets are
public and contain no journal data. Unexpected Host headers are refused,
only GET/HEAD are allowed, and each API request opens the journal read-only.

Public surface:
    make_server(db_path, port=0) -> (server, url)   # for tests / embedding
    serve(db_path, port=0, open_browser=True)        # blocking; used by `agent-msg ui`
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import secrets
import socket
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

HOST = "127.0.0.1"
UI_HOSTNAMES = ("127.0.0.1", "localhost", "agent-msg.local")
MAX_LIMIT = 500
DEFAULT_LIMIT = 100
STATUSES = ("prepared", "dispatching", "sent", "failed", "uncertain", "received")

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}

JournalOpener = Callable[[], Any]


def _default_opener(db_path: str | Path) -> JournalOpener:
    path = Path(db_path).expanduser()

    def open_journal():
        from agent_msg.journal import Journal  # imported lazily: the backend owns this module

        return Journal(path, readonly=True)

    return open_journal


def _static_root():
    return resources.files("agent_msg") / "static"


class _Handler(BaseHTTPRequestHandler):
    server_version = "agent-msg-ui"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # Set per server by make_server().
    token: str
    allowed_hosts: frozenset[str]
    open_journal: JournalOpener

    # -- plumbing ----------------------------------------------------------- #
    def log_message(self, fmt: str, *args: Any) -> None:  # keep stderr quiet; never log tokens
        pass

    def _send(self, status: int, body: bytes = b"", content_type: str = "text/plain; charset=utf-8",
              extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode()
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _token_ok(self, candidate: str | None) -> bool:
        return bool(candidate) and hmac.compare_digest(candidate.encode(), self.token.encode())

    # -- request entry points ---------------------------------------------- #
    def do_GET(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    def _refuse_method(self) -> None:
        self.close_connection = True
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"read-only\n", extra={"Allow": "GET, HEAD"})

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _refuse_method

    def _dispatch(self) -> None:
        if (self.headers.get("Host") or "").lower() not in self.allowed_hosts:
            self._send(HTTPStatus.MISDIRECTED_REQUEST, b"unexpected Host header\n")
            return

        url = urlsplit(self.path)
        query = parse_qs(url.query, keep_blank_values=True)

        if url.path.startswith("/api/"):
            authorization = self.headers.get("Authorization", "")
            candidate = authorization[7:] if authorization.startswith("Bearer ") else None
            if not self._token_ok(candidate):
                self._send(HTTPStatus.FORBIDDEN, b"Use the link printed by agent-msg ui.\n")
                return

        try:
            if url.path.startswith("/api/"):
                self._api(url.path, query)
            else:
                self._static(url.path)
        except BrokenPipeError:
            pass
        except Exception as exc:  # never leak a traceback to the browser
            print(f"agent-msg ui: {type(exc).__name__}: {exc}", file=sys.stderr)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error; see the terminal running `agent-msg ui`")

    # -- static files ------------------------------------------------------- #
    def _static(self, path: str) -> None:
        name = "index.html" if path in ("", "/") else path.lstrip("/")
        parts = name.split("/")
        if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        node = _static_root()
        for p in parts:
            node = node / p
        if not node.is_file():
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(HTTPStatus.OK, node.read_bytes(), ctype)

    # -- JSON API ----------------------------------------------------------- #
    def _api(self, path: str, query: dict[str, list[str]]) -> None:
        def arg(name: str) -> str | None:
            v = query.get(name, [None])[0]
            return v if v not in (None, "") else None

        parts = [p for p in path.split("/") if p][1:]  # drop "api"
        try:
            journal = self.open_journal()
        except (FileNotFoundError, ValueError):
            self._error(HTTPStatus.SERVICE_UNAVAILABLE,
                        "No journal yet. Send a message with `agent-msg send` to create it.")
            return

        with journal as j:
            if parts == ["stats"]:
                self._json(HTTPStatus.OK, j.stats())
            elif parts == ["peers"]:
                self._json(HTTPStatus.OK, {"peers": j.list_peers()})
            elif parts == ["messages"]:
                try:
                    limit = min(max(int(arg("limit") or DEFAULT_LIMIT), 1), MAX_LIMIT)
                    before_seq = int(arg("before_seq")) if arg("before_seq") else None
                except ValueError:
                    self._error(HTTPStatus.BAD_REQUEST, "limit and before_seq must be integers")
                    return
                status = arg("status")
                if status is not None and status not in STATUSES:
                    self._error(HTTPStatus.BAD_REQUEST, f"status must be one of: {', '.join(STATUSES)}")
                    return
                messages = j.list_messages(limit=limit, before_seq=before_seq, peer=arg("peer"),
                                           q=arg("q"), status=status)
                self._json(HTTPStatus.OK, {"messages": messages, "limit": limit})
            elif len(parts) == 2 and parts[0] == "messages":
                msg = j.get_message(parts[1])
                if msg is None:
                    self._error(HTTPStatus.NOT_FOUND, "no message with that id")
                else:
                    self._json(HTTPStatus.OK, msg)
            elif len(parts) == 3 and parts[0] == "messages" and parts[2] == "conversation":
                if j.get_message(parts[1]) is None:
                    self._error(HTTPStatus.NOT_FOUND, "no message with that id")
                else:
                    self._json(HTTPStatus.OK, {"messages": j.get_conversation(parts[1])})
            else:
                self._error(HTTPStatus.NOT_FOUND, "unknown endpoint")


def make_server(db_path: str | Path, port: int = 0, *, hostname: str = HOST,
                open_journal: JournalOpener | None = None):
    """Create (but do not start) the UI server. Returns ``(server, url)``.

    ``url`` contains the per-launch token; hand it to the user or a browser.
    ``open_journal`` replaces the real journal (used by tests).
    """
    if hostname not in UI_HOSTNAMES:
        raise ValueError('UI hostname must be 127.0.0.1, localhost, or agent-msg.local')
    token = secrets.token_urlsafe(32)
    server = ThreadingHTTPServer((HOST, port), _Handler)
    server.daemon_threads = True
    bound_port = server.server_address[1]
    handler = type("Handler", (_Handler,), {
        "token": token,
        "allowed_hosts": frozenset(
            {f"{name}:{bound_port}" for name in UI_HOSTNAMES}
            | (set(UI_HOSTNAMES) if bound_port == 80 else set())),
        "open_journal": staticmethod(open_journal or _default_opener(db_path)),
    })
    server.RequestHandlerClass = handler
    authority = hostname if bound_port == 80 else f"{hostname}:{bound_port}"
    return server, f"http://{authority}/#token={token}"


def serve(db_path: str | Path, port: int = 0, open_browser: bool = True, hostname: str = 'auto') -> None:
    """Run the UI until interrupted. Prints the tokenised URL."""
    if hostname in ('auto', 'agent-msg.local'):
        try:
            addresses = {entry[4][0] for entry in socket.getaddrinfo('agent-msg.local', None)}
        except OSError:
            addresses = set()
        local_alias = bool(addresses) and addresses <= {HOST, '::1'} and HOST in addresses
        if hostname == 'auto':
            hostname = 'agent-msg.local' if local_alias else HOST
        elif not local_alias:
            raise ValueError("agent-msg.local must resolve to 127.0.0.1. Add '127.0.0.1 agent-msg.local' "
                             "to /etc/hosts with administrator privileges, or use --hostname 127.0.0.1.")
    server, url = make_server(db_path, port, hostname=hostname)
    print(f"agent-msg journal: {url}\nPress Ctrl+C to stop.", flush=True)
    if open_browser:
        def launch():
            if not webbrowser.open(url, new=1):
                print('Could not open a browser automatically; open the printed URL.', file=sys.stderr)
        timer = threading.Timer(0.3, launch)
        timer.daemon = True
        timer.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
