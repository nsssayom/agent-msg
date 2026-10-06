# Installation and configuration

[Back to the README](../README.md)

Choose the CLI for a terminal command, or the Claude/Codex plugin for a bundled skill and launcher. OpenCode additionally needs its native bridge. This guide covers installation alternatives, activation, and configuration details.

Requires Python 3.10+, macOS or Linux, and running local Claude Code, Codex, or OpenCode sessions. The package has no Python runtime dependencies. Discovery uses OS process tools, including `lsof` on macOS.

## Python package

With [uv](https://docs.astral.sh/uv/), install the versioned release into an isolated environment and expose `agent-msg` on PATH:

```sh
uv tool install https://github.com/nsssayom/agent-msg/releases/download/v0.2.0/agent_msg-0.2.0-py3-none-any.whl
agent-msg --version
agent-msg ui
```

If uv reports that its executable directory is missing from PATH, run `uv tool update-shell` and open a new terminal. From a checkout, `uv tool install .` installs the current source instead.

Alternatively, use Python's built-in virtual environment support from a checkout:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
```

Activate that environment in each terminal where you want the command, or invoke `.venv/bin/agent-msg` directly. For a standalone skill without the plugin, run `agent-msg skill install --harness both`. Use either the plugin or the standalone skill to avoid duplicates. No package has been published to PyPI yet.

Release archives and checksums are on [GitHub Releases](https://github.com/nsssayom/agent-msg/releases). The code is [MIT licensed](../LICENSE). See [data handling](../PRIVACY.md) for what is read, stored, and sent to the harnesses.

## Claude Code and Codex plugins

Install from the GitHub marketplace:

```sh
claude plugin marketplace add nsssayom/agent-msg
claude plugin install agent-msg@agent-msg

codex plugin marketplace add nsssayom/agent-msg
codex plugin add agent-msg@agent-msg
```

For a local checkout, replace `nsssayom/agent-msg` with its absolute directory path. The earlier `./plugins` marketplace remains available as `agent-msg-local`; use one marketplace to avoid duplicate installs. This is a publisher-hosted marketplace, not an official-directory endorsement.

The plugin bundles the Python code and a skill-local launcher; pip installation is not required. Its skill resolves that launcher directly rather than relying on PATH. Plugin installation does not add `agent-msg` to your terminal's PATH; install the Python package for that command.

### Load into a running session

In **Claude Code**, type this directly into each running session after installing or updating the plugin:

```text
/reload-plugins
```

This applies pending plugin changes without restarting. Check the reload summary for errors. It reloads all active plugins; if it warns about MCP tool changes, review the warning before using `--force`. For a standalone skill installation, use `/reload-skills`. These are in-session commands, not shell commands. See [Claude's command reference](https://code.claude.com/docs/en/commands).

In **Codex**, use `/plugins` to inspect installation and enabled state. There is no equivalent reload slash command documented in the current [CLI reference](https://learn.chatgpt.com/docs/developer-commands?surface=cli). If the skill is unavailable, restart the CLI and resume your conversation with `codex resume`; for local plugin file changes in the desktop app, OpenAI documents an [app restart](https://developers.openai.com/plugins/build/plugins).

Codex's [app-server API](https://learn.chatgpt.com/docs/app-server) supports `skills/list` with `forceReload: true` and emits `skills/changed` notifications. That refreshes skill discovery; it is not a documented full-plugin reload command for CLI users.

## OpenCode

From an installed CLI or the bundled Python launcher:

```sh
agent-msg opencode install --symlink
agent-msg agents --harness opencode --json
agent-msg send 'opencode:ses_...' 'Please review the change.' --wait
```

The installer adds `plugins/agent-msg.js` and `skills/agent-msg` to the OpenCode config directory. It respects `OPENCODE_CONFIG_DIR`, otherwise `$XDG_CONFIG_HOME/opencode` or `~/.config/opencode`. `--dest DIR` selects a directory explicitly; `--symlink` follows the installed source. Existing customized files are never overwritten. Start a new OpenCode process to load the bridge, then create or use a session. Installing only the skill does not enable the transport.

The bridge uses the plugin's supplied [native SDK](https://opencode.ai/docs/sdk/) and [hooks](https://opencode.ai/docs/plugins/), without starting an HTTP server or reading credentials. Only sessions observed in that process are advertised; historical sessions are excluded. Exact titles or full session IDs work. If multiple processes host the same session ID, routing is rejected as ambiguous.

Registry files live in `$XDG_STATE_HOME/agent-msg/opencode` (default `~/.local/state/agent-msg/opencode`); private sockets live in `/tmp/agent-msg-UID`. All harnesses must share XDG state settings. An absolute `AGENT_MSG_OPENCODE_RUNTIME_DIR` can override the socket directory. Stale records are ignored after PID/start-time validation. Unix paths must fit the platform's socket limit.

Sender attribution uses per-command session metadata plus a live process ancestor. On OpenCode 1.18.34 the V2 bash tool lacks `shell.env`; the native `tool.execute.before` hook prefixes two exported identity variables, scoped to the command. No shell startup files or permissions are changed. Metadata is an auditable same-user claim, not authentication.

Delivery preserves the current agent, model and variant, leaving system instructions and tool permissions to OpenCode. Busy sessions consume the prompt through their existing loop. API acceptance is not proof of model processing; use `reply` for correlated acknowledgments. Socket drops after dispatch are uncertain and never automatically retried. Receipts deduplicate message IDs within a live bridge process.

## Shared journal and discovery

Every participant must use the same journal. The default is `$XDG_STATE_HOME/agent-msg/journal.db`, falling back to `~/.local/state/agent-msg/journal.db`. Place an explicit `--db /absolute/path/journal.db` before the subcommand and use the same setting on every peer. Incoming messages cannot choose a database path. `agents --json` and send/reply results report `db_path` for verification.

Peers also need consistent XDG state and OpenCode runtime settings for discovery. For Claude sessions launched under a custom configuration home, supply that home through `CLAUDE_CONFIG_DIR` when discovering or messaging them.
