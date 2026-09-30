#!/usr/bin/env python3
"""Discover running Codex CLI sessions on this machine.

Interactive Codex sessions (TUIs, sub-agents, IDE clients) run their threads
inside a shared, per-user app-server daemon that listens on a Unix socket and
speaks JSON-RPC over WebSocket. This script:

  1. connects to that daemon and lists every loaded thread with its metadata
     (name, cwd, status, active turn, queued messages, model, origin, ...);
  2. scans the OS process table for Codex processes, records their PID, TTY,
     cwd and whether they are attached to the daemon;
  3. links top-level threads to the TUI process most likely hosting them.

Only the Python standard library is used. Read-only: no thread is modified.

Examples:
  discover-codex-agents.py                    # table of all sessions
  discover-codex-agents.py --cwd .            # only sessions in this directory
  discover-codex-agents.py --json             # full machine-readable output
  discover-codex-agents.py --name 'codex[1]'  # threads whose name contains a substring
  discover-codex-agents.py --no-subagents     # hide sub-agent threads
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CLIENT_INFO = {"name": "discover-codex-agents", "title": "Codex session discovery", "version": "1.0"}
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# --------------------------------------------------------------------------- #
# Minimal WebSocket-over-Unix-socket JSON-RPC client (stdlib only)
# --------------------------------------------------------------------------- #

class RpcError(Exception):
    def __init__(self, method: str, error: dict):
        super().__init__(f"{method}: {error.get('message', error)}")
        self.method = method
        self.error = error


class DaemonClient:
    def __init__(self, path: str, timeout: float = 10.0):
        self.path = path
        self.timeout = timeout
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(path)
        self._buf = b""
        self._next_id = 0
        self.notifications: list[dict] = []
        self._handshake()

    # -- transport ---------------------------------------------------------- #
    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        while b"\r\n\r\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("daemon closed the connection during the WebSocket handshake")
            self._buf += chunk
        head, self._buf = self._buf.split(b"\r\n\r\n", 1)
        lines = head.decode(errors="replace").split("\r\n")
        if not lines or " 101 " not in f"{lines[0]} ":
            raise ConnectionError(f"WebSocket upgrade refused: {lines[0] if lines else '<empty>'}")
        headers = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:])}
        expected = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        if headers.get("sec-websocket-accept") != expected:
            raise ConnectionError("WebSocket upgrade returned an invalid Sec-WebSocket-Accept")

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise ConnectionError("daemon closed the connection")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 1 << 16:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def _recv_message(self) -> str:
        parts: list[bytes] = []
        while True:
            b0, b1 = self._read_exact(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if b1 & 0x80 else None
            payload = self._read_exact(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:  # close
                raise ConnectionError("daemon sent a WebSocket close frame")
            if opcode == 0x9:  # ping
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode in (0x0, 0x1, 0x2):
                parts.append(payload)
                if fin:
                    return b"".join(parts).decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self._send_frame(0x8, struct.pack("!H", 1000))
        except OSError:
            pass
        self.sock.close()

    # -- JSON-RPC ----------------------------------------------------------- #
    def notify(self, method: str, params: dict | None = None) -> None:
        msg = {"method": method}
        if params is not None:
            msg["params"] = params
        self._send_frame(0x1, json.dumps(msg).encode())

    def call(self, method: str, params: dict | None = None) -> Any:
        self._next_id += 1
        rid = self._next_id
        self._send_frame(0x1, json.dumps({"id": rid, "method": method, "params": params or {}}).encode())
        while True:
            msg = json.loads(self._recv_message())
            if "method" in msg:
                if "id" in msg:
                    # Server-initiated request; this client never takes actions on its behalf.
                    self._send_frame(0x1, json.dumps({
                        "id": msg["id"],
                        "error": {"code": -32601, "message": "discovery client does not handle requests"},
                    }).encode())
                else:
                    self.notifications.append(msg)
                continue
            if msg.get("id") != rid:
                continue
            if "error" in msg:
                raise RpcError(method, msg["error"])
            return msg.get("result")

    def initialize(self) -> dict:
        result = self.call("initialize", {"clientInfo": CLIENT_INFO, "capabilities": {"experimentalApi": True}})
        self.notify("initialized")
        return result or {}


# --------------------------------------------------------------------------- #
# Daemon discovery
# --------------------------------------------------------------------------- #

def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def run(cmd: list[str], timeout: float = 10.0) -> str | None:
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return cp.stdout if cp.returncode == 0 else None


def daemon_version_info() -> dict | None:
    if not shutil.which("codex"):
        return None
    out = run(["codex", "app-server", "daemon", "version"], timeout=15)
    if not out:
        return None
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return None


def resolve_socket(explicit: str | None, version_info: dict | None) -> str | None:
    if explicit:
        return explicit if Path(explicit).exists() else None
    candidates = []
    if version_info and version_info.get("socketPath"):
        candidates.append(version_info["socketPath"])
    candidates.append(str(codex_home() / "app-server-control" / "app-server-control.sock"))
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


def paginate(client: DaemonClient, method: str, params: dict) -> list:
    items, cursor = [], None
    for _ in range(1000):
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        res = client.call(method, p) or {}
        items.extend(res.get("data") or [])
        cursor = res.get("nextCursor")
        if not cursor:
            break
    return items


def iso(ts: Any) -> str | None:
    if not isinstance(ts, (int, float)):
        return None
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def describe_thread(client: DaemonClient, tid: str) -> dict:
    rec: dict[str, Any] = {"threadId": tid, "errors": []}
    try:
        th = (client.call("thread/read", {"threadId": tid}) or {}).get("thread") or {}
    except RpcError as e:
        rec["errors"].append(str(e))
        return rec

    status = th.get("status") or {}
    src = th.get("source")
    spawn = src.get("subAgent", {}).get("thread_spawn", {}) if isinstance(src, dict) else {}
    rec.update({
        "name": th.get("name"),
        "cwd": th.get("cwd"),
        "status": status.get("type"),
        "activeFlags": status.get("activeFlags") or [],
        "model": th.get("model"),
        "reasoningEffort": th.get("reasoningEffort"),
        "modelProvider": th.get("modelProvider"),
        "originator": th.get("originator"),
        "source": src if isinstance(src, str) else ("subAgent" if spawn else src),
        "threadSource": th.get("threadSource"),
        "isSubAgent": bool(spawn) or bool(th.get("parentThreadId")),
        "parentThreadId": th.get("parentThreadId") or spawn.get("parent_thread_id"),
        "agentNickname": th.get("agentNickname") or spawn.get("agent_nickname"),
        "agentRole": th.get("agentRole") or spawn.get("agent_role"),
        "agentPath": spawn.get("agent_path"),
        "forkedFromId": th.get("forkedFromId"),
        "ephemeral": th.get("ephemeral"),
        "canAcceptDirectInput": th.get("canAcceptDirectInput"),
        "cliVersion": th.get("cliVersion"),
        "createdAt": iso(th.get("createdAt")),
        "updatedAt": iso(th.get("updatedAt")),
        "createdAtEpoch": th.get("createdAt"),
        "rolloutPath": th.get("path"),
        "workspaceRoots": sorted({r for env in th.get("environments") or [] for r in env.get("runtimeWorkspaceRoots") or []}),
        "preview": (th.get("preview") or "").strip().replace("\n", " ")[:200] or None,
        "activeTurnId": None,
        "lastTurn": None,
        "queued": None,
    })

    try:
        turns = paginate_once(client, "thread/turns/list", {"threadId": tid, "limit": 1})
        if turns:
            t = turns[0]
            rec["lastTurn"] = {
                "id": t.get("id"),
                "status": t.get("status"),
                "startedAt": iso(t.get("startedAt")),
                "completedAt": iso(t.get("completedAt")),
            }
            if t.get("status") == "inProgress":
                rec["activeTurnId"] = t.get("id")
    except RpcError as e:
        rec["errors"].append(str(e))

    try:
        q = paginate(client, "thread/queue/list", {"threadId": tid})
        rec["queued"] = [
            {
                "id": item.get("id"),
                "text": " ".join(
                    part.get("text", "") for part in item.get("input") or [] if isinstance(part, dict)
                ).strip()[:160],
            }
            for item in q
        ]
    except RpcError as e:
        rec["errors"].append(str(e))

    if not rec["errors"]:
        del rec["errors"]
    return rec


def paginate_once(client: DaemonClient, method: str, params: dict) -> list:
    return (client.call(method, params) or {}).get("data") or []


# --------------------------------------------------------------------------- #
# Process discovery
# --------------------------------------------------------------------------- #

@dataclass
class Proc:
    pid: int
    ppid: int
    tty: str
    started: dt.datetime | None
    command: str
    kind: str = "other"
    cwd: str | None = None
    daemon_attached: bool | None = None
    thread_ids: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "pid": self.pid,
            "ppid": self.ppid,
            "tty": None if self.tty in ("?", "??", "") else self.tty,
            "started": self.started.astimezone().isoformat(timespec="seconds") if self.started else None,
            "kind": self.kind,
            "cwd": self.cwd,
            "daemonAttached": self.daemon_attached,
            "command": self.command,
        }


OPTIONS_WITH_VALUE = {
    "-c", "--config", "--enable", "--disable", "-m", "--model", "-p", "--profile", "-s", "--sandbox",
    "-a", "--ask-for-approval", "-C", "--cd", "-i", "--image", "--add-dir", "--remote",
    "--remote-auth-token-env", "--listen", "--code-mode-host", "--local-provider", "-o",
    "--output-last-message", "--output-schema", "--color",
}


def classify(argv: list[str]) -> str | None:
    """Return the kind of Codex process, or None if the command is not Codex itself."""
    if not argv:
        return None
    exe = os.path.basename(argv[0])
    if exe != "codex":
        if exe.startswith("codex-code-mode-host"):
            return "code-mode-host"
        return None
    args, skip = [], False
    for a in argv[1:]:
        if skip:
            skip = False
        elif a.startswith("-"):
            skip = "=" not in a and a in OPTIONS_WITH_VALUE
        else:
            args.append(a)
    if args[:1] == ["app-server"]:
        if "--managed-daemon" in argv:
            return "app-server-daemon"
        if args[1:2] == ["daemon"]:
            return "daemon-helper"
        if any(ide in argv[0] for ide in ("/.vscode", "/.cursor", "/.windsurf", "/.vscode-server")):
            return "ide-app-server"
        return "app-server"
    if args[:1] == ["exec"]:
        return "exec"
    if args[:1] in (["mcp-server"], ["mcp"]):
        return "mcp"
    if args[:1] in ([], ["resume"], ["fork"]) or (args and not args[0] in ("login", "logout", "queue", "agents")):
        return "tui"
    return "cli"


def list_codex_processes() -> list[Proc]:
    out = run(["ps", "-axo", "pid=,ppid=,tty=,lstart=,command="])
    procs: list[Proc] = []
    if not out:
        return procs
    line_re = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\S+)\s+(\w{3}\s+\w{3}\s+\d+\s+[\d:]+\s+\d{4})\s+(.*)$")
    for line in out.splitlines():
        m = line_re.match(line)
        if not m:
            continue
        pid, ppid, tty, lstart, command = m.groups()
        if int(pid) == os.getpid():
            continue
        try:
            argv = command.split()
        except ValueError:
            continue
        kind = classify(argv)
        if kind is None:
            continue
        try:
            started = dt.datetime.strptime(" ".join(lstart.split()), "%a %b %d %H:%M:%S %Y")
            started = started.replace(tzinfo=dt.datetime.now().astimezone().tzinfo)
        except ValueError:
            started = None
        procs.append(Proc(int(pid), int(ppid), tty, started, command, kind))
    return procs


def fill_cwds(procs: list[Proc]) -> None:
    if not procs:
        return
    if sys.platform.startswith("linux"):
        for p in procs:
            try:
                p.cwd = os.readlink(f"/proc/{p.pid}/cwd")
            except OSError:
                pass
        return
    if not shutil.which("lsof"):
        return
    out = run(["lsof", "-nP", "-a", "-d", "cwd", "-p", ",".join(str(p.pid) for p in procs), "-Fpn"]) or ""
    by_pid = {p.pid: p for p in procs}
    cur = None
    for line in out.splitlines():
        if line.startswith("p"):
            cur = by_pid.get(int(line[1:]))
        elif line.startswith("n") and cur is not None:
            cur.cwd = line[1:]


def fill_daemon_attachment(procs: list[Proc], socket_path: str | None) -> None:
    """Mark processes holding a connection to the daemon's control socket (macOS lsof endpoint matching)."""
    daemons = [p for p in procs if p.kind == "app-server-daemon"]
    if not socket_path or not daemons or not shutil.which("lsof"):
        return
    real = os.path.realpath(socket_path)
    out = run(["lsof", "-nP", "-U", "-a", "-p", ",".join(str(p.pid) for p in procs), "-Fpdn"]) or ""
    daemon_pids = {p.pid for p in daemons}
    endpoints: set[str] = set()      # socket endpoints owned by the daemon on the control socket
    peers: dict[int, set[str]] = {}  # pid -> endpoints this process's sockets point to
    cur_pid, cur_dev = None, None
    for line in out.splitlines():
        tag, val = line[:1], line[1:]
        if tag == "p":
            cur_pid, cur_dev = int(val), None
        elif tag == "d":
            cur_dev = val
        elif tag == "n" and cur_pid is not None:
            if cur_pid in daemon_pids and cur_dev and os.path.realpath(val) == real:
                endpoints.add(cur_dev)
            elif val.startswith("->"):
                peers.setdefault(cur_pid, set()).add(val[2:])
    if not endpoints:
        return  # platform does not expose peer endpoints; leave as unknown
    for p in procs:
        if p.kind in ("app-server-daemon", "daemon-helper", "code-mode-host"):
            continue
        p.daemon_attached = bool(peers.get(p.pid, set()) & endpoints)


