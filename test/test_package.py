"""Offline package invariants. No discovery or native sends in this suite."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent_msg import cli, protocol, service, transports
from agent_msg import __version__
from agent_msg.journal import Journal, default_path


def envelope(msg='Hello', sender=None, recipient=None, parent=None, notify=None):
    mid = str(uuid.uuid4())
    sender = sender or protocol.identity('codex', name='one', thread_id='codex-id', cwd='/repo')
    recipient = recipient or protocol.identity('claude', name='two', thread_id='claude-id', cwd='/repo', pid=123)
    return protocol.make(sender, recipient, msg, {'kind': 'agent-msg', 'request_id': mid, 'notify': notify},
                         message_id=mid, in_reply_to=parent)


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'journal.db'
        self.j = Journal(self.path)
        self.addCleanup(self.j.close)

    def test_append_only_including_replace(self):
        e = envelope(); self.j.append(e, {'pid': 1}); self.j.event(e['id'], 'sent', 'test')
        for sql in ['UPDATE messages SET envelope=envelope', 'DELETE FROM messages',
                    'INSERT OR REPLACE INTO messages SELECT * FROM messages',
                    'UPDATE deliveries SET status=status', 'DELETE FROM deliveries',
                    'INSERT OR REPLACE INTO deliveries SELECT * FROM deliveries']:
            with self.assertRaises(sqlite3.IntegrityError, msg=sql):
                with self.j.connection:
                    self.j.connection.execute(sql)
        self.assertEqual(self.j.get_message(e['id'])['msg'], 'Hello')

    def test_page_batch_query_count_and_unicode_search(self):
        for i in range(24):
            e = envelope(f'ÜBER {i}'); self.j.append(e, {}); self.j.event(e['id'], 'sent', 'test')
        queries = []; self.j.connection.set_trace_callback(queries.append)
        page = self.j.list_messages(limit=10, q='über', status='sent')
        self.assertEqual(len(page), 10)
        self.assertEqual(len(queries), 2)
        next_page = self.j.list_messages(limit=10, before_seq=page[-1]['seq'])
        self.assertFalse({r['id'] for r in page} & {r['id'] for r in next_page})
        self.assertEqual(self.j.stats()['by_status'], {'sent': 24})

    def test_peer_key_roundtrip(self):
        for tid in (None, '', 't1'):
            for name in (None, '', 'named'):
                e = envelope(sender=protocol.identity('claude', name=name, thread_id=tid))
                self.j.append(e, {})
        for peer in self.j.list_peers():
            self.assertTrue(self.j.list_messages(peer=peer['key']), peer)

    def test_reply_conversation_and_atomic_event(self):
        a = envelope(); b = envelope(parent=a['id']); c = envelope(parent=b['id'])
        for e in (a,b,c):
            self.j.append(e, {}, initial_event={'status':'received','transport':'journal'})
        self.assertEqual([r['id'] for r in self.j.get_conversation(c['id'])], [a['id'],b['id'],c['id']])
        self.assertEqual(self.j.replies(a['id'])[0]['id'], b['id'])
        failed = envelope()
        with self.assertRaises(ValueError):
            self.j.append(failed, {}, initial_event={'status':'invalid','transport':'test'})
        self.assertIsNone(self.j.get_message(failed['id']))

    def test_readonly_checkpoint_copy_and_permissions(self):
        e = envelope(); self.j.append(e,{})
        self.j.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        destination = self.path.with_name('snapshot.db'); shutil.copyfile(self.path,destination)
        with Journal(destination,readonly=True) as reader:
            self.assertEqual(reader.get_message(e['id'])['msg'],'Hello')
            with self.assertRaises(sqlite3.OperationalError):
                reader.append(envelope(),{})
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)
        with self.assertRaises(FileNotFoundError):
            Journal(self.path.with_name('missing.db'),readonly=True)

    def test_concurrent_writers_counters_consistent(self):
        def write(worker):
            with Journal(self.path) as other:
                for i in range(10):
                    e=envelope(f'{worker}-{i}');other.append(e,{})
                    other.event(e['id'],'sent','test')
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(write,range(4)))
        self.assertEqual(self.j.stats()['messages'],40)
        self.assertEqual(self.j.stats()['by_status'],{'sent':40})

    def test_relative_xdg_ignored(self):
        with patch.dict(os.environ,{'XDG_STATE_HOME':'relative'}):
            self.assertEqual(default_path(),Path.home()/'.local/state/agent-msg/journal.db')


class ProtocolTests(unittest.TestCase):
    def test_invalid_timestamps_and_uuids(self):
        for change in ({'id':str(uuid.uuid4()).upper()}, {'created_at':'bad'},
                       {'created_at':'2026-09-30T12:00:00.000+02:00'}, {'created_at':'2026-09-30T12:00:00'},
                       {'in_reply_to':'{'+str(uuid.uuid4())+'}'}):
            e=envelope();e.update(change)
            with self.assertRaises(ValueError):protocol.validate(e)

    def test_route_has_no_executable_path_or_socket(self):
        for key,value in [('command',['sh','-c','bad']),('db','/tmp/arbitrary.db'),('socket','/tmp/x.sock')]:
            e=envelope();e['reply_route'][key]=value
            with self.assertRaises(ValueError):protocol.validate(e)
        wire=protocol.wire_text(envelope())
        self.assertNotIn('reply_route.command',wire)
        self.assertNotIn('/tmp/',wire)


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.j=Journal(Path(self.temp.name)/'journal.db');self.addCleanup(self.j.close)
        self.e=envelope();self.observed={'pid':os.getpid(),'started_at_epoch':0}

    def test_failed_vs_uncertain_and_no_retry(self):
        for error,status in [(transports.NotDispatched('gone'),'failed'),(ConnectionError('write failed'),'uncertain')]:
            e=envelope()
            with patch.object(transports,'send',side_effect=error) as send:
                with self.assertRaises(type(error)):service.dispatch(self.j,e,self.observed)
                send.assert_called_once()
            self.assertEqual(self.j.get_message(e['id'])['status'],status)

    def test_journal_only_reply_no_transport_and_correlated_wait(self):
        self.j.append(self.e,self.observed)
        wrong=envelope(sender=protocol.identity('unknown'),parent=self.e['id']);self.j.append(wrong,{})
        self.assertIsNone(service.wait_reply(self.j,self.e['id'],.01))
        with patch.object(service,'current_sender',return_value=(self.e['to'],self.observed)),patch.object(transports,'send') as send:
            reply=service.reply_message(self.j,self.e['id'],'ACK')
            send.assert_not_called()
        self.assertEqual(reply['status'],'received')
        self.assertEqual(service.wait_reply(self.j,self.e['id'],.01)['msg'],'ACK')

    def test_sender_cannot_reply_as_recipient(self):
        self.j.append(self.e,self.observed)
        with patch.object(service,'current_sender',return_value=(self.e['from'],self.observed)):
            with self.assertRaisesRegex(ValueError,'another agent'):
                service.reply_message(self.j,self.e['id'],'wrong')

    def test_self_send_rejected(self):
        sender=self.e['from'];target={**sender,'reachable':True}
        with patch.object(transports,'resolve_target',return_value=target),patch.object(service,'current_sender',return_value=(sender,self.observed)):
            with self.assertRaisesRegex(ValueError,'itself'):
                service.send_message(self.j,'one','oops')
        self.assertEqual(self.j.stats()['messages'],0)

    def test_notify_reply_requires_cwd_and_allows_recipient_subdir(self):
        e=envelope();e['reply_route']['notify']=e['from'];self.j.append(e,{})
        with patch.object(service,'current_sender',return_value=(e['to'],self.observed)),patch.object(transports,'send',return_value={'transport':'test'}),patch.object(Path,'cwd',return_value=Path('/repo/subdir')):
            self.assertEqual(service.reply_message(self.j,e['id'],'OK')['status'],'sent')
        e=envelope(sender=protocol.identity('codex',thread_id='codex-id'));e['reply_route']['notify']=e['from'];self.j.append(e,{})
        with patch.object(service,'current_sender',return_value=(e['to'],self.observed)):
            with self.assertRaisesRegex(ValueError,'working directories'):
                service.reply_message(self.j,e['id'],'no')

    def test_cli_stdin_and_empty_message(self):
        with patch.object(sys,'stdin',io.StringIO(' ')),contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(['--db',str(self.j.path),'send','nobody','--json']),1)
        self.assertFalse(json.loads(out.getvalue())['ok'])

    def test_plugin_export_and_skill_install_no_overwrite(self):
        dest=Path(self.temp.name)/'plugin';cli.export_plugin(dest)
        for harness in ('claude','codex'):
            self.assertEqual(json.loads((dest/f'.{harness}-plugin/plugin.json').read_text())['name'],'agent-msg')
        self.assertEqual((dest/'LICENSE').read_bytes(), (Path(cli.__file__).parent/'LICENSE').read_bytes())
        self.assertEqual((dest/'README.md').read_bytes(), (Path(cli.__file__).parent/'plugin_assets/README.md').read_bytes())
        interface=json.loads((dest/'.codex-plugin/plugin.json').read_text())['interface']
        for field in ('logo','composerIcon'):
            self.assertTrue((dest/interface[field]).is_file())
        self.assertLessEqual(len(interface['shortDescription']),30)
        for source in Path(cli.__file__).parent.rglob('*.py'):
            self.assertEqual(source.read_bytes(), (dest/'lib/agent_msg'/source.relative_to(Path(cli.__file__).parent)).read_bytes())
        run=subprocess.run([sys.executable,str(dest/'skills/agent-msg/scripts/agent-msg'),'--version'],
            cwd=self.temp.name,env={**os.environ,'PYTHONPATH':''},capture_output=True,text=True,check=True)
        self.assertEqual(run.stdout.strip(),__version__)
        install=Path(self.temp.name)/'install';result=cli.install_skill('both',install)
        self.assertEqual(len(result['installed']),2)
        (Path(result['installed'][0])/'SKILL.md').write_text('customized')
        with self.assertRaisesRegex(ValueError,'different contents'):cli.install_skill('both',install)


if __name__=='__main__':unittest.main()
