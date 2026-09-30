"""Small, composable CLI. Structured results go to stdout; errors to stderr."""
import argparse
import json
import math
import os
from pathlib import Path
import shutil
import sys

from . import __version__, service, transports
from .journal import Journal, default_path


def seconds(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError('must be a finite positive number')
    return value


def clean(value):
    return '\n'.join(''.join(c for c in line if c.isprintable()) for line in str(value).splitlines())


def output(value, as_json):
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
    elif isinstance(value, list):
        for row in value:
            if 'msg' in row:
                print(clean(f"{row['id']}  {row['status']:<11}  {row['from'].get('name') or row['from']['harness']} → {row['to'].get('name') or row['to']['harness']}"))
                print('  ' + clean(row['msg']).replace('\n', ' ')[:160])
            else:
                print(clean(f"{row['harness']}:{row['thread_id']}  {row.get('name') or '-'}  {row.get('status', '')}  {row.get('cwd') or '-'}"))
    elif 'message' in value:
        row = value['message']
        print(clean(f"{row['id']}  {row['status']}  → {row['to'].get('name') or row['to']['harness']}"))
        if 'reply' in value:
            print(clean(value['reply']['msg']) if value['reply'] else
                  f"Timed out. Late replies remain in the journal without a native notification. Inspect: agent-msg show {row['id']} --conversation (using the same --db).")
    else:
        print(clean(json.dumps(value, ensure_ascii=False, indent=2)))


def install_skill(harness, destination=None):
    source = Path(__file__).parent / 'skill'
    if not (source / 'SKILL.md').is_file():
        raise ValueError('skill is missing from this installation')
    paths = []
    for name in ('claude', 'codex') if harness == 'both' else (harness,):
        if destination:
            base = Path(destination).expanduser() / name
        elif name == 'codex':
            base = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
        else:
            base = Path(os.environ.get('CLAUDE_CONFIG_DIR', str(Path.home() / '.claude')))
        target = base / 'skills/agent-msg'
        # Never replace an existing customized skill silently.
        if target.exists():
            if not (target / 'SKILL.md').is_file() or (target / 'SKILL.md').read_bytes() != (source / 'SKILL.md').read_bytes():
                raise ValueError(f'skill already exists with different contents: {target}')
        paths.append(target)
    for target in paths:
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target)
    return {'installed': [str(path) for path in paths]}