def link_threads_to_processes(threads: list[dict], procs: list[Proc]) -> None:
    """Best-effort: pair each top-level TUI thread with the TUI process in the same cwd.

    A TUI starts its thread a moment after the process starts, so candidates are
    ranked by |thread.createdAt - process.start|. Resumed sessions (thread older
    than every candidate) fall back to a unique remaining cwd match.
    """
    tuis = [p for p in procs if p.kind == "tui" and p.daemon_attached is not False]
    claimed: set[int] = set()
    top = [t for t in threads if not t.get("isSubAgent") and t.get("originator") == "codex-tui" and t.get("cwd")]
    pairs = []
    for t in top:
        for p in tuis:
            if p.cwd and os.path.realpath(p.cwd) == os.path.realpath(t["cwd"]) and p.started and t.get("createdAtEpoch"):
                delta = t["createdAtEpoch"] - p.started.timestamp()
                if -5 <= delta <= 6 * 3600:
                    pairs.append((abs(delta), t["threadId"], p.pid, "start-time"))
    assigned: dict[str, tuple[int, str]] = {}
    for _, tid, pid, how in sorted(pairs):
        if tid in assigned or pid in claimed:
            continue
        assigned[tid] = (pid, how)
        claimed.add(pid)
    for t in top:
        if t["threadId"] in assigned:
            continue
        left = [p for p in tuis if p.pid not in claimed and p.cwd
                and os.path.realpath(p.cwd) == os.path.realpath(t["cwd"])]
        if len(left) == 1:
            assigned[t["threadId"]] = (left[0].pid, "unique-cwd")
            claimed.add(left[0].pid)
    by_pid = {p.pid: p for p in procs}
    for t in threads:
        hit = assigned.get(t["threadId"])
        if hit:
            p = by_pid[hit[0]]
            p.thread_ids.append(t["threadId"])
            t["process"] = {**p.to_json(), "match": hit[1]}
        else:
            t["process"] = None


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #

