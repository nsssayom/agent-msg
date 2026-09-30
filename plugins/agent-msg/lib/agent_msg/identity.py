"""Observe the invoking OS process independently of protocol identity claims."""
import os
from pathlib import Path
import socket
import subprocess
import sys

from ._native import claude_discovery
from .protocol import identity, now


def observe_process():
    warnings = []
    # args are used transiently to parse processes; never written to the journal.
    processes = claude_discovery.parse_processes(claude_discovery.command(
        ['ps', '-axo', 'pid,ppid,uid,tty,lstart,args'], warnings))
    try:
        result = subprocess.run(['ps', '-ww', '-axo', 'pid=,comm='], capture_output=True,
                                text=True, timeout=10, env={**os.environ, 'LC_ALL': 'C'})
        executables = {int(parts[0]): parts[1] for line in result.stdout.splitlines()
                       if len(parts := line.strip().split(maxsplit=1)) == 2 and parts[0].isdigit()}
    except (OSError, subprocess.TimeoutExpired):
        executables = {}
    ancestors, seen = [], set()
    pid = os.getppid()
    while pid in processes and pid not in seen and len(ancestors) < 32:
        seen.add(pid)
        process = processes[pid]
        ancestors.append({'pid': pid, 'ppid': process['ppid'], 'uid': process['uid'],
                          'started_at_epoch': process.get('startedAtEpoch'), 'executable': executables.get(pid)})
        pid = process['ppid']
    return {'pid': os.getpid(), 'ppid': os.getppid(), 'uid': os.getuid(), 'cwd': str(Path.cwd()),
            'hostname': socket.gethostname(), 'executable': str(Path(sys.executable).resolve()),
            'started_at_epoch': processes.get(os.getpid(), {}).get('startedAtEpoch'),
            'observed_at': now(), 'ancestors': ancestors, 'identity_source': 'unattributed',
            'warnings': warnings}


def identify_sender(observed, agents):
    # Claude's ancestor PID is matched to a live start-time-verified registry.
    ancestors = {p['pid']: index for index, p in enumerate(observed['ancestors'])}
    matches = sorted((a for a in agents if a['harness'] == 'claude' and a.get('pid') in ancestors),
                     key=lambda a: ancestors[a['pid']])
    if matches:
        agent = matches[0]
        observed['identity_source'] = 'process-tree'
    else:
        claimed = os.environ.get('CODEX_THREAD_ID')
        matches = [a for a in agents if a['harness'] == 'codex' and a['thread_id'] == claimed]
        if not matches:
            return identity('unknown', name='local-process', cwd=observed['cwd'], pid=observed['pid'])
        agent = matches[0]
        observed['identity_source'] = 'environment-claim'
        observed['identity_note'] = 'CODEX_THREAD_ID matches a live daemon thread; environment is caller-controlled, not authentication.'
    return identity(agent['harness'], name=agent.get('name'), thread_id=agent['thread_id'],
                    cwd=agent.get('cwd'), pid=agent.get('pid'))