def export_plugin(destination):
    """Emit one portable bundle with both harness compatibility manifests."""
    target = Path(destination).expanduser().absolute()
    if target.exists():
        raise ValueError(f'plugin destination already exists: {target}')
    metadata = {'name': 'agent-msg', 'version': __version__,
                'author': {'name': 'nsssayom'}, 'repository': 'https://github.com/nsssayom/agent-msg',
                'description': 'Local, journaled messages between Claude Code and Codex agents.'}
    target.mkdir(parents=True)
    (target / 'plugin.json').write_text(json.dumps({
        '$schema': 'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json', **metadata}, indent=2) + '\n')
    for harness in ('claude', 'codex'):
        folder = target / f'.{harness}-plugin'
        folder.mkdir()
        manifest = dict(metadata)
        if harness == 'codex':
            manifest['skills'] = './skills/'
        (folder / 'plugin.json').write_text(json.dumps(manifest, indent=2) + '\n')
    shutil.copytree(Path(__file__).parent / 'skill', target / 'skills/agent-msg')
    shutil.copytree(Path(__file__).parent, target / 'lib/agent_msg',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    scripts = target / 'skills/agent-msg/scripts'
    scripts.mkdir(exist_ok=True)
    launcher = scripts / 'agent-msg'
    launcher.write_text('#!/usr/bin/env python3\n'
                        '"""Run the bundled package without pip or network setup."""\n'
                        'from pathlib import Path\nimport sys\n'
                        'if sys.version_info < (3, 10):\n'
                        '    raise SystemExit("agent-msg requires Python 3.10+; invoke this launcher with a supported interpreter.")\n'
                        'sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))\n'
                        'from agent_msg.cli import main\nraise SystemExit(main())\n')
    launcher.chmod(0o755)
    (target / 'scripts').mkdir()
    convenience = target / 'scripts/agent-msg'
    convenience.write_text('#!/usr/bin/env python3\nfrom pathlib import Path\nimport runpy\n'
        'runpy.run_path(str(Path(__file__).resolve().parents[1] / "skills/agent-msg/scripts/agent-msg"), run_name="__main__")\n')
    convenience.chmod(0o755)
    return {'plugin': str(target), 'harnesses': ['claude', 'codex'],
            'launcher': str(launcher), 'requires': 'Python 3.10+; no pip or runtime dependencies required.'}


def parser():
    cli = argparse.ArgumentParser(prog='agent-msg', description='Local, journaled messages between Claude Code and Codex.')
    cli.add_argument('--version', action='version', version=__version__)
    cli.add_argument('--db', default=str(default_path()), help='SQLite journal path (or use XDG_STATE_HOME)')
    cli.add_argument('--json', action='store_true', help='machine-readable output')
    commands = cli.add_subparsers(dest='command', required=True)
    agents = commands.add_parser('agents', help='discover live agents (current workdir by default)')
    agents.add_argument('--all', action='store_true', help='discover across working directories; does not send')
    agents.add_argument('--harness', choices=('claude', 'codex'))
    agents.add_argument('--cwd', default=str(Path.cwd()))
    for name in ('send', 'reply'):
        command = commands.add_parser(name, help='send to a live agent' if name == 'send' else 'reply to a journal message ID')
        command.add_argument('target', help='[harness:]exact-name or thread-id' if name == 'send' else 'message UUID')
        command.add_argument('message', nargs='?', default='-', help="message text; omit or use '-' for stdin")
        command.add_argument('--wait', action='store_true', help='wait for a correlated journal reply')
        command.add_argument('--timeout', type=seconds, default=60, help='reply wait seconds (default: 60)')
        if name == 'send':
            command.add_argument('--cwd', default=str(Path.cwd()), help='exact destination workdir')
    log = commands.add_parser('log', help='list messages, newest first')
    log.add_argument('--limit', type=int, default=50)
    log.add_argument('--before', type=int, help='sequence cursor for older records')
    log.add_argument('--peer', help='harness:thread-id')
    log.add_argument('--search')
    log.add_argument('--status', choices=('prepared', 'dispatching', 'sent', 'failed', 'uncertain', 'received'))
    show = commands.add_parser('show', help='inspect a message, provenance, and delivery events')
    show.add_argument('id')
    show.add_argument('--conversation', action='store_true')
    ui = commands.add_parser('ui', help='serve the read-only local inspection UI')
    ui.add_argument('--port', type=int, default=0, help='loopback port (default: random available port)')
    browser = ui.add_mutually_exclusive_group()
    browser.add_argument('--open', dest='open', action='store_true', default=True, help='open a browser (default)')
    browser.add_argument('--no-open', dest='open', action='store_false', help='serve without opening a browser')
    ui.add_argument('--hostname', choices=('auto', '127.0.0.1', 'localhost', 'agent-msg.local'), default='auto',
                    help='browser hostname; server always binds to 127.0.0.1')
    skill = commands.add_parser('skill', help='install the bundled skill explicitly')
    skill.add_argument('action', choices=('install',))
    skill.add_argument('--harness', choices=('claude', 'codex', 'both'), required=True)
    skill.add_argument('--dest', help='alternate root; writes ROOT/{claude,codex}/skills/agent-msg')
    plugin = commands.add_parser('plugin', help='export an installable plugin bundle for both harnesses')
    plugin.add_argument('action', choices=('export',))
    plugin.add_argument('destination', help='new directory for the plugin bundle')
    for subparser in commands.choices.values():
        subparser.add_argument('--json', action='store_true', default=argparse.SUPPRESS, help='machine-readable output')
    return cli


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == 'agents':
            report = transports.discover_agents(args.harness, None if args.all else args.cwd)
            report['db_path'] = str(Path(args.db).expanduser().absolute())
            output(report if args.json else report['agents'], args.json)
            if not args.json:
                for warning in report['warnings']:
                    print('warning: ' + clean(warning), file=sys.stderr)
            return 0
        if args.command == 'skill':
            output(install_skill(args.harness, args.dest), args.json)
            return 0
        if args.command == 'plugin':
            output(export_plugin(args.destination), args.json)
            return 0
        if args.command == 'ui':
            from .web import serve
            if not 0 <= args.port <= 65535:
                raise ValueError('port must be between 0 and 65535')
            serve(args.db, port=args.port, open_browser=args.open, hostname=args.hostname)
            return 0
        if args.command in ('send', 'reply'):
            text = (sys.stdin.read() if args.message == '-' else args.message).strip()
            if not text:
                raise ValueError('empty message')
            with Journal(args.db) as journal:
                if args.command == 'send':
                    message = service.send_message(journal, args.target, text, cwd=args.cwd, wait=args.wait)
                else:
                    message = service.reply_message(journal, args.target, text, wait=args.wait)
                result = {'ok': True, 'db_path': str(journal.path), 'message': message}
                if args.wait:
                    result['reply'] = service.wait_reply(journal, message['id'], args.timeout)
                    result['ok'] = result['reply'] is not None
                output(result, args.json)
                return 0 if result['ok'] else 3
        with Journal(args.db, readonly=True) as journal:
            if args.command == 'log':
                result = journal.list_messages(limit=args.limit, before_seq=args.before,
                    peer=args.peer, q=args.search, status=args.status)
            else:
                result = journal.get_conversation(args.id) if args.conversation else journal.get_message(args.id)
                if not result:
                    raise ValueError('message not found')
            output(result, args.json)
        return 0
    except KeyboardInterrupt:
        print('interrupted; any dispatched message remains in the journal', file=sys.stderr)
        return 130
    except Exception as error:
        # CLI boundary: native protocol errors, DB failures, and permission errors
        # should be readable without printing a traceback or credential contents.
        if args.json:
            print(json.dumps({'ok': False, 'error': str(error)}))
        else:
            print('error: ' + clean(error), file=sys.stderr)
        return 1
