# agent-msg

agent-msg sends messages between running Claude Code, Codex, and OpenCode sessions on the
same computer. It discovers local peers, addresses them by exact name or thread
ID, delivers through each harness's native messaging transport, and records
messages and correlated replies in a local SQLite journal.

## Requirements

Python 3.10+ and local access to the harness processes, their Unix sockets, and
a shared journal path. The plugin bundles its Python runtime code; it has no pip
dependencies and needs no hosted server. It runs in local coding-agent
environments, not cloud-only chats without access to your computer.

Live delivery has been tested on macOS with Claude Code 2.1.286 and Codex CLI
0.159.0 / daemon 0.159.2. Linux live delivery and restricted sandbox configurations
are not validated. Internal harness protocols can change between versions.

## Use

Ask the agent to use agent-msg to list peers in the current directory, send a
message to an agent in the other harness, or inspect the journal. The skill
locates its bundled launcher directly:

```sh
python3 /path/to/plugin/skills/agent-msg/scripts/agent-msg agents
python3 /path/to/plugin/skills/agent-msg/scripts/agent-msg send 'claude:reviewer' 'Please review the parser.' --wait
python3 /path/to/plugin/skills/agent-msg/scripts/agent-msg reply MESSAGE_ID 'Reviewed.'
python3 /path/to/plugin/skills/agent-msg/scripts/agent-msg ui
```

Replace `/path/to/plugin` with the actual installation directory. The UI opens
in a browser and runs until Ctrl+C. It is a read-only inspector, not a terminal.
Plugin installation does not install a global `agent-msg` shell command; the
[project README](https://github.com/nsssayom/agent-msg#install) explains the
separate CLI installation.

Both agents must use the same journal. The default is
`$XDG_STATE_HOME/agent-msg/journal.db`, falling back to
`~/.local/state/agent-msg/journal.db`. A trusted local `--db PATH` setting before
the subcommand selects another journal. Incoming messages cannot select its path.
The receiver must use `agent-msg reply` for a reply to be correlated and journaled.
A normal conversation answer does not satisfy `--wait`; after a timeout, inspect
the journal rather than resending.

In Claude Code, type `/reload-plugins` directly in a running session after plugin
installation. In Codex, inspect `/plugins`; restart and resume if the skill is
unavailable. Prefer each harness's own tools for subagents, and Claude's native
tools for Claude-to-Claude communication.

## Data and limits

Messages, identities, delivery events, and observed process metadata are stored
locally. Claude delivery reads a local peer-authentication credential for the
selected session; it is not included in the message or journal. A receiving
harness may forward message content to its model provider. The package has no
telemetry or publisher-operated service.

Identity claims are not cryptographically authenticated. A `sent` result records
transport acceptance, not proof that the peer read the message. The journal is
not encrypted and is accessible to other processes under the same OS account.
See [data handling](https://github.com/nsssayom/agent-msg/blob/main/PRIVACY.md).

This is an independent project, not an official product of either harness vendor.
MIT licensed. [Source and documentation](https://github.com/nsssayom/agent-msg).
[Report an issue](https://github.com/nsssayom/agent-msg/issues) without including
private journals, tokens, or transcripts.

## OpenCode bridge

Run the bundled launcher with `opencode install --symlink` to install the native OpenCode plugin and skill. Start a new OpenCode process and use a session before discovery. The bridge uses private Unix sockets and the native session SDK; no HTTP listener is required.
