# Cross-agent messaging: Claude Code ↔ Codex CLI

Findings from a communication test between agents sharing the workdir `~/Dev/agent-msg`.
Environment: macOS, Claude Code 2.1.286, Codex CLI 0.159.0 (app-server daemon 0.159.2). Date: 2026-09-30.

## Summary

| From → To        | Native push? | Mechanism                                                      | Tested |
|------------------|--------------|----------------------------------------------------------------|--------|
| Claude → Claude  | Yes          | `ListAgents` + `SendMessage` (per-session Unix socket)          | ✅ ack received |
| Claude → Codex   | Yes          | Codex app-server daemon: `turn/start`, `codex queue`            | ✅ ack received (idle thread) |
| Codex → Codex    | Yes          | Same daemon APIs                                               | ✅ observed (one Codex thread messaged the other) |
| Codex → Claude   | Yes (observed) | A helper script launched by the Codex agent joined as a peer on Claude's per-session sockets | ✅ message received, ack sent back |
| Codex → Claude (reply to a Claude prompt) | Pull | The Codex agent's reply stays in its own thread. Claude pulls it with `thread/read` or live notifications. | ✅ |

---

## 1. Claude ↔ Claude

- Each interactive Claude Code session runs a messaging endpoint on a Unix socket: `/tmp/cc-socks/<pid>.sock`.
- `ListAgents` shows peer sessions by name (e.g. `agent-msg-f5 [3ed062]`), with their mode and busy/idle state.
- `SendMessage {to: "<name>", message: "..."}` delivers a message to a peer. The recipient sees it as
  `<cross-session-message from="uds:/tmp/cc-socks/<pid>.sock" from-name="..." from-mode="...">`.
  To reply, the recipient uses the `from` value as its `to`.
- `notify_when_idle: true` gives the sender a one-shot notification when the peer next goes idle.

**Test:** sent "reply ack" to `agent-msg-f5`. It answered `ack`, and the reply was pushed straight back into this session.

---

## 2. Codex architecture: the shared app-server daemon

Since roughly v0.148, interactive `codex` TUIs don't run their threads in-process. They are clients of one shared, auto-managed **app-server daemon** per user:

```
codex app-server --listen unix:// --managed-daemon      # the daemon (auto-started)
```

- Control socket: `~/.codex/app-server-control/app-server-control.sock`, a symlink to `/private/tmp/codex-daemon-<uid>/<hash>`.
  - Confirm with `codex app-server daemon version`.
- `lsof` shows every TUI holding a connection to this socket. Any other client that connects can list and act on those TUIs' live threads.
- **Not on the daemon:** `codex --no-daemon`, `codex exec`, and the VS Code extension's own bundled `codex app-server` process.
  - `~/.codex/ipc/ipc.sock` is a separate socket, used by the VS Code host.

### Transport

- The socket speaks **WebSocket over the Unix domain socket**. The client has to do the HTTP `Upgrade: websocket` handshake first; the server answers `101 Switching Protocols`.
- After the handshake, each JSON-RPC message is one text frame. `"jsonrpc":"2.0"` is optional.
- `codex app-server proxy` relays raw stdio bytes and doesn't do the WebSocket handshake. Writing JSONL through it fails with a broken pipe.

### Handshake

```json
{"id":1,"method":"initialize","params":{"clientInfo":{"name":"my-client","version":"0.1"},"capabilities":{"experimentalApi":true}}}
{"method":"initialized"}
```

`experimentalApi: true` is required for the `thread/queue/*` methods. Without it they return `-32600 ... requires experimentalApi capability`.

### Discovery

| Method | Purpose |
|--------|---------|
| `thread/loaded/list {}` | IDs of threads currently loaded in the daemon (live sessions) |
| `thread/read {threadId, includeTurns?}` | Thread metadata (`cwd`, `name`, `status`, `source`, `preview`). With `includeTurns: true`, also the full turn/item history |
| `thread/list {...}` | Search saved sessions (filter by `cwd`, `searchTerm`, …) |

Additional ways to find sessions:
- `~/.codex/session_index.jsonl` maps session names to IDs.
- `codex agents` is a TUI that browses all sessions on the daemon.

To find the Codex agents in a given workdir, run `thread/loaded/list`, then `thread/read` each ID and filter on `cwd`.
Sub-agent threads report `source.subAgent.thread_spawn.parent_thread_id`.

### Sending input

| Situation | Method | Notes |
|-----------|--------|-------|
| Thread idle | `turn/start {threadId, input:[{type:"text", text:"…", text_elements:[]}]}` | Starts a new turn right away. Optional overrides (model, cwd, sandbox, …): omit them so the thread's settings don't change |
| Thread busy, join the current turn | `turn/steer {threadId, expectedTurnId, input}` | Fails if `expectedTurnId` isn't the active turn |
| Queue for later | `thread/queue/add {threadId, clientUserMessageId, input}` or `codex queue --thread <id\|exact-name> --message "…"` | See the queue caveat below |
| Force a queued item to run | `thread/queue/start {threadId, queuedSubmissionId?}` | Also available: `thread/queue/list`, `thread/queue/update`, `thread/queue/delete`, `thread/queue/reorder` |
| Add context without a turn | `thread/inject_items {threadId, items:[<Responses API items>]}` | The model sees the items on its next turn |
| Stop a turn | `turn/interrupt` | |

