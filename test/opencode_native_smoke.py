"""Opt-in real OpenCode API smoke test; isolated sessions and no inference.

python test/opencode_native_smoke.py --opencode /path/to/opencode --output NEW_DIR
Outputs are retained. Does not stop an existing process or start a TCP listener.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--opencode',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    root=Path(args.output).resolve();root.mkdir(parents=True,exist_ok=False)
    config=root/'config';(config/'plugins').mkdir(parents=True)
    (config/'plugins/agent-msg-smoke.js').symlink_to(Path(__file__).with_suffix('.mjs').resolve())
    conf={'model':'smoke/test','small_model':'smoke/test','enabled_providers':['smoke'],
          'autoupdate':False,'share':'disabled','provider':{'smoke':{'npm':'@ai-sdk/openai-compatible',
          'options':{'baseURL':'http://127.0.0.1:1/v1','apiKey':'unused-test-value'},
          'models':{'test':{'name':'No inference smoke fixture','limit':{'context':8192,'output':1024}}}}}}
    (config/'opencode.json').write_text(json.dumps(conf))
    env={**os.environ,'OPENCODE_CONFIG_DIR':str(config),'OPENCODE_CONFIG':str(config/'opencode.json'),
         'OPENCODE_DISABLE_CLAUDE_CODE':'1','OPENCODE_DISABLE_EXTERNAL_SKILLS':'1',
         'OPENCODE_DISABLE_AUTOUPDATE':'1','OPENCODE_DISABLE_MODELS_FETCH':'1',
         'XDG_CONFIG_HOME':str(root/'global'),'XDG_DATA_HOME':str(root/'data'),
         'XDG_STATE_HOME':str(root/'state'),'AGENT_MSG_OPENCODE_RUNTIME_DIR':str(root/'r'),
         'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src'),'TEST_PYTHON':sys.executable,
         'TEST_DB':str(root/'journal.db'),'TEST_REPORT':str(root/'result.json'),'CODEX_THREAD_ID':''}
    # No timeout that would kill the process. OpenCode exits normally after the
    # probe's intentional hook error; process lifetime stays visible to caller.
    result=subprocess.run([args.opencode,'run','--model','smoke/test','agent-msg-native-smoke'],
        cwd=root,env=env,capture_output=True,text=True)
    (root/'stdout.log').write_text(result.stdout);(root/'stderr.log').write_text(result.stderr)
    report=json.loads((root/'result.json').read_text()) if (root/'result.json').exists() else {'ok':False,'error':'probe did not run','stderr':result.stderr}
    print(json.dumps(report,indent=2))
    return 0 if report['ok'] else 1


if __name__=='__main__':raise SystemExit(main())
