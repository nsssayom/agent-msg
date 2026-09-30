"""Opt-in busy-peer test of the unified protocol, using the existing codex[1]."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from agent_msg import service, transports
from agent_msg.journal import Journal


@unittest.skipUnless(os.environ.get('RUN_AGENT_MSG_LIVE_TESTS') == '1', 'Live messaging requires opt-in')
class BusyLiveTests(unittest.TestCase):
    def test_busy_codex_with_standard_reply(self):
        peer = transports.resolve_target('codex:codex[1]-msg', str(ROOT))
        self.assertEqual(peer['status'], 'idle')
        nonce = uuid.uuid4().hex[:10]
        results = ROOT / 'test/results'
        results.mkdir(exist_ok=True)
        db = results / f'agent-msg-busy-{nonce}.db'
        executable = str(ROOT / '.venv/bin/agent-msg')
        prefix = shlex.join([executable, '--db', str(db)])
        marker = 'AGENT_MSG_BUSY_' + nonce
        with Journal(db) as journal:
            prompt = (f'Authorized busy-peer integration test. Run python3 -c "import time; time.sleep(20)" {marker}. '
                      f'After it completes, use {prefix} reply MESSAGE_ID BASE_{nonce} --json, substituting this envelope id. '
                      'If another test envelope arrives during the sleep, reply to it as well. No other edits or messaging.')
            initial = service.send_message(journal, 'codex:codex[1]-msg', prompt, cwd=str(ROOT), wait=True)
            deadline = time.monotonic() + 35
            sleepers = []
            while time.monotonic() < deadline:
                snapshot = subprocess.check_output(['ps', '-ww', '-axo', 'pid=,args='], text=True)
                sleepers = [line for line in snapshot.splitlines() if marker in line and 'time.sleep(20)' in line
                            and len(line.split()) > 1 and Path(line.split()[1]).name.lower().startswith('python')]
                if sleepers:
                    break
                time.sleep(.2)
            self.assertTrue(sleepers, 'Observe the actual sleep before sending')
            reply_marker = 'BUSY_REPLY_' + nonce
            prompt = (f'Busy-peer update: after finishing the sleep, use {prefix} reply MESSAGE_ID {reply_marker} --json '
                      'using THIS envelope id. Also finish the initial test reply. No other actions.')
            started = time.monotonic()
            run = subprocess.run([executable, '--db', str(db), 'send', 'codex:codex[1]-msg', prompt,
                                  '--wait', '--timeout', '60', '--json'], cwd=ROOT, capture_output=True, text=True, timeout=80)
            result = json.loads(run.stdout)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertEqual(result['reply']['msg'], reply_marker)
            self.assertEqual(result['message']['deliveries'][-1]['detail']['mode'], 'steer')
            self.assertIsNotNone(service.wait_reply(journal, initial['id'], 15))
            evidence = {'elapsed': round(time.monotonic() - started, 3), 'result': result,
                        'sleep_pids': [int(line.split()[0]) for line in sleepers]}
            (results / 'agent-msg-busy.json').write_text(json.dumps(evidence, indent=2) + '\n')
            print(json.dumps({'busy_roundtrip_seconds': evidence['elapsed'], 'mode': 'steer'}), flush=True)
