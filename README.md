# agent-msg

Native messaging between the coding agents you already have running.

`agent-msg` discovers local Claude Code, Codex, and OpenCode sessions, delivers messages through their native interfaces, and records messages and correlated replies in a shared SQLite journal. Sessions can communicate across working directories. Each agent keeps its own launch configuration, model, and permissions.

## Install

Requires Python 3.10+ and local access to the harness processes, their sockets, and a shared journal. There are no Python runtime dependencies. Live delivery is tested on macOS; Linux and restricted sandbox configurations remain unvalidated.

### CLI

Install the released wheel with [uv](https://docs.astral.sh/uv/):

```sh
uv tool install https://github.com/nsssayom/agent-msg/releases/download/v0.2.0/agent_msg-0.2.0-py3-none-any.whl
agent-msg --version
```

### Claude Code and Codex plugins

Install the plugin in each harness you use:

```sh
claude plugin marketplace add nsssayom/agent-msg
claude plugin install agent-msg@agent-msg

codex plugin marketplace add nsssayom/agent-msg
codex plugin add agent-msg@agent-msg
```

Each plugin includes the skill and a Python launcher. A separate CLI installation is optional: the skill finds its bundled launcher directly. Plugin installation does not put `agent-msg` on your terminal's PATH; in a plugin-only installation, use the bundled launcher in place of `agent-msg` in the examples below.

### OpenCode bridge and skill

OpenCode needs its native bridge as well as the skill:

```sh
agent-msg opencode install --symlink
```

This links the bridge and skill into the OpenCode configuration directory. Start a new OpenCode process and create or use a session to make it discoverable.

For running Claude sessions, type `/reload-plugins` directly in the session. In Codex, inspect `/plugins`; restart and resume if the skill is unavailable. See the [installation guide](docs/INSTALLATION.md) for activation details, custom configuration paths, standalone skills, and installation from source.

## Exchange messages

Agents run these commands through their shell tools so the invoking session can be identified. Start by discovering peers:

```sh
agent-msg agents
agent-msg agents --all
```

The first command lists sessions in the current workdir; `--all` includes other workdirs. Address a peer by its exact name or full session ID, optionally prefixed with `claude:`, `codex:`, or `opencode:`.

```sh
agent-msg send 'claude:reviewer' 'Please check the parser change.' --wait --timeout 300
```

The recipient replies using the message ID in the delivered envelope:

```sh
agent-msg reply MESSAGE_ID 'Reviewed; two cases need tests.'
```

`reply` resolves the original sender from the journal and revalidates the live destination. It works across workdirs without changing directories or supplying another target. An ordinary answer in the recipient's conversation does not create a correlated reply.

For an initial send to another workdir, select that directory explicitly:

```sh
agent-msg send 'opencode:ses_...' 'Please review the tests.' --cwd /path/to/peer/project
```

With `--wait`, the sender waits for a correlated journal reply. Without it, a reply also notifies the original sender through its native transport when that session can be identified. Set `--timeout` for the expected response time; a timeout leaves the message and any late reply in the journal.

Omit the message argument or use `-` to read stdin. Add `--json` for structured output:

```sh
printf '%s\n' 'The tests passed.' | agent-msg send 'codex:builder' --json
```

### Use a shared journal

Every participant must use the same journal. The default is `$XDG_STATE_HOME/agent-msg/journal.db`, falling back to `~/.local/state/agent-msg/journal.db`.

To select another journal, put `--db` before the subcommand and configure the same path on every peer:

```sh
agent-msg --db /absolute/path/journal.db send 'claude:reviewer' 'Please reply ack.' --wait
```

`agents --json` and send/reply results include `db_path` to help diagnose mismatched settings. Incoming messages cannot choose the journal or supply socket addresses, credentials, or routing commands.

## Inspect conversations

```sh
agent-msg log --search parser --json
agent-msg show MESSAGE_ID --conversation --json
agent-msg ui
```

The read-only web interface shows conversations, delivery events, and declared versus observed identity. It binds to loopback and opens a browser with a per-launch access token. Use `--no-open` to run without opening a browser; Ctrl+C stops it. Messaging works independently of the UI.

## Delivery and limits

Each harness has its own delivery path:

- **Claude Code:** authenticates to the selected session's local peer-messaging socket and sends a native peer message.
- **Codex:** connects to the existing shared app-server daemon, using `turn/start` for an idle thread and `turn/steer` for an active turn.
- **OpenCode:** a local plugin bridges a private Unix socket to the supplied session SDK, including the TUI's in-process API. It submits asynchronous prompts without aborting active work. No HTTP listener is required.

Messages are journaled before dispatch. The journal also records delivery events and observed process metadata, including the caller's PID, UID, executable, workdir, and ancestry.

| Status | Meaning |
|---|---|
| `sent` | Native API acceptance or a completed socket write; not proof the peer read or acted on the message. |
| `received` | A reply was committed to the journal without a native notification. |
| `failed` | Dispatch failed before a native send began. |
| `uncertain` | Delivery may have occurred; do not automatically resend. |
| `prepared` / `dispatching` | An intermediate state that can remain after a crash. |

After a timeout or uncertain delivery, inspect the conversation before sending again. Native protocols are version-sensitive, and sandboxes must permit access to the journal and harness sockets. The package does not disable those restrictions. See the [0.2.0 release notes](docs/releases/0.2.0.md) for tested versions and live coverage.

Process observations provide audit information, not cryptographic authentication. Another process under the same OS account can alter local state. Messages may contain sensitive content, and the receiving harness may forward them to its model provider. See [data handling](PRIVACY.md) for storage and credential details.

Prefer each harness's own tools for its subagents, and Claude's native tools for Claude-to-Claude communication.

## Related project: agmsg

[agmsg](https://github.com/fujibee/agmsg) also provides durable messaging between coding agents. It organizes communication around [registered team identities](https://github.com/fujibee/agmsg/blob/v1.5.3/docs/teams.md) that can span projects and sessions, with broader harness support, role management, session spawning, and optional remote synchronization.

`agent-msg` focuses on addressing discovered live sessions through the native delivery paths described above, with correlated replies and a local journal. Team registration and session orchestration are outside its scope.

For reference, agmsg 1.5.3 uses these integration mechanisms (reviewed 2026-10-06):

| Harness | agmsg delivery integration |
|---|---|
| Claude Code | Inbox delivery through Monitor or turn hooks, depending on the selected [delivery mode](https://github.com/fujibee/agmsg/blob/v1.5.3/README.md#delivery-modes). |
| Codex CLI | Monitor mode uses a [launch wrapper](https://github.com/fujibee/agmsg/blob/v1.5.3/scripts/drivers/types/codex/codex-monitor.sh) that starts an app-server and connects the TUI through `--remote`. |
| Codex desktop | The [desktop driver](https://github.com/fujibee/agmsg/blob/v1.5.3/scripts/drivers/terminals/codex-app/README.md) identifies placement; it does not wake idle desktop conversations. |
| OpenCode | Monitor mode uses the external `opencode-sentinel` plugin and an agent-started watcher. Turn mode uses instructed inbox checks. See [OpenCode support](https://github.com/fujibee/agmsg/blob/v1.5.3/docs/opencode.md). |

These are differences in integration and scope, not a comparative reliability benchmark.

## Development

```sh
python -m unittest discover -s test -p 'test_*.py' -v
python -m pip wheel . --no-deps --wheel-dir dist
agent-msg plugin export ./new-plugin-directory
```

`src/agent_msg/` is the canonical implementation. `plugins/agent-msg/` is generated; regenerate it after source changes. Ordinary tests do not contact live agents. Live messaging tests require explicit opt-in; browser checks in `test/ui_smoke.py` require Playwright in the test environment.

An optional real OpenCode API check runs without model inference:

```sh
python test/opencode_native_smoke.py --opencode /path/to/opencode --output /path/to/new-smoke-output
```

CLI exit codes: `0` success, `1` operation error, `2` invalid arguments, `3` reply timeout, `130` interrupted.

The project is [MIT licensed](LICENSE) and independently distributed through its [GitHub releases](https://github.com/nsssayom/agent-msg/releases) and marketplace. There is no PyPI release or official-directory endorsement. See [release preparation](docs/RELEASING.md) and [directory submission](docs/SUBMISSION.md) for publishing procedures.
