// Opt-in probe loaded ONLY by test/opencode_native_smoke.py in isolated config.
// Exercise the real OpenCode session API without calling a model: decorate only
// promptAsync with noReply=true. Production transport options are unchanged.
import { AgentMsgPlugin } from "../src/agent_msg/plugin_assets/opencode.js";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { writeFileSync } from "node:fs";
import assert from "node:assert/strict";

export const NativeSmoke = async ({client,directory}) => {
  const bridge = await AgentMsgPlugin({directory,client:{session:{
    get:(o)=>client.session.get(o), status:(o)=>client.session.status(o),
    messages:(o)=>client.session.messages(o),
    promptAsync:(o)=>client.session.promptAsync({...o,body:{...o.body,noReply:true}}),
  }}});
  let started=false;
  async function cli(id,args) {
    const output={env:{}};
    await bridge["shell.env"]({sessionID:id},output);
    const result=await promisify(execFile)(process.env.TEST_PYTHON,["-m","agent_msg", "--db",process.env.TEST_DB,...args,"--json"],{
      cwd:directory,env:{...process.env,...output.env,CODEX_THREAD_ID:""},maxBuffer:1024*1024,
    });
    return JSON.parse(result.stdout);
  }
  async function probe() {
    const sessions=[];
    for(const title of ["agent-msg-smoke-sender","agent-msg-smoke-recipient"]) {
      const s=(await client.session.create({body:{title},throwOnError:true})).data;
      await client.session.prompt({path:{id:s.id},body:{noReply:true,agent:"plan",
        model:{providerID:"smoke",modelID:"test"},parts:[{type:"text",text:"seed"}]},throwOnError:true});
      sessions.push(s);
    }
    const [one,two]=sessions;
    const discovery=await cli(one.id,["agents","--harness","opencode"]);
    assert(discovery.agents.some(a=>a.thread_id===one.id));
    assert(discovery.agents.some(a=>a.thread_id===two.id));
    const sent=(await cli(one.id,["send",`opencode:${two.id}`,"native smoke request"])).message;
    assert.equal(sent.status,"sent"); assert.equal(sent.from.thread_id,one.id);
    const reply=(await cli(two.id,["reply",sent.id,"native smoke ACK"])).message;
    assert.equal(reply.status,"sent"); assert.equal(reply.in_reply_to,sent.id);
    for(const [session,id] of [[two,sent.id],[one,reply.id]]) {
      let found;
      for(let i=0;i<40;i++) {
        const history=(await client.session.messages({path:{id:session.id},throwOnError:true})).data;
        found=history.find(m=>m.parts.some(p=>p.type==="text" && p.text.includes(id)));
        if(found) break;
        await new Promise(resolve=>setTimeout(resolve,50));
      }
      assert(found,"native message was not persisted");
      assert.equal(found.info.agent,"plan");
      assert.equal(found.info.model.providerID,"smoke");
      assert.equal(found.info.model.modelID,"test");
      assert.equal(found.info.tools,undefined);
    }
    writeFileSync(process.env.TEST_REPORT,JSON.stringify({ok:true,nativeAPI:true,noModelCalls:true,
      sessions:sessions.map(s=>s.id),request:sent.id,reply:reply.id,
      checked:["native discovery","process attribution","native prompt persistence","model and agent preserved","correlated native reply notification"]},null,2));
  }
  return {...bridge,"chat.message":async(input,output)=>{
    await bridge["chat.message"](input,output);
    if(started) return;
    started=true;
    try { await probe(); }
    catch(error) { writeFileSync(process.env.TEST_REPORT,JSON.stringify({ok:false,error:String(error),stack:error.stack},null,2)); }
    finally { await bridge.dispose(); }
    // End the initial CLI run before it reaches a model. This expected native
    // hook error is not a transport failure; the report is the test oracle.
    throw new Error("AGENT_MSG_SMOKE_COMPLETE (intentional; no model invocation)");
  }};
};
