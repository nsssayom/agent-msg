#!/usr/bin/env python3
"""Push a message into a running Codex CLI session.

The target is a loaded thread on the shared Codex app-server daemon, given by
thread ID or by exact session name (see discover-codex-agents.py).

If the session is idle, the message starts a new turn. If the session is busy,
the message is added to the turn that is currently running.

Examples:
  push-to-codex.py 'codex[1]-msg' "Please reply ack"
  push-to-codex.py 01a0f3cc-acd2-74a3-b786-c2fd454382b1 "status?" --wait
  echo "long message" | push-to-codex.py 'codex[0]-msg' --json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import time
import uuid
from pathlib import Path

from . import codex_discovery as discover

DaemonClient, RpcError = discover.DaemonClient, discover.RpcError
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
TERMINAL_TURN_STATES = {"completed", "failed", "interrupted", "cancelled", "canceled"}
WAIT_TIMEOUT = 600


class PushError(Exception):
    pass


def read_thread(client: DaemonClient, tid: str) -> dict:
    return (client.call("thread/read", {"threadId": tid}) or {}).get("thread") or {}


def resolve_target(client: DaemonClient, target: str) -> dict:
    """Return the loaded thread matching a thread ID or an exact session name."""
    loaded = discover.paginate(client, "thread/loaded/list", {})
    if UUID_RE.match(target):
        if target.lower() not in {t.lower() for t in loaded}:
            raise PushError(f"thread {target} is not loaded in the Codex daemon (session not running)")
        return read_thread(client, target)

    matches = [th for th in (read_thread(client, tid) for tid in loaded) if th.get("name") == target]
    if not matches:
        raise PushError(f"no running Codex session named {target!r} (names are exact and case-sensitive)")
    if len(matches) > 1:
        listing = "\n".join(f"  {t['id']}  cwd={t.get('cwd')}" for t in matches)
        raise PushError(f"session name {target!r} is ambiguous; use a thread ID:\n{listing}")
    return matches[0]


def active_turn_id(client: DaemonClient, tid: str) -> str | None:
    turns = (client.call("thread/turns/list", {"threadId": tid, "limit": 1}) or {}).get("data") or []
    if turns and turns[0].get("status") == "inProgress":
        return turns[0].get("id")
    return None


def deliver(client: DaemonClient, thread: dict, text: str, msg_id: str) -> dict:
    """Start a new turn on an idle thread, or steer the message into the running turn."""
    tid = thread["id"]
    status = (thread.get("status") or {}).get("type")
    busy_turn = active_turn_id(client, tid) if status == "active" else None
    user_input = [{"type": "text", "text": text, "text_elements": []}]
    if busy_turn:
        res = client.call("turn/steer", {"threadId": tid, "input": user_input,
                                         "expectedTurnId": busy_turn, "clientUserMessageId": msg_id})
        return {"mode": "steer", "turnId": (res or {}).get("turnId") or busy_turn}
    res = client.call("turn/start", {"threadId": tid, "input": user_input, "clientUserMessageId": msg_id})
    return {"mode": "start", "turnId": ((res or {}).get("turn") or {}).get("id")}


def _item_text(item: dict) -> str:
    if item.get("text"):
        return item["text"]
    return " ".join(p.get("text", "") for p in item.get("content") or [] if isinstance(p, dict))


def wait_for_reply(client: DaemonClient, tid: str, delivery: dict, msg_id: str, text: str,
                   timeout: float, poll: float = 1.5) -> dict:
    """Poll the thread until the turn carrying our message finishes; return its last agent message."""
    deadline = time.time() + timeout
    turn_id = delivery.get("turnId")
    while time.time() < deadline:
        turns = (client.call("thread/turns/list", {"threadId": tid, "limit": 5, "itemsView": "full"}) or {}).get("data") or []
        for turn in turns:
            items = turn.get("items") or []
            ours = turn.get("id") == turn_id or any(
                it.get("type") == "userMessage" and (it.get("clientId") == msg_id or _item_text(it) == text)
                for it in items)
            if not ours:
                continue
            turn_id = turn.get("id")
            if turn.get("status") in TERMINAL_TURN_STATES:
                # For a steered turn, only agent messages after our input count as the reply.
                after, seen = [], False
                for it in items:
                    if it.get("type") == "userMessage" and (it.get("clientId") == msg_id or _item_text(it) == text):
                        seen, after = True, []
                    elif it.get("type") == "agentMessage":
                        after.append(it)
                replies = after if seen else [it for it in items if it.get("type") == "agentMessage"]
                final = [it for it in replies if it.get("phase") == "final_answer"] or replies
                return {"turnId": turn_id, "turnStatus": turn.get("status"),
                        "reply": _item_text(final[-1]) if final else None,
                        "error": turn.get("error")}
            break
        time.sleep(poll)
    return {"turnId": turn_id, "turnStatus": "timeout", "reply": None}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="thread ID or exact session name")
    ap.add_argument("message", nargs="?", default="-", help="message text, or '-' / omitted to read stdin")
    ap.add_argument("--wait", action="store_true",
                    help=f"wait (up to {WAIT_TIMEOUT}s) for the turn to finish and print the reply")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    args = ap.parse_args()

    text = (sys.stdin.read() if args.message == "-" else args.message).strip()
    if not text:
        print("error: empty message", file=sys.stderr)
        return 1

    sock_path = discover.resolve_socket(None, discover.daemon_version_info())
    if not sock_path:
        print("error: Codex app-server daemon socket not found", file=sys.stderr)
        return 2

    result: dict = {"ok": False}
    try:
        client = DaemonClient(sock_path, timeout=30)
        try:
            client.initialize()
            thread = resolve_target(client, args.target)
            result.update({"threadId": thread["id"], "name": thread.get("name"), "cwd": thread.get("cwd")})
            msg_id = str(uuid.uuid4())
            result.update(deliver(client, thread, text, msg_id))
            result.update({"ok": True, "clientUserMessageId": msg_id})
            if args.wait:
                result.update(wait_for_reply(client, thread["id"], result, msg_id, text, WAIT_TIMEOUT))
                result["ok"] = result.get("turnStatus") == "completed"
        finally:
            client.close()
    except PushError as e:
        result["error"] = str(e)
    except RpcError as e:
        result["error"] = f"daemon rejected {e.method}: {e.error.get('message', e.error)}"
    except (OSError, ConnectionError) as e:
        result["error"] = f"could not talk to the Codex daemon at {sock_path}: {e}"

    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result["ok"] else 1
    if result.get("error"):
        print(f"error: {result['error']}", file=sys.stderr)
        return 1

    print(f"sent via {result['mode']} to {result.get('name') or '-'} ({result['threadId']}) -> {result.get('turnId')}")
    if args.wait:
        print(f"turn {result.get('turnStatus')}:")
        print(result.get("reply") or "(no agent message)")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
