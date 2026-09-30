# agent-msg

`agent-msg` sends messages between running Claude Code and Codex sessions on the same machine. It provides a Python CLI, plugins for both harnesses, a SQLite message journal, and a local read-only web interface.

It handles session discovery, target selection, delivery, and correlated replies. Agents use one command interface instead of implementing each harness's transport themselves.

## How it works

- **Codex:** connects to the shared app-server daemon. Uses `turn/start` for an idle session and `turn/steer` for a busy session.
- **Claude Code:** authenticates to the session's local peer-messaging socket and submits a native peer message.
- **Protocol:** wraps the message with an ID, timestamp, sender and recipient identities, message text, and an `agent-msg` reply route. Replies reference the original message ID.
- **Journal:** records each message before dispatch, then appends delivery events. Separately records the invoking process's PID, parent PID, UID, executable, workdir, and ancestry.
- **Replies:** the recipient runs `agent-msg reply MESSAGE_ID 'response'`. The program resolves the destination from the local journal. Reply routes contain no socket addresses, credentials, executable commands, or database paths.

`--wait` polls the journal for a reply from the addressed agent. Without it, a reply also notifies the original sender through its native transport when the sender can be identified. A normal answer in the recipient's conversation is not a journaled reply.

Prefer Claude's native tools for Claude-to-Claude messages, and each harness's own tools for its subagents.

## Install

Requires Python 3.10+, macOS or Linux, and running local Claude Code or Codex sessions. The package has no Python runtime dependencies. Discovery uses OS process tools, including `lsof` on macOS.

### Plugins

This checkout includes a self-contained plugin and local marketplace:

```sh
claude plugin marketplace add ./plugins
claude plugin install agent-msg@agent-msg-local

codex plugin marketplace add ./plugins
codex plugin add agent-msg@agent-msg-local
```

Restart sessions after installation. The plugin bundles the Python code and a skill-local launcher; pip installation is not required. Its skill resolves that launcher directly rather than relying on PATH.

### Python package

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

For a standalone skill without the plugin, run `agent-msg skill install --harness both`. Use either the plugin or the standalone skill to avoid duplicates. No package has been published to PyPI yet.

## Use

```sh
agent-msg agents
agent-msg send 'claude:reviewer' 'Please check the parser change.' --wait --timeout 300
printf '%s\n' 'The tests passed.' | agent-msg send 'codex:builder'
agent-msg reply MESSAGE_ID 'Reviewed; two cases need tests.'

agent-msg log --search parser --json
agent-msg show MESSAGE_ID --conversation --json
agent-msg ui
```

Targets are exact session names or full thread IDs, optionally prefixed with `claude:` or `codex:`. Discovery and sends default to the current workdir. `agents --all` lists other workdirs; `send --cwd DIR` selects an exact destination workdir. The caller must have authorization to contact it.

Messages can be supplied as an argument or through stdin. Add `--json` for structured output. In a plugin-only installation, substitute the bundled `scripts/agent-msg` launcher for `agent-msg`.

## Shared journal

Every participant must use the same journal. The default is `$XDG_STATE_HOME/agent-msg/journal.db`, falling back to `~/.local/state/agent-msg/journal.db`.

To select a different database, put the global option before the command:

```sh
agent-msg --db /trusted/path/journal.db send 'claude:reviewer' 'Please reply ack.' --wait
```

Configure the same path locally on both sides. `agents --json` and send/reply results report `db_path` to help detect mismatches. Incoming messages cannot select the database.

The web server runs only while `agent-msg ui` is running; it is not an MCP server or an always-on service. The command opens a browser window automatically at the token-bearing URL on an available local port. It uses `agent-msg.local` if that name resolves to loopback, otherwise `127.0.0.1`. Use `--no-open` for a headless session; Ctrl+C stops the server. Messaging does not require the UI.

The web interface shows conversations, delivery events, and declared versus observed identity. It binds to loopback, requires a per-launch token for journal APIs, and cannot send messages. SQL writes are disabled; SQLite may create WAL coordination files when opening a journal.

## Delivery and limits

- `sent` means native transport acceptance or a completed socket write, not proof that the peer read the message.
- `received` means a reply was journaled without a native notification.
- `failed` means dispatch failed before a native send began. `uncertain` means delivery may have occurred; do not automatically resend.
- A crash can leave `prepared` or `dispatching` records without an outcome.
- A wait timeout does not retract the message. Late replies stay in the journal; inspect them with `show --conversation` rather than resending.

Native harness protocols are version-sensitive. Live round trips have been tested on macOS, including a busy Codex peer; Linux live transports and restricted sandbox configurations have not been validated. Sandboxes must permit the journal and native socket access. The package does not disable them.

Process observations are audit information, not cryptographic authentication. Codex attribution uses an environment-provided thread ID; Claude attribution uses a live registry ancestor match. Another process running as the same OS user can alter local state. Message bodies are stored as supplied and may contain sensitive content.

## Development

```sh
python -m unittest discover -s test -p 'test_*.py' -v
python -m pip wheel . --no-deps --wheel-dir dist
agent-msg plugin export ./new-plugin-directory
```

`src/agent_msg/` is the canonical implementation. `plugins/agent-msg/` is a generated bundle; regenerate it after source changes. Tests live in `test/`. Live messaging tests require explicit environment opt-in; ordinary test runs do not contact agents. Browser checks in `test/ui_smoke.py` require Playwright only in the test environment.

Exit codes: `0` success, `1` operation error, `2` invalid arguments, `3` reply timeout, `130` interrupted.