def short_home(path: str | None) -> str:
    if not path:
        return "-"
    home = str(Path.home())
    return "~" + path[len(home):] if path.startswith(home) else path


def print_table(report: dict) -> None:
    d = report["daemon"]
    print(f"Daemon: {d.get('status', 'unknown')}  socket={short_home(d.get('socket'))}  "
          f"appServer={d.get('appServerVersion') or '?'}  cli={d.get('cliVersion') or '?'}")
    threads = report["threads"]
    if not threads:
        print("\nNo loaded Codex threads.")
    else:
        rows = []
        for t in threads:
            proc = t.get("process") or {}
            kind = f"sub:{t.get('agentNickname') or '?'}" if t.get("isSubAgent") else (t.get("originator") or "-")
            status = t.get("status") or "?"
            if t.get("activeTurnId"):
                status += f" (turn {t['activeTurnId'][:13]})"
            queued = len(t["queued"]) if isinstance(t.get("queued"), list) else "?"
            rows.append([
                t["threadId"], t.get("name") or "-", kind, status, str(queued),
                t.get("model") or "-", short_home(t.get("cwd")),
                f"{proc['pid']}/{proc.get('tty') or '-'}" if proc else "-",
            ])
        headers = ["THREAD ID", "NAME", "KIND", "STATUS", "Q", "MODEL", "CWD", "PID/TTY"]
        widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
        print()
        print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
        print("  ".join("-" * w for w in widths))
        for r in rows:
            print("  ".join(c.ljust(w) for c, w in zip(r, widths)))

    others = report["processes"]
    unreached = [p for p in others if p["kind"] in ("tui", "exec", "ide-app-server", "app-server")
                 and not p.get("linkedThreads") and p["kind"] != "app-server-daemon"]
    if unreached:
        print("\nCodex processes without a linked daemon thread:")
        for p in unreached:
            att = {True: "attached", False: "NOT attached (unreachable via daemon)", None: "attachment unknown"}[p["daemonAttached"]]
            print(f"  pid {p['pid']:<6} {p['kind']:<15} {p.get('tty') or '-':<8} {short_home(p.get('cwd')):<40} {att}")
    for w in report.get("warnings", []):
        print(f"\nwarning: {w}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="emit the full report as JSON")
    ap.add_argument("--cwd", help="only threads whose cwd is (or is inside) this directory")
    ap.add_argument("--name", help="only threads whose name contains this substring (case-insensitive)")
    ap.add_argument("--no-subagents", action="store_true", help="hide sub-agent threads")
    args = ap.parse_args()

    warnings: list[str] = []
    version = daemon_version_info()
    sock_path = resolve_socket(None, version)
    daemon: dict[str, Any] = {
        "socket": sock_path,
        "status": (version or {}).get("status") or ("socket-present" if sock_path else "not-found"),
        "appServerVersion": (version or {}).get("appServerVersion"),
        "cliVersion": (version or {}).get("cliVersion"),
        "managedCodexPath": (version or {}).get("managedCodexPath"),
    }

    threads: list[dict] = []
    if sock_path:
        try:
            client = DaemonClient(sock_path)
            try:
                init = client.initialize()
                daemon["userAgent"] = init.get("userAgent")
                daemon["codexHome"] = init.get("codexHome")
                ids = paginate(client, "thread/loaded/list", {})
                threads = [describe_thread(client, tid) for tid in ids]
            finally:
                client.close()
        except (OSError, ConnectionError, RpcError, json.JSONDecodeError) as e:
            warnings.append(f"could not query the app-server daemon at {sock_path}: {e}")
            daemon["status"] = "unreachable"
    else:
        warnings.append("no app-server daemon socket found; is the Codex daemon running?")

    procs = list_codex_processes()
    fill_cwds(procs)
    fill_daemon_attachment(procs, sock_path)
    link_threads_to_processes(threads, procs)
    daemon_proc = next((p for p in procs if p.kind == "app-server-daemon"), None)
    if daemon_proc:
        daemon["pid"] = daemon_proc.pid

    if args.cwd:
        root = os.path.realpath(os.path.expanduser(args.cwd))
        threads = [t for t in threads if t.get("cwd") and
                   (os.path.realpath(t["cwd"]) == root or os.path.realpath(t["cwd"]).startswith(root + os.sep))]
        procs = [p for p in procs if p.cwd and
                 (os.path.realpath(p.cwd) == root or os.path.realpath(p.cwd).startswith(root + os.sep))]
    if args.name:
        threads = [t for t in threads if args.name.lower() in (t.get("name") or "").lower()]
    if args.no_subagents:
        threads = [t for t in threads if not t.get("isSubAgent")]

    threads.sort(key=lambda t: (t.get("cwd") or "", t.get("isSubAgent", False), t.get("createdAtEpoch") or 0))
    for t in threads:
        t.pop("createdAtEpoch", None)

    report = {
        "generatedAt": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "daemon": daemon,
        "threads": threads,
        "processes": [{**p.to_json(), "linkedThreads": p.thread_ids} for p in procs],
        "warnings": warnings,
    }

    if args.json:
        json.dump(report, sys.stdout, indent=2)
        print()
    else:
        print_table(report)
    return 0 if daemon["status"] not in ("not-found", "unreachable") else 2


if __name__ == "__main__":
    sys.exit(main())
