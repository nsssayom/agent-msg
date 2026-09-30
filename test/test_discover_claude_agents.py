"""Fixture tests: no Claude process is launched and no message is sent."""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent_msg._native import claude_discovery as discovery


class DiscoveryTests(unittest.TestCase):
    def test_process_snapshot_parses_spacing_and_does_not_publish_args(self):
        result = discovery.parse_processes(' 123 1 501 ttys007 Wed Sep  9 14:00:00 2026 claude --secret value')
        self.assertEqual(result[123]['tty'], 'ttys007')
        self.assertNotIn('args', result[123])

    def test_process_roles(self):
        for args, expected in [
            ('claude', 'session'), ('claude daemon run', 'daemon'),
            ('/a/claude --bg-pty-host /tmp/a.sock', 'bg-pty-host'),
            ('claude bg-spare --bg-spare /tmp/a.sock', 'bg-spare'),
            ('/a/claude/versions/2.1.286 --session-id abc', 'session'),
            ('node /a/node_modules/@anthropic-ai/claude-code/cli.js -p hello', 'session'),
            ('tail -F /tmp/claude-log', None), ('/Applications/Claude.app/helper', None),
        ]:
            with self.subTest(args=args):
                self.assertEqual(discovery.classify_process({'_args': args}), expected)

    def test_pid_reuse_is_rejected(self):
        domain = 'darwin' if sys.platform == 'darwin' else 'linux'
        record = {'procStart': 'Wed Sep 30 20:06:37 2026', 'pidDomain': domain}
        epoch = discovery.parse_start(record['procStart'], utc=True)
        self.assertEqual(discovery.validate_identity(record, {'startedAtEpoch': epoch + 30}),
                         (False, 'process-start-mismatch'))
        self.assertEqual(discovery.validate_identity(record, {'startedAtEpoch': epoch}),
                         (True, 'verified-start-time'))

    def test_foreign_domain_and_dead_pid_are_rejected(self):
        self.assertEqual(discovery.validate_identity({}, None), (False, 'process-not-running'))
        self.assertEqual(discovery.validate_identity({'pidDomain': 'windows'}, {}),
                         (False, 'foreign-pid-domain'))

    def test_registry_without_start_time_is_not_trusted(self):
        self.assertEqual(discovery.validate_identity({}, {'_args': 'claude', 'startedAtEpoch': 1}),
                         (False, 'missing-process-start-time'))

    def test_refresh_uses_renamed_session_and_latest_status(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            path = home / '123.json'
            record = {'pid': 123, 'sessionId': 'abc', 'procStart': 'Wed Sep 30 20:06:37 2026',
                      'name': 'old-name', 'status': 'busy'}
            previous = dict(record)
            record.update(name='new-name', status='idle')
            path.write_text(json.dumps(record))
            process = {'uid': os.getuid(), 'startedAtEpoch': discovery.parse_start(record['procStart'], utc=True)}
            discarded, warnings = [], []
            refreshed = discovery.refresh_candidates([(home, path, previous, 'verified-start-time')],
                                                      {123: process}, discarded, warnings)
            self.assertEqual(refreshed[0][2]['name'], 'new-name')
            self.assertEqual(refreshed[0][2]['status'], 'idle')
            self.assertEqual(discarded, [])
            self.assertEqual(warnings, [])

    def test_refresh_drops_exited_and_reused_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            path = home / '123.json'
            record = {'pid': 123, 'sessionId': 'abc', 'procStart': 'Wed Sep 30 20:06:37 2026'}
            path.write_text(json.dumps(record))
            candidate = [(home, path, record, 'verified-start-time')]
            for processes in ({}, {123: {'uid': os.getuid(), 'startedAtEpoch': 0}}):
                discarded = []
                self.assertEqual(discovery.refresh_candidates(candidate, processes, discarded, []), [])
                self.assertEqual(len(discarded), 1)

    def test_historical_model_and_obsolete_waiting_reason_are_not_live_fields(self):
        thread = discovery.compatible_thread({'pid': 123, 'sessionId': 'abc', 'status': 'idle',
            'waitingFor': 'old prompt', 'spare': True, 'formerNames': ['old name'],
            'process': {'pid': 123}, 'transcript': {
                'lastObservedModel': 'old-model', 'modelObservedAt': '2026-09-01T00:00:00Z'}})
        for field in ('model', 'waitingFor', 'spare', 'formerNames'):
            self.assertNotIn(field, thread)
        self.assertEqual(thread['transcript']['lastObservedModel'], 'old-model')

    def test_record_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '123.json'
            for record in [{'pid': True, 'sessionId': 'abc'}, {'pid': 124, 'sessionId': 'abc'},
                           {'pid': 123}, []]:
                path.write_text(json.dumps(record))
                with self.assertRaises(ValueError):
                    discovery.read_record(path)
            path.write_text(json.dumps({'pid': 123, 'sessionId': 'abc'}))
            self.assertEqual(discovery.read_record(path)['pid'], 123)

    def test_symlink_record_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'target'
            target.write_text(json.dumps({'pid': 123, 'sessionId': 'abc'}))
            path = Path(directory) / '123.json'
            path.symlink_to(target)
            with self.assertRaises(OSError):
                discovery.read_record(path)

    def test_secret_fields_are_not_allowlisted(self):
        self.assertNotIn('peerToken', discovery.REGISTRY_FIELDS)
        self.assertNotIn('token', discovery.REGISTRY_FIELDS)
        self.assertNotIn('env', discovery.REGISTRY_FIELDS)

    def test_shared_thread_format_preserves_native_status(self):
        thread = discovery.compatible_thread({'pid': 123, 'sessionId': 'abc', 'status': 'shell',
            'kind': 'interactive', 'cwd': '/repo', 'startedAt': 1000000,
            'messaging': {'usableByCurrentUser': True}, 'process': {'pid': 123}})
        self.assertEqual(thread['threadId'], thread['sessionId'])
        self.assertEqual(thread['status'], 'active')
        self.assertEqual(thread['claudeStatus'], 'shell')
        self.assertNotIn('canAcceptDirectInput', thread)
        for unsupported in ('queued', 'activeTurnId', 'reasoningEffort', 'modelProvider',
                            'daemonAttached', 'isSubAgent', 'model'):
            self.assertNotIn(unsupported, thread)

    def test_directory_filter_has_path_boundaries(self):
        self.assertTrue(discovery.within_cwd('/repo/child', '/repo'))
        self.assertFalse(discovery.within_cwd('/repo-other', '/repo'))
        self.assertTrue(discovery.within_cwd('/repo', '/'))

    def test_default_table_has_codex_columns_and_no_terminal_controls(self):
        report = {'daemon': {'status': 'running'}, 'coverage': {'claudeHomes': ['/tmp']},
            'sessionCount': 1, 'processes': [], 'warnings': [], 'threads': [{
                'threadId': 'abc', 'name': '\x1b[31mname', 'kind': 'interactive',
                'status': 'idle', 'process': {'pid': 123, 'tty': 'ttys001'}}]}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            discovery.print_table(report)
        self.assertIn('THREAD ID', output.getvalue())
        self.assertIn('PID/TTY', output.getvalue())
        self.assertNotIn('\x1b', output.getvalue())

    def test_socket_credentials_are_never_read(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'sessions').mkdir()
            path = str(home / 'peer.sock')
            listener = socket.socket(socket.AF_UNIX)
            try:
                listener.bind(path)
                os.chmod(path, 0o600)
                digest = hashlib.sha256(path.encode()).hexdigest()
                credential = home / 'sessions' / f'123.{digest}.key'
                # Deliberately not JSON: credential discovery must use stat only.
                credential.write_text('SECRET_MUST_NOT_APPEAR')
                result = discovery.socket_metadata(path, home, 123, {'uid': os.getuid()},
                                                    {'openSocketPaths': [path]})
                self.assertTrue(result['usableByCurrentUser'])
                self.assertNotIn('SECRET_MUST_NOT_APPEAR', json.dumps(result))
                credential.unlink()
                result = discovery.socket_metadata(path, home, 123, {'uid': os.getuid()},
                                                    {'openSocketPaths': [path]})
                self.assertFalse(result['usableByCurrentUser'])
            finally:
                listener.close()


if __name__ == '__main__':
    unittest.main()
