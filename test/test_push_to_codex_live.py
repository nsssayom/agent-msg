"""Opt-in end-to-end tests against the existing codex[1]-msg session.

Run from the repository root:
  RUN_CODEX_LIVE_TESTS=1 python3 -m unittest discover -s test -p test_push_to_codex_live.py -v

Sends messages only to the named, already-loaded session in this repository.
Never creates agents. The busy test asks the peer to sleep for 25 seconds.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from agent_msg._native import codex_push as push
MODULE = 'agent_msg._native.codex_push'


@unittest.skipUnless(os.environ.get('RUN_CODEX_LIVE_TESTS') == '1',
                     'Set RUN_CODEX_LIVE_TESTS=1 to send messages to codex[1]-msg')
class CodexLiveTests(unittest.TestCase):
    def setUp(self):
        self.evidence = {'targetName': 'codex[1]-msg', 'cwd': str(ROOT), 'steps': []}
        path = push.discover.resolve_socket(None, push.discover.daemon_version_info())
        self.assertIsNotNone(path, 'Codex daemon must already be running')
        self.client = push.DaemonClient(path, timeout=10)
        self.client.initialize()
        target = push.resolve_target(self.client, 'codex[1]-msg')
        self.assertEqual(Path(target['cwd']).resolve(), ROOT)
        self.assertEqual(target['status']['type'], 'idle', 'Do not disturb unrelated work')
        self.tid = target['id']
        self.evidence['threadId'] = self.tid
        self.nonce = uuid.uuid4().hex[:12]

    def tearDown(self):
        self.client.close()
        output = ROOT / 'test/results/codex-live.json'
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(self.evidence, indent=2) + '\n')

    def cli(self, target, message, *, wait=False, stdin=False):
        args = [sys.executable, "-m", MODULE, target, '-' if stdin else message, '--json']
        if wait:
            args.append('--wait')
        started = time.monotonic()
        result = subprocess.run(args, input=message if stdin else None, capture_output=True,
                                text=True, cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, timeout=100)
        elapsed = time.monotonic() - started
        record = {'target': target, 'wait': wait, 'stdin': stdin,
                  'elapsedSeconds': round(elapsed, 3), 'returnCode': result.returncode}
        try:
            record['result'] = json.loads(result.stdout)
        except ValueError:
            record['stdout'] = result.stdout
        if result.stderr:
            record['stderr'] = result.stderr
        self.evidence['steps'].append(record)
        print(json.dumps(record), flush=True)
        self.assertEqual(result.returncode, 0, record)
        self.assertTrue(record['result']['ok'], record)
        return record['result']

    def turns(self):
        return self.client.call('thread/turns/list', {
            'threadId': self.tid, 'limit': 5, 'itemsView': 'full'})['data']

    def wait_until_tool_running(self, turn_id):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            thread = push.read_thread(self.client, self.tid)
            turns = self.turns()
            turn = next((turn for turn in turns if turn['id'] == turn_id), None)
            if turn:
                if turn['status'] in push.TERMINAL_TURN_STATES:
                    self.fail('Peer completed before the busy test could send its message')
                running = [item for item in turn.get('items', [])
                           if item.get('status') == 'inProgress'
                           and item.get('type') in ('commandExecution', 'dynamicToolCall', 'mcpToolCall')]
                # In this daemon, turn history may only gain a command item when
                # it completes. Independently observe our unique sleep process.
                snapshot = subprocess.run(['ps', '-ww', '-axo', 'pid=,ppid=,args='],
                    capture_output=True, text=True, check=True).stdout
                sleepers = []
                for line in snapshot.splitlines():
                    parts = line.split(maxsplit=2)
                    if len(parts) == 3 and Path(parts[2].split()[0]).name.lower().startswith('python'):
                        if f'CODEX_BUSY_{self.nonce}' in parts[2] and 'time.sleep(25)' in parts[2]:
                            sleepers.append(int(parts[0]))
                if thread['status']['type'] == 'active' and (running or sleepers):
                    record = {'observedStatus': thread['status'], 'turnId': turn_id,
                              'runningItemTypes': [item['type'] for item in running],
                              'sleepProcessIds': sleepers}
                    self.evidence['busyBeforeSend'] = record
                    print(json.dumps(record), flush=True)
                    return
            time.sleep(0.3)
        self.fail('Did not observe a running tool before sending the busy-peer message')

    def test_idle_wait_and_busy_steer_wait(self):
        idle_ack = f'IDLE_ACK_{self.nonce}'
        idle = self.cli('codex[1]-msg',
            f'Authorized communication test. Reply exactly {idle_ack}. Do not run tools or edit files.', wait=True)
        self.assertEqual(idle['mode'], 'start')
        self.assertEqual(idle['turnStatus'], 'completed')
        self.assertEqual(idle['reply'], idle_ack)

        base = f'BUSY_BASE_{self.nonce}'
        steer_ack = f'STEER_ACK_{self.nonce}'
        started = self.cli('codex[1]-msg',
            'Authorized busy-peer communication test. Use your shell tool to run '
            f"python3 -c 'import time; time.sleep(25)' CODEX_BUSY_{self.nonce} and wait until that command completes. "
            f'Then give one final response containing {base}. '
            'If a follow-up test marker arrives during the command, include that marker in the same final response. '
            'Do not edit files, spawn agents, or message anyone else.')
        self.assertEqual(started['mode'], 'start')
        self.assertNotIn('reply', started)
        self.wait_until_tool_running(started['turnId'])

        steered = self.cli(self.tid,
            f'Busy-peer test update: include {steer_ack} in your final response along with {base}. '
            'Finish the running sleep command first. No other actions are needed.', wait=True, stdin=True)
        self.assertEqual(steered['mode'], 'steer')
        self.assertEqual(steered['turnId'], started['turnId'])
        self.assertEqual(steered['turnStatus'], 'completed')
        self.assertIn(base, steered['reply'])
        self.assertIn(steer_ack, steered['reply'])
        turn = next(turn for turn in self.turns() if turn['id'] == steered['turnId'])
        matched = [item for item in turn['items'] if item.get('type') == 'userMessage'
                   and item.get('clientId') == steered['clientUserMessageId']]
        self.evidence['steerMessageMatchedByClientId'] = bool(matched)
        self.assertTrue(matched, 'Reply must belong to the input we actually sent')
        commands = [item for item in turn['items'] if item.get('type') == 'commandExecution'
                    and f'CODEX_BUSY_{self.nonce}' in item.get('command', '')]
        self.assertTrue(commands, 'The observed sleep must belong to this peer turn')
        self.assertEqual(commands[0]['exitCode'], 0)
        self.evidence['sleepCommandCompleted'] = True
        self.assertEqual(push.read_thread(self.client, self.tid)['status']['type'], 'idle')
        self.evidence['passed'] = True


if __name__ == '__main__':
    unittest.main()
