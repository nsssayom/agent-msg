// Local OpenCode plugin: a private Unix socket bridges to the supplied native
// SDK, including the TUI's in-process fetch. No HTTP server, keys or npm deps.
import { createServer } from "node:net";
import { mkdirSync, lstatSync, writeFileSync, chmodSync, unlinkSync, realpathSync } from "node:fs";
import { join, isAbsolute } from "node:path";
import { homedir } from "node:os";
import { randomBytes, createHash } from "node:crypto";
import { execFileSync } from "node:child_process";

const MAX_FRAME = 1024 * 1024;
const sessionID = /^ses_[A-Za-z0-9_-]+$/;
const uuid = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/;

function privateDirectory(path) {
  mkdirSync(path, { recursive: true, mode: 0o700 });
  const s = lstatSync(path);
  if (!s.isDirectory() || s.uid !== process.getuid() || (s.mode & 0o077)) {
    throw new Error(`agent-msg requires a private current-user directory: ${path}`);
  }
}

export const AgentMsgPlugin = async ({ client, directory }) => {
  if (process.platform === "win32") return {};
  directory = realpathSync(directory);
  const state = isAbsolute(process.env.XDG_STATE_HOME || "")
    ? process.env.XDG_STATE_HOME : join(homedir(), ".local/state");
  const registry = join(state, "agent-msg/opencode");
  const runtime = isAbsolute(process.env.AGENT_MSG_OPENCODE_RUNTIME_DIR || "")
    ? process.env.AGENT_MSG_OPENCODE_RUNTIME_DIR : `/tmp/agent-msg-${process.getuid()}`;
  privateDirectory(registry);
  privateDirectory(runtime);
  const instance = `${process.pid}-${randomBytes(8).toString("hex")}`;
  const socketPath = join(runtime, instance + ".sock");
  if (Buffer.byteLength(socketPath) > 103) throw new Error("agent-msg Unix socket path is too long");
  const registryPath = join(registry, instance + ".json");
  const processStart = execFileSync("ps", ["-p", String(process.pid), "-o", "lstart="],
    { encoding: "utf8", env: { ...process.env, LC_ALL: "C" } }).trim();
  // Only sessions observed by hooks in this process, never historical DB rows.
  const active = new Set();
  const receipts = new Map();
  let disposed = false;

  async function getSession(id) {
    const result = await client.session.get({ path: { id }, query: { directory }, throwOnError: true });
    const info = result.data;
    if (!info || info.id !== id || realpathSync(info.directory) !== directory || info.time?.archived) {
      throw new Error("session is not active in this working directory");
    }
    return info;
  }

  async function handle(input) {
    const reject = (error) => ({ ok: false, stage: "rejected", error });
    if (disposed || input.version !== 1 || input.instance !== instance) return reject("bridge identity changed");
    if (input.op === "list") {
      const status = await client.session.status({ query: { directory }, throwOnError: true });
      const result = [];
      for (const id of active) {
        try {
          const s = await getSession(id);
          result.push({ id: s.id, title: s.title, directory,
            status: status.data?.[id]?.type || "idle" });
        } catch { active.delete(id); }
      }
      return { ok: true, result };
    }
    if (input.op !== "send" || !sessionID.test(input.session_id || "") || !uuid.test(input.message_id || "")
        || typeof input.text !== "string" || !input.text.trim() || Buffer.byteLength(input.text) > 200 * 1024
        || input.cwd !== directory || !active.has(input.session_id)) return reject("invalid or inactive destination");
    const digest = createHash("sha256").update(JSON.stringify([input.session_id, input.text])).digest("hex");
    const previous = receipts.get(input.message_id);
    if (previous) return previous.digest === digest ? previous.promise : reject("message ID reused with different content");
    if (receipts.size >= 4096) return reject("bridge receipt capacity reached; start a new OpenCode process");
    const promise = (async () => {
      let current;
      try { current = await getSession(input.session_id); }
      catch { return reject("session disappeared before native dispatch"); }
      try {
        // Preserve the most recent agent/model/variant. Omitting tools/system
        // leaves session permissions and normal system instructions intact.
        const history = await client.session.messages({ path: { id: input.session_id },
          query: { directory, limit: 20 }, throwOnError: true });
        const last = [...(history.data || [])].reverse().find((m) => m.info?.role === "user")?.info;
        const body = { parts: [{ type: "text", text: input.text }] };
        if (current.agent || last?.agent) body.agent = current.agent || last.agent;
        const model = current.model?.providerID && current.model?.id
          ? { providerID: current.model.providerID, modelID: current.model.id } : last?.model;
        if (model) body.model = { providerID: model.providerID, modelID: model.modelID };
        const variant = current.model?.variant ?? last?.model?.variant ?? last?.variant;
        if (variant && variant !== "default") body.variant = variant;
        // Native promptAsync creates a normal user message, starts an idle
        // session, or feeds its existing loop. Never abort or restart a turn.
        try {
          await client.session.promptAsync({ path: { id: input.session_id },
            query: { directory }, body, throwOnError: true });
        } catch {
          return { ok: false, stage: "uncertain", error: "native prompt acceptance could not be confirmed; do not resend" };
        }
        return { ok: true, result: { transport: "opencode-native", mode: "prompt_async",
          deliveryStatus: "accepted", meaning: "native API accepted prompt; not proof of processing" } };
      } catch { return reject("could not read session settings before native dispatch"); }
    })();
    receipts.set(input.message_id, { digest, promise });
    return promise;
  }

  const server = createServer((socket) => {
    socket.on("error", () => {});
    socket.setTimeout(10_000, () => socket.destroy());
    let buffer = Buffer.alloc(0), consumed = false;
    socket.on("data", (chunk) => {
      if (consumed) return;
      buffer = Buffer.concat([buffer, chunk]);
      if (buffer.length > MAX_FRAME) { socket.destroy(); return; }
      const end = buffer.indexOf(10);
      if (end < 0) return;
      consumed = true;
      const respond = (r) => socket.end(JSON.stringify({ instance, ...r }) + "\n");
      let input;
      try { input = JSON.parse(buffer.subarray(0, end).toString("utf8")); }
      catch { respond({ ok: false, stage: "rejected", error: "invalid JSON" }); return; }
      if (!input || typeof input !== "object") {
        respond({ ok: false, stage: "rejected", error: "invalid request" }); return;
      }
      handle(input).then(respond, () => respond({ ok: false, stage: "uncertain", error: "bridge request failed" }));
    });
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(socketPath, resolve);
  });
  chmodSync(socketPath, 0o600);
  writeFileSync(registryPath, JSON.stringify({ version: 1, instance, pid: process.pid,
    process_start: processStart, directory }), { mode: 0o600, flag: "wx" });
  server.unref();

  function observe(id) { if (typeof id === "string" && sessionID.test(id)) active.add(id); }
  function env(id) {
    return { AGENT_MSG_OPENCODE_SESSION_ID: id, AGENT_MSG_OPENCODE_INSTANCE: instance };
  }
  return {
    "chat.message": async (input) => { observe(input.sessionID); },
    "shell.env": async (input, output) => {
      if (!sessionID.test(input.sessionID || "")) return;
      observe(input.sessionID);
      Object.assign(output.env, env(input.sessionID));
    },
    // OpenCode 1.18.34's V2 bash tool does not yet call shell.env. Use its
    // native before hook for the same session metadata, scoped to that command.
    "tool.execute.before": async (input, output) => {
      observe(input.sessionID);
      if (input.tool !== "bash" || !sessionID.test(input.sessionID || "")
          || typeof output.args?.command !== "string") return;
      output.args.command = `export AGENT_MSG_OPENCODE_SESSION_ID='${input.sessionID}' AGENT_MSG_OPENCODE_INSTANCE='${instance}'\n` + output.args.command;
    },
    event: async ({ event }) => {
      if (event.type === "session.created") observe(event.properties?.info?.id);
      if (event.type === "session.status" && event.properties?.status?.type !== "idle") observe(event.properties?.sessionID);
      if (event.type === "session.deleted") active.delete(event.properties?.info?.id);
    },
    dispose: async () => {
      disposed = true;
      await new Promise((resolve) => server.close(resolve));
      try { unlinkSync(registryPath); } catch (e) { if (e.code !== "ENOENT") throw e; }
      // node:net removes its own socket on close; never remove a replacement.
    },
  };
};
