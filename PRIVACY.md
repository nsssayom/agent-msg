# Data handling

agent-msg runs on your computer. It has no publisher-operated service, telemetry,
analytics, or automatic update checker.

Discovery reads local process information and harness session metadata to find
running agents. Depending on the harness, this includes session names, thread IDs,
working directories, status, and transcript metadata. The Claude delivery adapter
reads a local peer-authentication credential for the selected, revalidated session.
That credential is used for the local connection and is not placed in messages or
the journal. Process command lines are parsed transiently, not stored in the journal.

The SQLite journal stores message bodies, sender and recipient identity claims,
reply relationships, delivery events, and separately observed process information:
PID, parent PID, user ID, hostname, working directory, executable, start time,
process ancestry, and observation time. Messages and error details can contain
sensitive data supplied by users or agents. The journal is not encrypted.

The default journal is `$XDG_STATE_HOME/agent-msg/journal.db`, or
`~/.local/state/agent-msg/journal.db` when that variable is unset or relative.
`--db` selects another local path. New journal files are restricted to the owning
OS user. Another process running as that user can still read or modify them.

Delivery uses local harness sockets. Once delivered, the receiving harness may
send the message to its model provider and retain it in its own transcript under
that provider's settings and policies. agent-msg does not control that processing.

The optional web inspector binds to loopback and uses a temporary bearer token.
It loads no remote assets. The browser keeps the token in tab session storage;
the startup URL should be treated as private. The inspector is read-only.

There is no automatic journal retention limit. To remove a journal, stop agent-msg
commands and the inspector using it, then delete the selected database and any
adjacent `-wal` and `-shm` files. This does not remove harness transcripts, backups,
or copies already sent to another agent. Exported JSON and copied browser content
must be managed separately.

Installation through GitHub or a plugin directory is subject to that service's
own data practices. Do not include private journals, tokens, or transcripts in
[public bug reports](https://github.com/nsssayom/agent-msg/issues).
