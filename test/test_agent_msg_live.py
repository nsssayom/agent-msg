"""Opt-in installed-package tests. Messages stay in this repository.

RUN_AGENT_MSG_LIVE_TESTS=1 .venv/bin/python -m unittest discover -s test -p test_agent_msg_live.py -v
"""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import unittest
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from agent_msg.journal import Journal
from agent_msg.transports import resolve_target


@unittest.skipUnless(os.environ.get('RUN_AGENT_MSG_LIVE_TESTS')=='1',
                     'Set RUN_AGENT_MSG_LIVE_TESTS=1 to message claude[0] and codex[1]')
class PackageLiveTests(unittest.TestCase):
    def test_both_harnesses_and_cross_harness_roundtrip(self):
        targets=['claude:claude[0]-msg','codex:codex[1]-msg']
        for target in targets:
            self.assertEqual(resolve_target(target,str(ROOT))['status'],'idle','Peer must be idle before testing')
        result_dir=ROOT/'test/results';result_dir.mkdir(exist_ok=True)
        db=result_dir/f'agent-msg-live-{uuid.uuid4().hex[:8]}.db'
        executable=ROOT/'.venv/bin/agent-msg'
        prefix=shlex.join([str(executable),'--db',str(db)])
        records=[]
        try:
            for target in targets:
                marker='PACKAGE_ACK_'+uuid.uuid4().hex[:10]
                prompt=(f'Authorized integration test in this repo. The user designated you as a test subject. '
                    f'Use the installed local package: {prefix} reply MESSAGE_ID {shlex.quote(marker)} --json. '
                    'Replace MESSAGE_ID with the id from this envelope. Reply once through agent-msg; '
                    'do not use SendMessage or any other transport. No file edits beyond the tool journal, no spawning agents.')
                started=time.monotonic()
                run=subprocess.run([str(executable),'--db',str(db),'send',target,prompt,'--wait','--timeout','60','--json'],
                                   cwd=ROOT,capture_output=True,text=True,timeout=80)
                result=json.loads(run.stdout)
                records.append({'target':target,'seconds':round(time.monotonic()-started,3),'exitCode':run.returncode,'result':result})
                print(json.dumps({'target':target,'seconds':records[-1]['seconds'],'ok':result.get('ok')}),flush=True)
                self.assertEqual(run.returncode,0,run.stdout+run.stderr)
                self.assertEqual(result['reply']['msg'],marker)
                self.assertEqual(result['reply']['in_reply_to'],result['message']['id'])
                self.assertEqual(result['reply']['from']['harness'],target.split(':')[0])
                self.assertIn('pid',result['reply']['observed'])
                self.assertEqual(set(result['message']['reply_route']),{'kind','request_id','notify'})

            marker='BRIDGE_ACK_'+uuid.uuid4().hex[:10]
            inner=(f'Authorized Claude-to-Codex bridge test. Run {prefix} reply MESSAGE_ID {marker} --json, '
                   'substituting your envelope id. No other actions or file edits.')
            nested=prefix+' send '+shlex.quote('codex:codex[1]-msg')+' '+shlex.quote(inner)+' --wait --timeout 45 --json'
            prompt=(f'Authorized full bridge test. Use your shell to run this exact local package command: {nested} . '
                    f'After it returns the exact reply {marker}, run {prefix} reply ORIGINAL_MESSAGE_ID {marker} --json '
                    'using this outer envelope id. No other agent messaging, file edits, or spawning.')
            start=time.monotonic()
            run=subprocess.run([str(executable),'--db',str(db),'send',targets[0],prompt,'--wait','--timeout','90','--json'],
                               cwd=ROOT,capture_output=True,text=True,timeout=110)
            result=json.loads(run.stdout)
            records.append({'target':'claude-to-codex-bridge','seconds':round(time.monotonic()-start,3),'exitCode':run.returncode,'result':result})
            print(json.dumps({'target':'bridge','seconds':records[-1]['seconds'],'ok':result.get('ok')}),flush=True)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            self.assertEqual(result['reply']['msg'],marker)
            with Journal(db,readonly=True) as journal:
                messages=journal.list_messages(limit=100)
                forwarded=[m for m in messages if m['from']['harness']=='claude' and m['to']['harness']=='codex'
                           and marker in m['msg'] and m['in_reply_to'] is None]
                self.assertEqual(len(forwarded),1)
                self.assertEqual(forwarded[0]['observed']['identity_source'],'process-tree')
        finally:
            (result_dir/'agent-msg-live.json').write_text(json.dumps({'db':str(db),'steps':records},indent=2)+'\n')


if __name__=='__main__':unittest.main()
