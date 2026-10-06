// Native socket integration fixture. Fake SDK records calls, never runs a model.
import { AgentMsgPlugin } from "../src/agent_msg/plugin_assets/opencode.js";
import { createInterface } from "node:readline";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
const calls = [];
const sessions = new Map(["ses_one", "ses_two", "ses_history"].map(id => [id, {
  id, title: id, directory: process.cwd(), time: {}, agent: "plan",
  model: { providerID: "local", id: "test", variant: "low" },
}]));
let fail = false;
const client = { session: {
  get: async ({path}) => { if (!sessions.has(path.id)) throw Error("gone"); return {data:sessions.get(path.id)}; },
  status: async () => ({data:{ses_two:{type:"busy"}}}),
  messages: async () => ({data:[{info:{role:"user",agent:"build",model:{providerID:"old",modelID:"old"}}}]}),
  promptAsync: async (args) => { calls.push(args); if (fail) throw Error("simulated drop"); return {data:undefined}; },
}};
const hooks = await AgentMsgPlugin({client,directory:process.cwd()});
await hooks["chat.message"]({sessionID:"ses_one"});
await hooks.event({event:{type:"session.created",properties:{info:sessions.get("ses_two")}}});
const shell = {env:{EXISTING:"kept"}};
await hooks["shell.env"]({sessionID:"ses_one"},shell);
const bash = {args:{command:"printf 'hello'"}};
await hooks["tool.execute.before"]({tool:"bash",sessionID:"ses_one"},bash);
console.log(JSON.stringify({ready:true,env:shell.env,command:bash.args.command}));
const lines = createInterface({input:process.stdin});
for await (const line of lines) {
  const op=JSON.parse(line);
  if(op.op==="close") { await hooks.dispose(); break; }
  if(op.op==="fail") fail=op.value;
  if(op.op==="delete") sessions.delete(op.id);
  if(op.op==="cli") {
    const result=await promisify(execFile)(process.env.TEST_PYTHON,["-m","agent_msg",...op.args], {
      env:{...process.env,...shell.env,AGENT_MSG_OPENCODE_SESSION_ID:op.session||"ses_one",CODEX_THREAD_ID:""},
    });
    console.log(JSON.stringify({cli:JSON.parse(result.stdout)}));
  } else console.log(JSON.stringify({calls}));
}
