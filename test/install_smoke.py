"""Opt-in installer check using disposable harness config; never sends messages.

Run: python test/install_smoke.py --marketplace /absolute/path/to/checkout
Use --marketplace nsssayom/agent-msg to exercise the published GitHub marketplace.
Requires the corresponding installed harness CLIs and Git. No model calls.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--marketplace', required=True)
    args = parser.parse_args()
    source = str(Path(args.marketplace).resolve()) if Path(args.marketplace).exists() else args.marketplace
    with tempfile.TemporaryDirectory(prefix='agent-msg-install-') as directory:
        root = Path(directory)
        env = {**os.environ, 'CLAUDE_CONFIG_DIR': str(root/'claude'),
               'CODEX_HOME': str(root/'codex'), 'PYTHONPATH': ''}
        # These are harness-supported config overrides, not the user's normal directories.
        for name in ('claude', 'codex'):
            (root/name).mkdir()

        def run(argv):
            result = subprocess.run(argv, cwd=root, env=env, capture_output=True,
                                    text=True, timeout=120)
            if result.returncode:
                raise RuntimeError(f'{argv[:3]} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}')
            return result.stdout

        for harness, action in (('claude', 'install'), ('codex', 'add')):
            run([harness, 'plugin', 'marketplace', 'add', source])
            run([harness, 'plugin', action, 'agent-msg@agent-msg'])
            manifests = list((root/harness).rglob('.claude-plugin/plugin.json'))
            if not manifests:
                raise RuntimeError(f'{harness}: installed plugin manifest not found')
            copies = set()
            for manifest in manifests:
                if json.loads(manifest.read_text()).get('name') != 'agent-msg':
                    continue
                plugin = manifest.parent.parent
                launcher = plugin/'skills/agent-msg/scripts/agent-msg'
                output = run([sys.executable, str(launcher), '--version']).strip()
                version = json.loads(manifest.read_text())['version']
                if output != version or not (plugin/'LICENSE').is_file():
                    raise RuntimeError(f'{harness}: installed bundle is incomplete')
                copies.add(plugin)
            if not copies:
                raise RuntimeError(f'{harness}: no runnable installed agent-msg bundle')
            print(f'{harness}: marketplace add, install, and bundled launcher passed', flush=True)


if __name__ == '__main__':
    main()
