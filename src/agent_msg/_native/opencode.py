"""Private local bridge to OpenCode's native session SDK (no TCP listener).

Registry entries are hints: verify owner, mode, live PID/start time and socket
before contacting the plugin. Wire envelopes never supply routing addresses.
"""
import json
import os
from pathlib import Path
import re
import socket
import stat

from . import claude_discovery

MAX_FRAME = 1024 * 1024
INSTANCE = re.compile(r'^[0-9]+-[0-9a-f]{16}$')


class Rejected(Exception):
    """The bridge explicitly rejected a request before native dispatch."""


def registry_dir():
    xdg = os.environ.get('XDG_STATE_HOME', '')
    base = Path(xdg) if Path(xdg).is_absolute() else Path.home() / '.local/state'
    return base / 'agent-msg/opencode'


def runtime_dir():
    override = os.environ.get('AGENT_MSG_OPENCODE_RUNTIME_DIR', '')
    return Path(override) if Path(override).is_absolute() else Path('/tmp') / f'agent-msg-{os.getuid()}'


def private_path(path, kind):
    info = path.lstat()
    if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError(f'not a private, current-user path: {path}')
    return info


def read_registry(path, processes):
    private_path(path.parent, stat.S_ISDIR)
    info = private_path(path, stat.S_ISREG)
    if info.st_size > 16384 or not INSTANCE.fullmatch(path.stem):
        raise ValueError('invalid OpenCode registry record')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(os.open(path, flags), 'rb') as stream:
        current = os.fstat(stream.fileno())
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError('OpenCode registry changed during discovery')
        record = json.loads(stream.read(16385))
    if not isinstance(record, dict):
        raise ValueError('invalid OpenCode registry record')
    pid = record.get('pid')
    process = processes.get(pid) if type(pid) is int else None
    if (record.get('version') != 1 or record.get('instance') != path.stem
            or str(pid) != path.stem.split('-')[0] or not process
            or process['uid'] != os.getuid()
            or process.get('startedAtEpoch') is None
            or claude_discovery.parse_start(record.get('process_start')) != process['startedAtEpoch']):
        raise ValueError('stale or invalid OpenCode process identity')
    if not isinstance(record.get('directory'), str) or not Path(record['directory']).is_absolute():
        raise ValueError('invalid OpenCode working directory')
    # Derive the socket locally, rather than following a path in a record.
    root = runtime_dir()
    private_path(root, stat.S_ISDIR)
    destination = root / (path.stem + '.sock')
    private_path(destination, stat.S_ISSOCK)
    return {**record, '_socket': str(destination)}


def request(record, operation, **fields):
    payload = json.dumps({'version': 1, 'instance': record['instance'], 'op': operation, **fields}).encode() + b'\n'
    if len(payload) > MAX_FRAME:
        raise Rejected('OpenCode bridge frame exceeds 1 MiB')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(10)
        try:
            client.connect(record['_socket'])
        except OSError as error:
            raise Rejected('OpenCode bridge is no longer reachable') from error
        # Once this write begins, failures are uncertain, never automatically retried.
        client.sendall(payload)
        response = bytearray()
        while b'\n' not in response:
            part = client.recv(65536)
            if not part:
                raise ConnectionError('OpenCode bridge closed without a receipt')
            response.extend(part)
            if len(response) > MAX_FRAME:
                raise ValueError('OpenCode bridge response exceeds 1 MiB')
    result = json.loads(response.split(b'\n', 1)[0])
    if not isinstance(result, dict) or result.get('instance') != record['instance']:
        raise ValueError('OpenCode bridge identity changed')
    if result.get('ok') is not True:
        error = str(result.get('error', 'OpenCode bridge failed'))
        if result.get('stage') == 'rejected':
            raise Rejected(error)
        raise RuntimeError(error)
    return result['result']


def discover():
    root = registry_dir()
    agents, warnings = [], []
    if not root.exists():
        return {'agents': agents, 'warnings': warnings}
    try:
        private_path(root, stat.S_ISDIR)
        paths = sorted(root.glob('*.json'))
    except (OSError, ValueError) as error:
        return {'agents': [], 'warnings': [f'OpenCode discovery: {error}']}
    processes = claude_discovery.parse_processes(claude_discovery.command(
        ['ps', '-axo', 'pid,ppid,uid,tty,lstart,args'], warnings))
    for path in paths:
        try:
            record = read_registry(path, processes)
        except (OSError, ValueError, TypeError):
            # Dead processes leave harmless registry files. Never delete them here.
            continue
        try:
            sessions = request(record, 'list')
            if not isinstance(sessions, list):
                raise ValueError('invalid OpenCode session list')
            for session in sessions:
                if (not isinstance(session, dict) or not isinstance(session.get('id'), str)
                        or not re.fullmatch(r'ses_[A-Za-z0-9_-]+', session['id'])
                        or session.get('directory') != record['directory']):
                    continue
                agents.append({'harness': 'opencode', 'thread_id': session['id'],
                    'name': session.get('title'), 'cwd': record['directory'], 'pid': record['pid'],
                    'status': session.get('status', 'unknown'), 'reachable': True,
                    'identity_evidence': 'plugin-process-start-and-session',
                    'instance': record['instance']})
        except (OSError, ValueError, RuntimeError, Rejected, KeyError) as error:
            warnings.append(f'OpenCode bridge {path.stem}: {error}')
    return {'agents': agents, 'warnings': warnings}


def deliver(agent, text, message_id):
    # Resolve routing afresh from local registry, never from an envelope or peer.
    report = discover()
    matches = [a for a in report['agents'] if a['thread_id'] == agent['thread_id']
               and Path(a['cwd']).resolve() == Path(agent['cwd']).resolve()]
    if len(matches) != 1:
        raise Rejected('OpenCode session is unavailable or hosted by multiple processes')
    target = matches[0]
    warnings = []
    processes = claude_discovery.parse_processes(claude_discovery.command(
        ['ps', '-p', str(target['pid']), '-o', 'pid,ppid,uid,tty,lstart,args'], warnings))
    try:
        record = read_registry(registry_dir() / (target['instance'] + '.json'), processes)
    except (OSError, ValueError, TypeError) as error:
        raise Rejected('OpenCode process changed before dispatch') from error
    return request(record, 'send', session_id=target['thread_id'], cwd=target['cwd'],
                   text=text, message_id=message_id)
