"""Offline SDK/Unix socket integration; no models or existing peer messages."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from agent_msg import cli, identity, protocol, service, transports
from agent_msg._native import opencode
from agent_msg.journal import Journal


class OpenCodeIdentityTests(unittest.TestCase):
    def test_attribution_requires_session_instance_and_ancestor(self):
        agent = dict(harness='opencode', thread_id='ses_one', instance='42-abcdef', pid=42, cwd='/repo')
        observed = dict(ancestors=[{'pid':42}], cwd='/repo', pid=100)
        with patch.dict(os.environ, {'AGENT_MSG_OPENCODE_SESSION_ID':'ses_one',
                                    'AGENT_MSG_OPENCODE_INSTANCE':'42-abcdef', 'CODEX_THREAD_ID':''}):
            self.assertEqual(identity.identify_sender(observed, [agent])['harness'], 'opencode')
            for update in ({'instance':'wrong'}, {'thread_id':'ses_two'}, {'pid':43}):
                self.assertEqual(identity.identify_sender(observed, [{**agent, **update}])['harness'], 'unknown')

    def test_installer_is_idempotent_and_preserves_customization(self):
        with tempfile.TemporaryDirectory() as d:
            result = cli.install_opencode(d, symlink=True)
            self.assertTrue(all(Path(p).is_symlink() for p in result['installed']))
            self.assertEqual(cli.install_opencode(d, symlink=True), result)
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'plugins/agent-msg.js'; p.parent.mkdir(); p.write_text('custom')
            with self.assertRaisesRegex(ValueError, 'different contents'): cli.install_opencode(d)
            self.assertFalse((Path(d)/'skills').exists())


@unittest.skipUnless(shutil.which('node') and os.name == 'posix', 'Node and Unix sockets required')
class OpenCodeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='oc-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.env = patch.dict(os.environ, {'XDG_STATE_HOME':str(self.root/'state'),
            'AGENT_MSG_OPENCODE_RUNTIME_DIR':str(self.root/'r'),
            'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src'), 'TEST_PYTHON':sys.executable})
        self.env.start(); self.addCleanup(self.env.stop)
        self.child = subprocess.Popen(['node', str(Path(__file__).with_name('opencode_bridge_fixture.mjs'))],
            cwd=self.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.close_child)
        line=self.child.stdout.readline()
        self.assertTrue(line, 'bridge fixture did not start')
        self.ready=json.loads(line)

    def close_child(self):
        if self.child.poll() is None:
            self.child.stdin.write('{"op":"close"}\n'); self.child.stdin.flush()
        _, errors=self.child.communicate(timeout=15)
        self.assertEqual(self.child.returncode,0,errors)

    def control(self, **op):
        self.child.stdin.write(json.dumps(op)+'\n'); self.child.stdin.flush()
        return json.loads(self.child.stdout.readline())

    def record(self):
        paths=list(opencode.registry_dir().glob('*.json'))
        warnings=[]
        processes=opencode.claude_discovery.parse_processes(opencode.claude_discovery.command(
            ['ps','-axo','pid,ppid,uid,tty,lstart,args'],warnings))
        return opencode.read_registry(paths[0],processes)

    def test_live_discovery_ignores_history_and_filters_workdir(self):
        report=opencode.discover()
        self.assertEqual(report['warnings'],[])
        self.assertEqual({a['thread_id'] for a in report['agents']},{'ses_one','ses_two'})
        self.assertEqual(next(a['status'] for a in report['agents'] if a['thread_id']=='ses_two'),'busy')
        self.assertEqual(transports.discover_agents('opencode','/unrelated')['agents'],[])
        self.assertEqual(self.ready['env']['EXISTING'],'kept')
        self.assertTrue(self.ready['command'].endswith("\nprintf 'hello'"))

    def test_send_idle_busy_preserves_model_agent_and_permissions(self):
        for sid in ('ses_one','ses_two'):
            a=transports.resolve_target('opencode:'+sid,str(self.root))
            receipt=transports.send(a,'peer message',str(uuid.uuid4()))
            self.assertEqual(receipt['transport'],'opencode-native')
        calls=self.control(op='calls')['calls']
        self.assertEqual(len(calls),2)
        for c in calls:
            self.assertEqual(c['body'],dict(parts=[dict(type='text',text='peer message')],
                agent='plan',model=dict(providerID='local',modelID='test'),variant='low'))
            self.assertNotIn('tools',c['body']); self.assertNotIn('system',c['body'])

    def test_duplicate_id_is_not_dispatched_twice(self):
        r=self.record(); mid=str(uuid.uuid4())
        fields=dict(session_id='ses_one',cwd=str(self.root),text='hi',message_id=mid)
        opencode.request(r,'send',**fields); opencode.request(r,'send',**fields)
        self.assertEqual(len(self.control(op='calls')['calls']),1)
        with self.assertRaises(opencode.Rejected): opencode.request(r,'send',**{**fields,'text':'different'})

    def test_unknown_destination_and_cross_directory_are_rejected(self):
        r=self.record()
        for sid,cwd in [('ses_history',str(self.root)),('ses_one','/wrong')]:
            with self.assertRaises(opencode.Rejected):
                opencode.request(r,'send',session_id=sid,cwd=cwd,text='hi',message_id=str(uuid.uuid4()))
        self.assertEqual(self.control(op='calls')['calls'],[])

    def test_registry_stale_pid_and_unsafe_permissions_are_ignored(self):
        p=next(opencode.registry_dir().glob('*.json')); original=p.read_text()
        changed=json.loads(original); changed['process_start']='Mon Jan  1 00:00:00 2001'
        p.write_text(json.dumps(changed)); self.assertEqual(opencode.discover()['agents'],[])
        p.write_text(original); p.chmod(0o644); self.assertEqual(opencode.discover()['agents'],[])
        p.chmod(0o600)

    def test_uncertain_dispatch_is_journaled_and_never_retried(self):
        self.control(op='fail',value=True)
        target=transports.resolve_target('opencode:ses_one',str(self.root))
        mid=str(uuid.uuid4()); sender=protocol.identity('human')
        e=protocol.make(sender,transports.agent_identity(target),'hi',
            {'kind':'agent-msg','request_id':mid,'notify':None},message_id=mid)
        with Journal(self.root/'test.db') as j:
            with self.assertRaises(RuntimeError): service.dispatch(j,e,{'pid':os.getpid()})
            self.assertEqual(j.get_message(mid)['status'],'uncertain')
        self.assertEqual(len(self.control(op='calls')['calls']),1)

    def test_real_cli_sender_attribution_correlated_reply_and_notification(self):
        db=str(self.root/'roundtrip.db')
        sent=self.control(op='cli',args=['--db',db,'send','opencode:ses_two','hello','--json'])['cli']['message']
        self.assertEqual(sent['from']['harness'],'opencode')
        self.assertEqual(sent['from']['thread_id'],'ses_one')
        replied=self.control(op='cli',session='ses_two',args=['--db',db,'reply',sent['id'],'ACK','--json'])['cli']['message']
        self.assertEqual(replied['from']['thread_id'],'ses_two')
        self.assertEqual(replied['status'],'sent')
        with Journal(db) as j:
            self.assertEqual(service.wait_reply(j,sent['id'],.01)['msg'],'ACK')
        self.assertEqual(len(self.control(op='calls')['calls']),2)


if __name__=='__main__': unittest.main()
