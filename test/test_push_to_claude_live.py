"""Opt-in native messaging tests against claude[0]-msg in this repository.

RUN_CLAUDE_LIVE_TESTS=1 python3 -m unittest discover -s test -p test_push_to_claude_live.py -v
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
from agent_msg._native import claude_push as push
MODULE = 'agent_msg._native.claude_push'


@unittest.skipUnless(os.environ.get('RUN_CLAUDE_LIVE_TESTS') == '1',
                     'Set RUN_CLAUDE_LIVE_TESTS=1 to message claude[0]-msg')
class ClaudeLiveTests(unittest.TestCase):
    def test_name_wait_and_id_stdin_without_wait(self):
        target = push.resolve_target('claude[0]-msg', cwd=str(ROOT))
        self.assertEqual(target['status'], 'idle', 'Do not disturb unrelated work')
        evidence = {'targetName': target['name'], 'threadId': target['sessionId'],
                    'cwd': target['cwd'], 'steps': [], 'passed': False}
        nonce = uuid.uuid4().hex[:12]
        try:
            ack = f'CLAUDE_WAIT_ACK_{nonce}'
            command = [sys.executable, "-m", MODULE, target['name'],
                       f'Authorized messaging test. Reply exactly {ack} using the native reply instructions below. '
                       'Do not edit files, spawn agents, or message anyone else.',
                       '--wait', '--timeout', '60', '--cwd', str(ROOT), '--json']
            start = time.monotonic()
            run = subprocess.run(command, capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, timeout=75)
            result = json.loads(run.stdout)
            evidence['steps'].append({'case': 'name-wait', 'elapsedSeconds': round(time.monotonic() - start, 3),
                                      'exitCode': run.returncode, 'result': result})
            print(json.dumps(evidence['steps'][-1]), flush=True)
            self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
            self.assertEqual(result['replyStatus'], 'received')
            self.assertEqual(result['reply'], ack)

            # Confirm non-wait delivery independently in the peer's new transcript
            # records; a successful socket write alone is not an application ack.
            transcript = Path(target['rolloutPath'])
            offset = transcript.stat().st_size
            ack = f'CLAUDE_NOWAIT_ACK_{nonce}'
            start = time.monotonic()
            run = subprocess.run([sys.executable, "-m", MODULE, target['sessionId'], '-', '--json', '--cwd', str(ROOT)],
                input=f'Authorized messaging test without a return address. Reply exactly {ack} in your own conversation. '
                      'Do not use tools, edit files, spawn agents, or send messages.',
                capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, timeout=20)
            result = json.loads(run.stdout)
            evidence['steps'].append({'case': 'id-stdin-no-wait', 'elapsedSeconds': round(time.monotonic() - start, 3),
                                      'exitCode': run.returncode, 'result': result})
            print(json.dumps(evidence['steps'][-1]), flush=True)
            self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
            self.assertTrue(result['ok'])
            self.assertEqual(result['deliveryStatus'], 'written')
            self.assertNotIn('reply', result)
            deadline = time.monotonic() + 60
            confirmed = False
            while time.monotonic() < deadline and not confirmed:
                with transcript.open('rb') as stream:
                    stream.seek(offset)
                    lines = stream.read().splitlines()
                for line in lines:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if entry.get('type') == 'assistant':
                        blocks = entry.get('message', {}).get('content', [])
                        confirmed = confirmed or any(block.get('type') == 'text' and ack in block.get('text', '')
                                                     for block in blocks if isinstance(block, dict))
                if not confirmed:
                    time.sleep(0.3)
            evidence['nonWaitReplyObservedInTranscript'] = confirmed
            self.assertTrue(confirmed, 'Peer must actually process the non-wait message')
            evidence['passed'] = True
        finally:
            output = ROOT / 'test/results/claude-live.json'
            output.parent.mkdir(exist_ok=True)
            output.write_text(json.dumps(evidence, indent=2) + '\n')


if __name__ == '__main__':
    unittest.main()