### Reading the reply

- **Live:** stay connected after `turn/start` and watch notifications for that `threadId`:
  - `item/completed` where `item.type == "agentMessage"` carries the reply text.
  - `turn/completed` marks the end of the turn.
- **After the fact:** `thread/read {threadId, includeTurns:true}` and take the `agentMessage` that follows your `userMessage`.
  - Full thread histories can be large (hundreds of KB), so don't truncate the JSON before parsing it.

---

## 3. Test results (Claude → Codex)

Two Codex TUIs were running in `~/Dev/agent-msg`:

| Thread | PID/TTY | Status at send | Method | Result |
|--------|---------|----------------|--------|--------|
| `01a0f3cc-acd2-74a3-b786-c2fd454382b1` | 82039 / ttys001 | idle | `turn/start` | Replied `ack` (~2.7 s turn) |
| `01a0f3cc-5bc8-7930-9b08-4cc7e62a7fd9` ("Discover local agent processes") | 80240 / ttys006 | active | `codex queue` | Queued as `01a0f3da-f771-…`. **Still in the queue after the thread went idle** (confirmed with `thread/queue/list`) |

**Queue caveat:** the queue did not drain automatically when the thread became idle. That's the opposite of the documented "starts a new turn when idle" behaviour. For reliable delivery:
- use `turn/start` when the thread is idle, or
- follow `thread/queue/add` with `thread/queue/start`.

At the same time, the Codex agent in thread `…5bc8` independently messaged thread `…acd2` over the same daemon ("Reply exactly CODEX_PEER_ACK_20260930"), and got that reply. This confirms Codex → Codex native messaging.

---

## 4. Codex → Claude

**Replies to a Claude prompt are pulled.** When Claude messages Codex, the Codex agent's answer is written to its own thread on the daemon. It isn't delivered to the sender. Claude reads the thread, or listens for daemon notifications while connected.

**Codex-initiated push works.** Codex has no built-in tool that addresses a Claude Code session. Even so, the Codex agent in this workdir reached a Claude session through Claude's own messaging path:

- It wrote and ran a helper, `claude_probe.py --send --wait 45 --out claude-experiment.json`.
  - The helper ran as a child of the Codex app-server daemon, in `~/Dev/agent-msg`.
- The helper listened on its own socket, `/tmp/cc-socks/22633.sock`, under the peer name `codex-native-probe`.
- It pushed a message into the Claude session (`/tmp/cc-socks/84207.sock`). The message arrived as a normal peer message, with a `from="uds:/tmp/cc-socks/22633.sock"` reply address.
- Claude answered with its native `SendMessage` tool (`to: "uds:/tmp/cc-socks/22633.sock"`). The send was accepted.

So both directions can be native push. Claude → Codex goes through the Codex daemon. Codex → Claude goes through a helper that speaks Claude's peer-messaging protocol. The original probe scripts and experiment captures have been retired; the maintained implementation is the `agent_msg` package under `src/agent_msg/`, with tests in `test/`.

---

## 5. Minimal Python client

Requires `pip install websockets`.

```python
import json, os, time
from websockets.sync.client import unix_connect

ws = unix_connect(os.path.expanduser("~/.codex/app-server-control/app-server-control.sock"),
                  uri="ws://localhost/")
_id = 0
def call(method, params=None, timeout=20):
    global _id; _id += 1; rid = _id
    ws.send(json.dumps({"id": rid, "method": method, "params": params or {}}))
    end = time.time() + timeout
    while time.time() < end:
        try: m = json.loads(ws.recv(timeout=1))
        except TimeoutError: continue
        if m.get("id") == rid and "method" not in m: return m

call("initialize", {"clientInfo": {"name": "probe", "version": "0.1"},
                    "capabilities": {"experimentalApi": True}})
ws.send(json.dumps({"method": "initialized"}))

# discover threads in a workdir
for tid in call("thread/loaded/list")["result"]["data"]:
    t = call("thread/read", {"threadId": tid})["result"]["thread"]
    print(tid, t["cwd"], t["status"])

# send to an idle thread
call("turn/start", {"threadId": "<uuid>",
                    "input": [{"type": "text", "text": "Please reply ack", "text_elements": []}]})
```

The protocol schemas can be generated locally with `codex app-server generate-json-schema --experimental --out DIR`.

## References

- Codex app-server docs: https://learn.chatgpt.com/docs/app-server
- App-server README: https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md
- `codex queue` PR: https://github.com/openai/codex/pull/39092
- v0.149 write-up (multi-session / `codex queue`): https://codex.danielvaughan.com/2026/08/29/codex-cli-v0149-multi-session-agents-dashboard-codex-queue-working-directory/
