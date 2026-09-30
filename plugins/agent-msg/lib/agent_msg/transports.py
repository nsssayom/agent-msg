"""Native transports. Discovery is read-only; sends preserve harness settings."""
from contextlib import contextmanager
from pathlib import Path

from ._native import claude_discovery, claude_push, codex_discovery, codex_push
from .protocol import identity


class NotDispatched(Exception):
    """A failure observed before invoking a native send operation."""


@contextmanager
def codex_client():
    path = codex_discovery.resolve_socket(None, codex_discovery.daemon_version_info())
    if not path:
        raise RuntimeError('Codex daemon socket not found')
    client = codex_discovery.DaemonClient(path, timeout=10)
    try:
        client.initialize()
        yield client
    finally:
        client.close()


def agent_identity(agent):
    return identity(agent['harness'], name=agent.get('name'), thread_id=agent['thread_id'],
                    cwd=agent.get('cwd'), pid=agent.get('pid'))


def discover_agents(harness=None, cwd=None):
    agents, warnings = [], []
    if harness in (None, 'claude'):
        try:
            report = claude_discovery.discover()
            warnings.extend(report['warnings'])
            for thread in report['threads']:
                if not thread.get('sessionId'):
                    continue
                agents.append({'harness': 'claude', 'thread_id': thread['sessionId'],
                    'name': thread.get('name'), 'cwd': thread.get('cwd'), 'pid': thread['pid'],
                    'status': thread['status'], 'reachable': bool((thread.get('messaging') or {}).get('usableByCurrentUser')),
                    'identity_evidence': thread['identityVerification']})
        except (OSError, ValueError, RuntimeError) as error:
            warnings.append(f'Claude discovery: {error}')
    if harness in (None, 'codex'):
        try:
            with codex_client() as client:
                for tid in codex_discovery.paginate(client, 'thread/loaded/list', {}):
                    thread = codex_push.read_thread(client, tid)
                    agents.append({'harness': 'codex', 'thread_id': tid, 'name': thread.get('name'),
                        'cwd': thread.get('cwd'), 'pid': None,
                        'status': (thread.get('status') or {}).get('type', 'unknown'),
                        'reachable': thread.get('canAcceptDirectInput', True), 'identity_evidence': 'daemon-thread'})
        except (OSError, ValueError, RuntimeError, codex_discovery.RpcError) as error:
            warnings.append(f'Codex discovery: {error}')
    if cwd is not None:
        root = Path(cwd).expanduser().resolve()
        agents = [a for a in agents if a.get('cwd') and Path(a['cwd']).resolve() == root]
    return {'agents': agents, 'warnings': warnings}


def resolve_target(target, cwd):
    harness = None
    if ':' in target:
        harness, target = target.split(':', 1)
        if harness not in ('codex', 'claude'):
            raise ValueError('target prefix must be codex: or claude:')
    report = discover_agents(harness, cwd)
    matches = [a for a in report['agents'] if a.get('name') == target or a['thread_id'].lower() == target.lower()]
    if not matches:
        warning = '; '.join(report['warnings'])
        raise ValueError(f'no live agent matches {target!r} in {cwd}' + (f'; {warning}' if warning else ''))
    if len(matches) > 1:
        raise ValueError('ambiguous target; use harness:thread-id: ' + ', '.join(a['harness'] + ':' + a['thread_id'] for a in matches))
    if not matches[0]['reachable']:
        raise ValueError('target has no reachable native messaging endpoint')
    return matches[0]


def send(agent, text, message_id):
    if agent['harness'] == 'claude':
        try:
            thread = claude_push.resolve_target(agent['thread_id'], cwd=agent['cwd'])
            record, token = claude_push.peer_credentials(thread)
        except Exception as error:
            raise NotDispatched(str(error)) from error
        claude_push.deliver(record, token, text, message_id)
        return {'transport': 'claude-native', 'mode': 'peer', 'deliveryStatus': 'written', 'priority': 'next'}
    if agent['harness'] == 'codex':
        from contextlib import ExitStack
        with ExitStack() as stack:
            try:
                client = stack.enter_context(codex_client())
                thread = codex_push.resolve_target(client, agent['thread_id'])
                if not thread.get('cwd') or Path(thread['cwd']).resolve() != Path(agent['cwd']).resolve():
                    raise ValueError('target changed working directory before send')
            except Exception as error:
                raise NotDispatched(str(error)) from error
            delivery = codex_push.deliver(client, thread, text, message_id)
            return {'transport': 'codex-daemon', **delivery, 'deliveryStatus': 'accepted'}
    raise NotDispatched('destination is not a supported harness')
