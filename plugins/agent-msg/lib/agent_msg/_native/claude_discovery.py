#!/usr/bin/env python3
"""Read-only, dependency-free discovery of local Claude Code sessions.

macOS and Linux are supported. A table is shown by default; --json emits the full
report, using the same threads/processes layout as discover-codex-agents.py.
Discovery never sends
messages, opens peer connections, reads authentication tokens, or changes files.
Session identity is (PID, process start time, session ID), not PID alone.

Examples:
  discover-claude-agents.py                 # table of all running sessions
  discover-claude-agents.py --cwd .         # sessions in this directory or below
  discover-claude-agents.py --json          # full machine-readable report
  discover-claude-agents.py --name claude   # filter names (case-insensitive)
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time

MAX_RECORD_BYTES = 256 * 1024
REGISTRY_FIELDS = (
    'sessionId', 'name', 'nameSource', 'cwd',
    'startedAt', 'updatedAt', 'status', 'statusUpdatedAt', 'waitingFor',
    'version', 'kind', 'entrypoint', 'pidDomain', 'peerProtocol', 'peerFeatures',
    'messagingSocketPath', 'jobId', 'agent', 'tmux', 'logPath', 'hostSessionId',
    'bridgeSessionId',
)


def command(argv, warnings):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=15,
                                env={**os.environ, 'LC_ALL': 'C'})
    except (OSError, subprocess.TimeoutExpired) as error:
        warnings.append(f'{argv[0]} unavailable: {error}')
        return ''
    if result.returncode and not result.stdout:
        warnings.append(f'{argv[0]} returned exit status {result.returncode}')
    return result.stdout


def parse_start(value, utc=False):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(' '.join(value.split()), '%a %b %d %H:%M:%S %Y')
        return parsed.replace(tzinfo=timezone.utc).timestamp() if utc else parsed.timestamp()
    except ValueError:
        return None


def parse_processes(output):
    processes = {}
    # Keep command-line arguments only in memory for classification; prompts and
    # tokens in those arguments must never appear in the emitted report.
    pattern = re.compile(r'^\s*(\d+)\s+(\d+)\s+(\d+)\s+(\S+)\s+'
                         r'(\w{3}\s+\w{3}\s+\d+\s+[\d:]+\s+\d{4})\s+(.*)$')
    for line in output.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        pid, parent, uid, tty, start, args = match.groups()
        processes[int(pid)] = {'pid': int(pid), 'ppid': int(parent), 'uid': int(uid),
                               'tty': None if tty in ('?', '??') else tty,
                               'startedAtEpoch': parse_start(start), '_args': args}
    return processes


def classify_process(process):
    args = process.get('_args', '')
    try:
        words = shlex.split(args)
    except ValueError:
        words = args.split()
    if not words:
        return None
    executable = words[0]
    name = Path(executable).name
    native_version = '/claude/versions/' in executable and bool(re.fullmatch(r'\d+\.\d+\.\d+.*', name))
    node_cli = name in ('node', 'nodejs', 'bun') and len(words) > 1 and '/@anthropic-ai/claude-code/' in words[1]
    if name != 'claude' and not native_version and not node_cli:
        return None
    process['launchExecutable'] = executable
    if node_cli:
        process['entryScript'] = words[1]
    subcommand_index = 2 if node_cli else 1
    subcommand = words[subcommand_index] if len(words) > subcommand_index else ''
    if subcommand.startswith('--') and subcommand[2:] in ('bg-pty-host', 'bg-spare'):
        subcommand = subcommand[2:]
    if subcommand in ('daemon', 'bg-pty-host', 'bg-spare', 'gateway'):
        return subcommand
    if subcommand in ('auth', 'mcp', 'plugin', 'plugins', 'doctor', 'update', 'install',
                      'agents', 'logs', 'stop', 'rm', 'respawn', 'project', 'setup-token'):
        return 'management'
    return 'session'


def discover_homes(processes, explicit, warnings):
    homes = {Path(path).expanduser().absolute() for path in explicit}
    homes.add(Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude'))).expanduser().absolute())
    uids = {p['uid'] for p in processes.values() if classify_process(p)}
    for uid in uids:
        try:
            home = Path(pwd.getpwuid(uid).pw_dir) / '.claude'
            if home.exists():
                homes.add(home)
        except (KeyError, OSError) as error:
            warnings.append(f'Could not resolve home for UID {uid}: {error}')
    return sorted(homes, key=str)


def process_files(pids, warnings):
    metadata = {pid: {'cwd': None, 'executable': None, 'openSocketPaths': []} for pid in pids}
    if sys.platform.startswith('linux'):
        for pid in pids:
            for entry, field in [('cwd', 'cwd'), ('exe', 'executable')]:
                try:
                    metadata[pid][field] = os.readlink(f'/proc/{pid}/{entry}')
                except OSError:
                    pass
        # Resolve socket inodes without opening a connection.
        inode_paths = {}
        try:
            for line in Path('/proc/net/unix').read_text().splitlines()[1:]:
                parts = line.split(maxsplit=7)
                if len(parts) == 8:
                    inode_paths[parts[6]] = parts[7]
            for pid in pids:
                try:
                    for fd in Path(f'/proc/{pid}/fd').iterdir():
                        try:
                            link = os.readlink(fd)
                        except OSError:
                            continue
                        match = re.fullmatch(r'socket:\[(\d+)\]', link)
                        if match and match[1] in inode_paths:
                            metadata[pid]['openSocketPaths'].append(inode_paths[match[1]])
                except OSError:
                    pass
        except OSError as error:
            warnings.append(f'Linux socket inspection unavailable: {error}')
    elif shutil.which('lsof') and pids:
        output = command(['lsof', '-nP', '-a', '-p', ','.join(map(str, pids)), '-F', 'pftn'], warnings)
        pid = descriptor = kind = None
        for line in output.splitlines():
            if line.startswith('p'):
                pid = int(line[1:])
            elif line.startswith('f'):
                descriptor, kind = line[1:], None
            elif line.startswith('t'):
                kind = line[1:]
            elif line.startswith('n') and pid in metadata:
                value = line[1:]
                if descriptor == 'cwd':
                    metadata[pid]['cwd'] = value
                elif descriptor == 'txt' and ('/claude/' in value or Path(value).name == 'claude'):
                    metadata[pid]['executable'] = metadata[pid]['executable'] or value
                elif kind == 'unix' and value.startswith('/'):
                    metadata[pid]['openSocketPaths'].append(value)
    else:
        warnings.append('Process cwd/socket inspection unavailable: lsof is not installed')
    return metadata


def read_record(path):
    # No following symlinks; bounded read; tolerate records being published in place.
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    for attempt in range(2):
        fd = os.open(path, flags)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECORD_BYTES:
                raise ValueError('registry record is not a bounded regular file')
            raw = stream.read(MAX_RECORD_BYTES + 1)
        if len(raw) > MAX_RECORD_BYTES:
            raise ValueError('registry record exceeds size limit')
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            if attempt == 0:
                time.sleep(0.025)
                continue
            raise
        if not isinstance(record, dict):
            raise ValueError('registry record must contain an object')
        pid = record.get('pid')
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or path.stem != str(pid):
            raise ValueError('invalid or mismatched registry PID')
        if not isinstance(record.get('sessionId'), str) or not record['sessionId']:
            raise ValueError('missing session ID')
        return record
    raise ValueError('unreadable registry record')


def validate_identity(record, process):
    if process is None:
        return False, 'process-not-running'
    domain = record.get('pidDomain')
    expected_domain = 'darwin' if sys.platform == 'darwin' else 'linux'
    if domain is not None and domain != expected_domain:
        return False, 'foreign-pid-domain'
    claimed = parse_start(record.get('procStart'), utc=True)
    actual = process.get('startedAtEpoch')
    if claimed is not None and actual is not None:
        if abs(claimed - actual) > 2:
            return False, 'process-start-mismatch'
        return True, 'verified-start-time'
    # A matching process name cannot establish that an old PID record belongs
    # to this process. Keep the live process, but discard unverifiable metadata.
    return False, 'missing-process-start-time'


def refresh_candidates(candidates, processes, discarded, warnings):
    """Reread mutable registry fields against a fresh process snapshot."""
    refreshed = []
    for home, path, _previous, _ in candidates:
        try:
            record = read_record(path)
            valid, identity = validate_identity(record, processes.get(record['pid']))
            if path.lstat().st_uid != processes.get(record['pid'], {}).get('uid'):
                valid, identity = False, 'registry-owner-mismatch'
            if not valid:
                discarded.append({'registryFile': str(path), 'pid': record['pid'], 'reason': identity})
                continue
            refreshed.append((home, path, record, identity))
        except (OSError, ValueError) as error:
            warnings.append(f'Registry changed during discovery at {path}: {error}')
    return refreshed


def socket_metadata(path, home, pid, process, files):
    result = {'path': path, 'address': None, 'exists': False, 'isSocket': False,
              'ownerUid': None, 'mode': None, 'heldByProcess': None,
              'credentialFile': None, 'credentialFilePresent': False,
              'usableByCurrentUser': False}
    if not isinstance(path, str) or not os.path.isabs(path):
        return result
    result['address'] = 'uds:' + path
    try:
        info = os.lstat(path)
        result.update(exists=True, isSocket=stat.S_ISSOCK(info.st_mode),
                      ownerUid=info.st_uid, mode=oct(stat.S_IMODE(info.st_mode)))
    except OSError as error:
        result['inspectionError'] = str(error)
    paths = files.get('openSocketPaths', [])
    if paths:
        result['heldByProcess'] = any(os.path.realpath(p) == os.path.realpath(path) for p in paths)
    digest = hashlib.sha256(os.path.abspath(path).encode()).hexdigest()
    credential = home / 'sessions' / f'{pid}.{digest}.key'
    result['credentialFile'] = str(credential)
    try:
        info = credential.lstat()
        result['credentialFilePresent'] = stat.S_ISREG(info.st_mode) and info.st_uid == process['uid']
        result['credentialReadableByCurrentUser'] = result['credentialFilePresent'] and os.access(credential, os.R_OK)
    except OSError:
        result['credentialReadableByCurrentUser'] = False
    result['usableByCurrentUser'] = bool(result['isSocket'] and result['ownerUid'] == os.getuid()
        and result['heldByProcess'] is True and result['credentialReadableByCurrentUser'])
    return result


def transcript_metadata(home, record):
    cwd = record.get('cwd')
    sid = record.get('sessionId')
    if not isinstance(cwd, str) or not isinstance(sid, str) or '/' in sid or '\\' in sid:
        return None
    project = re.sub(r'[^A-Za-z0-9]', '-', cwd)
    path = home / 'projects' / project / f'{sid}.jsonl'
    if not path.is_file():
        return None
    info = path.stat()
    result = {'path': str(path), 'bytes': info.st_size,
              'modifiedAtEpoch': info.st_mtime, 'preview': None}
    with path.open('rb') as stream:
        first = stream.read(128 * 1024)
        stream.seek(max(0, info.st_size - 256 * 1024))
        last = stream.read(256 * 1024)
    for line in reversed(last.splitlines()):
        try:
            entry = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(entry, dict) and entry.get('type') == 'assistant':
            message = entry.get('message')
            if isinstance(message, dict) and isinstance(message.get('model'), str):
                result['lastObservedModel'] = message['model']
                result['modelObservedAt'] = entry.get('timestamp')
                break
    for line in first.splitlines():
        try:
            entry = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(entry, dict) or entry.get('type') != 'user' or entry.get('isMeta') or entry.get('isSynthetic'):
            continue
        if isinstance(entry.get('origin'), dict) and entry['origin'].get('kind') == 'peer':
            continue
        message = entry.get('message')
        if not isinstance(message, dict):
            continue
        content = message.get('content')
        if isinstance(content, list):
            if any(isinstance(block, dict) and block.get('type') == 'tool_result' for block in content):
                continue
            content = ' '.join(block.get('text', '') for block in content
                               if isinstance(block, dict) and block.get('type') == 'text'
                               and isinstance(block.get('text'), str))
        if isinstance(content, str) and content.strip():
            result['preview'] = ' '.join(content.split())[:200]
            break
    return result


def iso(epoch):
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
        return None
    try:
        return datetime.fromtimestamp(epoch, timezone.utc).astimezone().isoformat(timespec='seconds')
    except (ValueError, OverflowError, OSError):
        return None


def within_cwd(path, root):
    if not isinstance(path, str):
        return False
    path, root = os.path.realpath(path), os.path.realpath(os.path.expanduser(root))
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def compatible_thread(session):
    """Use shared field names only where Claude provides equivalent evidence."""
    transcript = session.get('transcript') or {}
    native_status = session.get('status', 'unknown')
    status = 'active' if native_status in ('busy', 'shell', 'waiting') else native_status
    session_id = session.get('sessionId')
    process = session.get('process') or {}
    epoch = session.get('startedAt')
    created = epoch / 1000 if isinstance(epoch, (int, float)) else process.get('startedAtEpoch')
    updated = session.get('updatedAt')
    result = {'threadId': session_id, 'sessionId': session_id, 'pid': session['pid'],
            'name': session.get('name'), 'cwd': session.get('cwd'),
            'status': status, 'claudeStatus': native_status,
            'kind': session.get('kind', 'session'), 'source': session.get('entrypoint'),
            'agentRole': session.get('agent'),
            'cliVersion': session.get('version'), 'createdAt': iso(created),
            'updatedAt': iso(updated / 1000) if isinstance(updated, (int, float)) else None,
            'statusReportedAt': iso(session['statusUpdatedAt'] / 1000)
                if isinstance(session.get('statusUpdatedAt'), (int, float)) else None,
            'rolloutPath': transcript.get('path'), 'preview': transcript.get('preview'),
            'process': process}
    for key in ('nameSource', 'waitingFor', 'claudeHome', 'registryFile', 'discoverySource',
                'identityVerification', 'peerProtocol', 'peerFeatures', 'jobId', 'tmux',
                'logPath', 'hostSessionId', 'bridgeSessionId', 'messaging', 'transcript', 'limitations'):
        if key == 'waitingFor' and native_status != 'waiting':
            continue
        if session.get(key) is not None:
            result[key] = session[key]
    # No invented Codex-only fields (queue, active turn, sandbox, etc.). Model
    # observations stay in transcript metadata, never masquerading as live state.
    return {key: value for key, value in result.items()
            if value is not None or key in ('threadId', 'sessionId', 'name')}


def compatible_process(process, kind, linked_threads):
    return {**process, 'kind': kind, 'started': iso(process.get('startedAtEpoch')),
            'linkedThreads': linked_threads}


def discover(explicit_homes=(), cwd_filter=None):
    warnings, discarded, sessions, infrastructure = [], [], [], []
    processes = parse_processes(command(['ps', '-axo', 'pid,ppid,uid,tty,lstart,args'], warnings))
    if not processes:
        raise RuntimeError('No process snapshot available; cannot verify running sessions')
    homes = discover_homes(processes, explicit_homes, warnings)
    candidates = []
    for home in homes:
        directory = home / 'sessions'
        try:
            paths = list(directory.iterdir())
        except FileNotFoundError:
            continue
        except OSError as error:
            warnings.append(f'Cannot inspect {directory}: {error}')
            continue
        for path in sorted(paths):
            if not re.fullmatch(r'\d+\.json', path.name):
                continue
            try:
                record = read_record(path)
                valid, identity = validate_identity(record, processes.get(record['pid']))
                if not valid:
                    discarded.append({'registryFile': str(path), 'pid': record['pid'], 'reason': identity})
                    continue
                candidates.append((home, path, record, identity))
            except (OSError, ValueError) as error:
                warnings.append(f'Cannot read {path}: {error}')
    pids = {r['pid'] for _, _, r, _ in candidates}
    pids.update(pid for pid, p in processes.items() if classify_process(p))
    files = process_files(sorted(pids), warnings)
    # Drop processes that exited or whose PID was reused while inspecting files.
    # Registry status/name/cwd are then reread, rather than reusing the first read.
    latest_processes = parse_processes(command(['ps', '-axo', 'pid,ppid,uid,tty,lstart,args'], warnings))
    if not latest_processes:
        raise RuntimeError('Cannot revalidate the process snapshot')
    live_pids = {pid for pid in pids if pid in latest_processes
                 and processes[pid]['startedAtEpoch'] == latest_processes[pid]['startedAtEpoch']}
    processes = latest_processes
    pids = live_pids
    candidates = refresh_candidates(candidates, processes, discarded, warnings)
    candidates = [candidate for candidate in candidates if candidate[2]['pid'] in pids]
    registered_pids = set()
    for home, path, record, identity in candidates:
        pid = record['pid']
        registered_pids.add(pid)
        # Claude retains spare=true after a prewarmed worker is claimed. A jobId
        # identifies a claimed background session, which must remain discoverable.
        if record.get('spare') and not record.get('jobId'):
            infrastructure.append({'pid': pid, 'role': 'unclaimed-spare', 'registryFile': str(path)})
            continue
        process = {k: v for k, v in processes[pid].items() if not k.startswith('_')}
        process.update(files.get(pid, {}))
        try:
            process['user'] = pwd.getpwuid(process['uid']).pw_name
        except KeyError:
            process['user'] = str(process['uid'])
        session = {key: record[key] for key in REGISTRY_FIELDS if key in record}
        session.update(pid=pid, claudeHome=str(home), registryFile=str(path),
                       discoverySource='session-registry', identityVerification=identity, process=process)
        session['messaging'] = socket_metadata(record.get('messagingSocketPath'), home, pid, process, files.get(pid, {}))
        try:
            session['transcript'] = transcript_metadata(home, record)
        except OSError as error:
            session['transcript'] = None
            warnings.append(f'Transcript metadata unavailable for PID {pid}: {error}')
        sessions.append(session)
    for pid in sorted(pids - registered_pids):
        role = classify_process(processes[pid])
        process = {k: v for k, v in processes[pid].items() if not k.startswith('_')}
        process.update(files.get(pid, {}))
        if role == 'session':
            sessions.append({'pid': pid, 'sessionId': None, 'name': None,
                             'cwd': process.get('cwd'), 'status': 'unknown',
                             'discoverySource': 'process-only', 'identityVerification': 'process-name-only',
                             'process': process, 'messaging': None,
                             'limitations': ['No live registry record; session ID and inbox are unknown']})
        else:
            infrastructure.append({'pid': pid, 'role': role, 'process': process})
    daemon_record = next((p for p in infrastructure if p['role'] == 'daemon'), None)
    daemon_process = (daemon_record or {}).get('process') or {}
    daemon_executable = daemon_process.get('executable') or ''
    daemon_version = Path(daemon_executable).name if '/versions/' in daemon_executable else None
    if cwd_filter:
        sessions = [s for s in sessions if within_cwd(s.get('cwd'), cwd_filter)]
        infrastructure = [p for p in infrastructure if within_cwd(p.get('process', {}).get('cwd'), cwd_filter)]
    sessions.sort(key=lambda s: (s.get('cwd') or '', s['pid']))
    threads = [compatible_thread(s) for s in sessions]
    process_reports = []
    for thread in threads:
        process = compatible_process(thread['process'], thread.get('kind') or 'session',
                                     [thread['threadId']] if thread['threadId'] else [])
        process_reports.append(process)
        thread['process'] = {**process, 'match': thread['identityVerification']}
    for item in infrastructure:
        process = item.get('process') or processes.get(item['pid'], {})
        process = {k: v for k, v in process.items() if not k.startswith('_')}
        process_reports.append(compatible_process(process, item['role'], []))
    daemon = {'status': 'running' if daemon_record else 'not-found',
              'socket': next((p for p in daemon_process.get('openSocketPaths', [])
                              if p.endswith('/control.sock')), None),
              'pid': daemon_process.get('pid'),
              'cliVersion': daemon_version, 'kind': 'claude-background-daemon'}
    return {'schemaVersion': 2, 'generatedAt': datetime.now().astimezone().isoformat(timespec='seconds'),
            'hostname': socket.gethostname(), 'platform': sys.platform,
            'currentUid': os.getuid(), 'sessionCount': len(threads), 'daemon': daemon,
            'threads': threads, 'processes': sorted(process_reports, key=lambda p: p['pid']),
            'discardedRegistryRecords': discarded,
            'coverage': {'claudeHomes': list(map(str, homes)), 'cwdFilter': cwd_filter,
                         'scope': 'System process snapshot and accessible Claude registries',
                         'limitations': ['Other users may hide process details or registries',
                                         'Custom config homes require --claude-home when not auto-discovered',
                                         'Snapshot is observational; sessions may exit after discovery']},
            'warnings': warnings}


def short_home(path):
    if not path:
        return '-'
    home = str(Path.home())
    return '~' + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def display(value):
    # Session names and previews are untrusted. Strip terminal control characters.
    return ''.join(c for c in str(value) if c.isprintable())


def print_table(report, show_preview=False):
    daemon = report['daemon']
    print(f"Daemon: {daemon['status']}  socket={short_home(daemon.get('socket'))}  "
          f"cli={daemon.get('cliVersion') or '?'}")
    print(f"Registry: {', '.join(short_home(p) for p in report['coverage']['claudeHomes'])}  "
          f"sessions={report['sessionCount']}")
    threads = report['threads']
    if not threads:
        print('\nNo running Claude Code sessions.')
    else:
        headers = ['THREAD ID', 'NAME', 'KIND', 'STATUS', 'MODEL (LAST)', 'CWD', 'PID/TTY']
        rows = []
        for thread in threads:
            proc = thread['process']
            rows.append([display(v) for v in [thread.get('threadId') or '-', thread.get('name') or '-',
                         thread.get('kind') or '-', thread.get('status') or '?',
                         (thread.get('transcript') or {}).get('lastObservedModel') or '-', short_home(thread.get('cwd')),
                         f"{proc['pid']}/{proc.get('tty') or '-'}"]])
        widths = [max(len(header), *(len(row[i]) for row in rows)) for i, header in enumerate(headers)]
        print('\n' + '  '.join(h.ljust(w) for h, w in zip(headers, widths)))
        print('  '.join('-' * w for w in widths))
        for thread, row in zip(threads, rows):
            print('  '.join(c.ljust(w) for c, w in zip(row, widths)))
            if show_preview and thread.get('preview'):
                print('    └ ' + display(thread['preview'])[:110])
    unlinked = [p for p in report['processes'] if p['kind'] == 'session' and not p['linkedThreads']]
    if unlinked:
        print('\nClaude processes without a live registry session:')
        for proc in unlinked:
            print(f"  pid {proc['pid']:<6} {proc.get('tty') or '-':<8} {short_home(proc.get('cwd'))}")
    for warning in report['warnings']:
        print('\nwarning: ' + display(warning), file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--claude-home', action='append', default=[], metavar='DIR',
                        help='Additional Claude config home, repeatable (contains sessions/)')
    parser.add_argument('--json', action='store_true', help='Emit the full report as JSON')
    parser.add_argument('--cwd', help='Only sessions in this directory or its descendants')
    parser.add_argument('--name', help='Only sessions whose name contains this substring (case-insensitive)')
    parser.add_argument('--preview', action='store_true', help="Show each session's first-prompt preview in the table")
    args = parser.parse_args()
    if sys.platform != 'darwin' and not sys.platform.startswith('linux'):
        parser.exit(2, 'Supported platforms: macOS and Linux\n')
    try:
        report = discover(args.claude_home, args.cwd)
        if args.name:
            report['threads'] = [t for t in report['threads'] if args.name.lower() in (t.get('name') or '').lower()]
            report['sessionCount'] = len(report['threads'])
            selected_pids = {t['pid'] for t in report['threads']}
            report['processes'] = [p for p in report['processes'] if not p['linkedThreads'] or p['pid'] in selected_pids]
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print_table(report, args.preview)
    except (OSError, RuntimeError) as error:
        print(json.dumps({'error': str(error), 'sessions': []}), file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0
    return 0


if __name__ == '__main__':
    sys.exit(main())
