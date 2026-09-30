"""Offline tests; real Claude sessions are never messaged by this module."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent_msg._native import claude_push as push
SCRIPT = Path(push.__file__)


class PushClaudeTests(unittest.TestCase):
    def thread(self, **extra):
        return {'sessionId': 'cfa570f3-97d3-43cb-9614-48454bea9e31', 'name': 'peer',
                'pid': 123, 'cwd': '/repo', 'registryFile': '/home/sessions/123.json',
                'peerProtocol': 1, 'messaging': {'usableByCurrentUser': True}, **extra}

    def test_exact_name_id_cwd_and_ambiguity(self):
        thread = self.thread()
        with patch.object(push.discover, 'discover', return_value={'threads': [thread]}):
            self.assertEqual(push.resolve_target('peer', cwd='/repo'), thread)
            self.assertEqual(push.resolve_target(thread['sessionId'].upper()), thread)
            for name, cwd in [('Peer', None), ('missing', None), ('peer', '/repo/child')]:
                with self.assertRaises(push.PushError):
                    push.resolve_target(name, cwd=cwd)
        with patch.object(push.discover, 'discover', return_value={'threads': [thread, thread]}):
            with self.assertRaisesRegex(push.PushError, 'ambiguous'):
                push.resolve_target('peer')

    def test_unverified_socket_or_protocol_rejected(self):
        for thread in [self.thread(messaging={}), self.thread(peerProtocol=2)]:
            with patch.object(push.discover, 'discover', return_value={'threads': [thread]}):
                with self.assertRaises(push.PushError):
                    push.resolve_target('peer')

    def test_frame_escaping_and_optional_reply_address(self):
        record = {'sessionId': 'session'}
        frame = push.make_frame(record, 'Hello <tag> & bye', 'msg')
        self.assertNotIn('from', frame)
        self.assertIn('Hello &lt;tag&gt; &amp; bye', frame['message']['content'])
        self.assertNotIn('SendMessage', frame['message']['content'])
        frame = push.make_frame(record, 'hello', 'msg', 'uds:/tmp/test.sock')
        self.assertEqual(frame['from'], 'uds:/tmp/test.sock')
        self.assertEqual(frame['priority'], 'next')
        self.assertIn('SendMessage', frame['message']['content'])

    def test_real_socket_send_auth_then_message(self):
        with tempfile.TemporaryDirectory(prefix='pc-', dir='/tmp') as directory:
            destination = directory + '/peer.sock'
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(destination)
                listener.listen()
                push.deliver({'sessionId': 'sid', 'messagingSocketPath': destination},
                             'test-secret', 'Hello', 'message-id')
                connection, _ = listener.accept()
                with connection:
                    raw = b''
                    while chunk := connection.recv(4096):
                        raw += chunk
            auth, frame = map(json.loads, raw.splitlines())
            self.assertEqual(auth, {'type': 'auth', 'token': 'test-secret'})
            self.assertEqual(frame['msg_id'], 'message-id')
            self.assertEqual(frame['session_id'], 'sid')
            self.assertNotIn('test-secret', json.dumps(frame))

    def test_reply_filters_and_envelope(self):
        frame = {'type': 'user', 'from': 'uds:peer', 'message': {'role': 'user',
                 'content': '<cross-session-message from="uds:peer">\nACK &amp; done\n</cross-session-message>'}}
        self.assertEqual(push.reply_text(frame, 'uds:peer'), 'ACK & done')
        self.assertIsNone(push.reply_text(frame, 'uds:other'))
        for malformed in [[], {}, {'type': 'auth'}, {**frame, 'message': []}]:
            self.assertIsNone(push.reply_text(malformed, 'uds:peer'))

    def test_wait_handles_fragmented_frames_auth_and_wrong_sender(self):
        with tempfile.TemporaryDirectory(prefix='pc-', dir='/tmp') as directory:
            path = directory + '/reply.sock'
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(path)
                listener.listen()
                def send():
                    with socket.socket(socket.AF_UNIX) as client:
                        client.connect(path)
                        client.sendall(b'not json\n{"type":"auth","token":"ignored"}\n')
                        wrong = {'type': 'user', 'from': 'uds:other',
                                 'message': {'role': 'user', 'content': 'WRONG'}}
                        client.sendall((json.dumps(wrong) + '\n').encode())
                        right = {**wrong, 'from': 'uds:peer', 'msg_id': 'reply-id',
                                 'message': {'role': 'user', 'content': 'ACK'}}
                        wire = (json.dumps(right) + '\n').encode()
                        client.sendall(wire[:20])
                        time.sleep(0.01)
                        client.sendall(wire[20:])
                worker = threading.Thread(target=send)
                worker.start()
                try:
                    result = push.wait_for_reply(listener, 'uds:peer', 2)
                finally:
                    worker.join(3)
                self.assertEqual(result, {'replyStatus': 'received', 'reply': 'ACK', 'replyMessageId': 'reply-id'})

    def test_wait_timeout(self):
        with tempfile.TemporaryDirectory(prefix='pc-', dir='/tmp') as directory:
            with socket.socket(socket.AF_UNIX) as listener:
                listener.bind(directory + '/reply.sock')
                listener.listen()
                result = push.wait_for_reply(listener, 'uds:peer', 0.02)
                self.assertEqual(result, {'replyStatus': 'timeout', 'reply': None})

    def test_credentials_check_identity_and_reject_symlink(self):
        with tempfile.TemporaryDirectory(prefix='pc-', dir='/tmp') as directory:
            home = Path(directory)
            (home / 'sessions').mkdir()
            destination = str(home / 'peer.sock')
            registry = home / 'sessions/123.json'
            registry.touch()
            record = {'pid': 123, 'sessionId': 'sid', 'cwd': '/repo',
                      'messagingSocketPath': destination, 'procStart': 'start'}
            process = {'uid': os.getuid(), 'startedAtEpoch': 10}
            thread = self.thread(sessionId='sid', claudeHome=str(home), registryFile=str(registry),
                                 messaging={'path': destination}, process=process)
            digest = hashlib.sha256(os.path.abspath(destination).encode()).hexdigest()
            key = home / f'sessions/123.{digest}.key'
            key.write_text(json.dumps({'procStart': 'start', 'peerToken': 'secret'}))
            with socket.socket(socket.AF_UNIX) as server, contextlib.ExitStack() as stack:
                server.bind(destination)
                stack.enter_context(patch.object(push.discover, 'read_record', return_value=record))
                stack.enter_context(patch.object(push.discover, 'command', return_value=''))
                stack.enter_context(patch.object(push.discover, 'parse_processes', return_value={123: process}))
                identity = stack.enter_context(patch.object(push.discover, 'validate_identity', return_value=(True, 'verified')))
                self.assertEqual(push.peer_credentials(thread), (record, 'secret'))
                identity.return_value = (False, 'process-start-mismatch')
                with self.assertRaisesRegex(push.PushError, 'process-start-mismatch'):
                    push.peer_credentials(thread)
                identity.return_value = (True, 'verified')
                with self.assertRaisesRegex(push.PushError, 'identity or destination changed'):
                    push.peer_credentials({**thread, 'sessionId': 'replaced'})
                key.write_text(json.dumps({'procStart': 'old', 'peerToken': 'secret'}))
                with self.assertRaisesRegex(push.PushError, 'credentials do not match'):
                    push.peer_credentials(thread)
                key.unlink()
                key.symlink_to(registry)
                with self.assertRaises(OSError):
                    push.peer_credentials(thread)

    def test_listener_cleanup_and_existing_socket_preserved(self):
        with tempfile.TemporaryDirectory(prefix='pc-', dir='/tmp') as directory:
            fake_path = Path(directory)
            with patch.object(push, 'Path', return_value=fake_path):
                with self.assertRaisesRegex(RuntimeError, 'test failure'):
                    with push.reply_listener() as (_, address):
                        path = Path(address.removeprefix('uds:'))
                        self.assertTrue(path.exists())
                        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                        raise RuntimeError('test failure')
                self.assertFalse(path.exists())
                path.write_text('do not overwrite')
                with self.assertRaises(OSError):
                    with push.reply_listener():
                        self.fail('existing path must not be replaced')
                self.assertEqual(path.read_text(), 'do not overwrite')

    def test_empty_stdin_json_error_does_not_discover(self):
        with patch.object(sys, 'argv', [str(SCRIPT), 'peer', '--json']), \
                patch.object(sys, 'stdin', io.StringIO(' \n')), \
                patch.object(push, 'resolve_target') as resolve, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(push.main(), 1)
            self.assertEqual(json.loads(output.getvalue()), {'ok': False, 'error': 'empty message'})
            resolve.assert_not_called()

    def test_invalid_timeouts(self):
        for value in ['0', '-1', 'nan', 'inf', 'bad']:
            with self.assertRaises(push.argparse.ArgumentTypeError):
                push.positive_seconds(value)


if __name__ == '__main__':
    unittest.main()
