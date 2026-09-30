#!/usr/bin/env python3
"""Push a message into a running Claude Code session using native peer messaging.

Target an exact session name or full session ID from discover-claude-agents.py.
Both idle and busy peers receive a native message with priority 'next'.
Default output is human-readable; --json prints a machine-readable result.
Requires only Python's standard library and the sibling discovery script.

--wait asks Claude to reply using SendMessage to a temporary local socket. It
returns the first reply from that peer, not the completion of its entire task.
Without --wait, success means the socket write completed, not that Claude read it.
A timeout does not cancel or retract the message; do not blindly resend it.

Examples:
  push-to-claude.py 'claude[1]-msg' 'Please reply ack' --wait
  push-to-claude.py <session-id> 'Here is an update' --json
  echo 'Please reply ack' | push-to-claude.py 'claude[1]-msg' --wait --cwd .
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import html
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import sys
import time
import uuid

from . import claude_discovery as discover

WAIT_TIMEOUT = 600
MAX_FRAME_BYTES = 1024 * 1024
UUID_RE = re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$', re.I)


class PushError(Exception):
    pass


def resolve_target(target: str, homes=(), cwd=None) -> dict:
    threads = discover.discover(homes)['threads']
    if cwd is not None:
        threads = [t for t in threads if t.get('cwd')
                   and Path(t['cwd']).resolve() == Path(cwd).expanduser().resolve()]
    if UUID_RE.fullmatch(target):
        matches = [t for t in threads if (t.get('sessionId') or '').lower() == target.lower()]
    else:
        matches = [t for t in threads if t.get('name') == target]
    if not matches:
        raise PushError(f'no running Claude session matching {target!r}'
                        + (' in the requested directory' if cwd is not None else '')
                        + '; use an exact case-sensitive name or full session ID')
    if len(matches) != 1:
        listing = '\n'.join(f"  {t.get('sessionId')}  pid={t['pid']}  cwd={t.get('cwd')}" for t in matches)
        raise PushError(f'target {target!r} is ambiguous; use a unique session ID:\n{listing}')
    thread = matches[0]
    if not thread.get('registryFile') or not (thread.get('messaging') or {}).get('usableByCurrentUser'):
        raise PushError('session has no verified native messaging socket accessible to this user')
    if thread.get('peerProtocol') != 1:
        raise PushError(f"unsupported Claude peer protocol: {thread.get('peerProtocol')!r}")
    return thread


def peer_credentials(thread: dict) -> tuple[dict, str]:
    """Revalidate process/session identity immediately before reading its secret."""
    path = Path(thread['registryFile'])
    record = discover.read_record(path)
    warnings = []
    processes = discover.parse_processes(discover.command(
        ['ps', '-p', str(thread['pid']), '-o', 'pid,ppid,uid,tty,lstart,args'], warnings))
    process = processes.get(thread['pid'])
    valid, reason = discover.validate_identity(record, process)
    if not valid:
        raise PushError(f'peer is no longer the discovered process: {reason}')
    if (record['pid'] != thread['pid'] or record['sessionId'] != thread['sessionId']
            or record.get('cwd') != thread.get('cwd')
            or record.get('messagingSocketPath') != thread['messaging']['path']
            or process['startedAtEpoch'] != thread['process']['startedAtEpoch']):
        raise PushError('peer identity or destination changed during discovery; discover it again')
    if process['uid'] != os.getuid() or path.lstat().st_uid != os.getuid():
        raise PushError('peer registry/process belongs to another user')
    destination = record['messagingSocketPath']
    info = os.lstat(destination)
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise PushError('peer destination is not a socket owned by this user')
    digest = hashlib.sha256(os.path.abspath(destination).encode()).hexdigest()
    key_path = Path(thread['claudeHome']) / 'sessions' / f"{record['pid']}.{digest}.key"
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(os.open(key_path, flags), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > 65536:
            raise PushError('invalid peer credential file')
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise PushError('peer credential file exceeds size limit')
    try:
        credential = json.loads(raw)
    except (ValueError, UnicodeError):
        raise PushError('invalid peer credential data') from None
    if (not isinstance(credential, dict) or credential.get('procStart') != record.get('procStart')
            or not isinstance(credential.get('peerToken'), str) or not credential['peerToken']):
        raise PushError('peer credentials do not match the running process')
    return record, credential['peerToken']


def make_frame(record, text, msg_id, reply_address=None):
    attrs = 'from-name="agent-msg"'
    if reply_address:
        attrs += f' from="{html.escape(reply_address, quote=True)}"'
    content = f'<cross-session-message {attrs}>\n{html.escape(text, quote=False)}'
    if reply_address:
        content += ('\n\nThe sender is waiting for a reply. Use your native SendMessage tool '
                    f'to send your response to {html.escape(reply_address, quote=False)}. '
                    'A reply in your own conversation alone will not reach the sender.')
    content += '\n</cross-session-message>'
    frame = {'msgV': 1, 'type': 'user', 'session_id': record['sessionId'],
             'message': {'role': 'user', 'content': content}, 'priority': 'next',
             'msg_id': msg_id, 'sent_at': int(time.time() * 1000)}
    if reply_address:
        frame['from'] = reply_address
    return frame


def deliver(record, token, text, msg_id, reply_address=None):
    frame = make_frame(record, text, msg_id, reply_address)
    wire = (json.dumps({'type': 'auth', 'token': token}) + '\n' + json.dumps(frame) + '\n').encode()
    if len(wire) > MAX_FRAME_BYTES:
        raise PushError('message exceeds the 1 MiB wire-size limit')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(record['messagingSocketPath'])
        client.sendall(wire)


@contextmanager
def reply_listener():
    # Claude expects a PID-shaped native peer address. Never replace an existing
    # socket or register this short-lived helper as a Claude session.
    path = Path('/tmp/cc-socks') / f'{os.getpid()}.sock'
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    identity = None
    try:
        listener.bind(str(path))
        info = path.lstat()
        identity = (info.st_dev, info.st_ino)
        os.chmod(path, 0o600)
        listener.listen(8)
        yield listener, 'uds:' + str(path)
    finally:
        listener.close()
        if identity is not None:
            try:
                info = path.lstat()
                if (info.st_dev, info.st_ino) == identity:
                    path.unlink()
            except FileNotFoundError:
                pass


def reply_text(frame, expected_sender):
    if (not isinstance(frame, dict) or frame.get('type') != 'user'
            or frame.get('from') != expected_sender):
        return None
    message = frame.get('message')
    if not isinstance(message, dict) or message.get('role') != 'user':
        return None
    content = message.get('content')
    if not isinstance(content, str):
        return None
    envelope = re.fullmatch(r'<cross-session-message\b[^>]*>\s*\n?(.*?)\n?</cross-session-message>\s*',
                            content, re.S)
    return html.unescape(envelope[1]).strip() if envelope else content


def wait_for_reply(listener, expected_sender, timeout):
    # The private per-invocation socket and expected sender isolate this reply.
    # `from` is protocol metadata, not a cryptographic sender attestation.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        listener.settimeout(max(0.001, deadline - time.monotonic()))
        try:
            connection, _ = listener.accept()
        except socket.timeout:
            break
        with connection:
            buffer = b''
            total = 0
            while time.monotonic() < deadline:
                connection.settimeout(max(0.001, min(1, deadline - time.monotonic())))
                try:
                    chunk = connection.recv(4096)
                except (socket.timeout, ConnectionError):
                    break
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_FRAME_BYTES:
                    break
                buffer += chunk
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    try:
                        frame = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    reply = reply_text(frame, expected_sender)
                    if reply is not None:
                        return {'replyStatus': 'received', 'reply': reply,
                                'replyMessageId': frame.get('msg_id')}
    return {'replyStatus': 'timeout', 'reply': None}


def positive_seconds(value):
    try:
        value = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError('timeout must be a positive number') from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError('timeout must be a finite positive number')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('target', help='full session ID or exact session name')
    parser.add_argument('message', nargs='?', default='-', help="message text, or '-' / omitted to read stdin")
    parser.add_argument('--wait', action='store_true', help='ask for and wait for a native SendMessage reply')
    parser.add_argument('--timeout', type=positive_seconds, default=WAIT_TIMEOUT,
                        help=f'reply wait in seconds (default: {WAIT_TIMEOUT}; requires --wait)')
    parser.add_argument('--json', action='store_true', help='print the result as JSON')
    parser.add_argument('--cwd', help='only match sessions in this exact working directory')
    parser.add_argument('--claude-home', action='append', default=[], metavar='DIR',
                        help='additional Claude config home, repeatable')
    args = parser.parse_args()
    result = {'ok': False}
    try:
        text = (sys.stdin.read() if args.message == '-' else args.message).strip()
        if not text:
            raise PushError('empty message')
        thread = resolve_target(args.target, args.claude_home, args.cwd)
        record, token = peer_credentials(thread)
        msg_id = str(uuid.uuid4())
        result.update(threadId=record['sessionId'], sessionId=record['sessionId'],
                      name=record.get('name'), pid=record['pid'], cwd=record.get('cwd'),
                      mode='peer', priority='next', clientUserMessageId=msg_id)
        if args.wait:
            with reply_listener() as (listener, address):
                deliver(record, token, text, msg_id, address)
                result.update(deliveryStatus='written')
                result.update(wait_for_reply(listener, 'uds:' + record['messagingSocketPath'], args.timeout))
                result['ok'] = result['replyStatus'] == 'received'
        else:
            deliver(record, token, text, msg_id)
            result.update(ok=True, deliveryStatus='written')
    except (PushError, OSError, ValueError, RuntimeError) as error:
        result['error'] = str(error)
    except KeyboardInterrupt:
        result['error'] = 'interrupted; any message already written has not been retracted'
    if args.json:
        print(json.dumps(result, indent=2))
    elif result.get('error'):
        print('error: ' + discover.display(result['error']), file=sys.stderr)
    else:
        print(discover.display(f"sent via peer to {result.get('name') or '-'} ({result['threadId']}); socket write completed"))
        if args.wait:
            print(f"reply {result['replyStatus']}:")
            print(discover.display(result.get('reply') or '(no reply received)'))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
