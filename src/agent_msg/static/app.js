/* No remote assets, HTML injection, frameworks, or write endpoints. */
'use strict';
const $ = id => document.getElementById(id);
const fragment = new URLSearchParams(location.hash.slice(1));
if (fragment.has('token')) { sessionStorage.setItem('agent-msg-token', fragment.get('token')); history.replaceState(null, '', location.pathname); }
const token = sessionStorage.getItem('agent-msg-token') || '';
const state = {rows: [], selected: null, live: true, busy: false, revision: 0};
const labels = {prepared: 'Prepared', dispatching: 'Dispatching', sent: 'Sent', received: 'Reply recorded', failed: 'Failed', uncertain: 'Uncertain'};
const explanations = {
  prepared: 'Recorded before dispatch. No send outcome is recorded.',
  dispatching: 'Dispatch started. An outcome has not yet been recorded.',
  sent: 'Native transport accepted the request or completed a socket write. This is not proof the recipient read it.',
  received: 'A correlated reply was committed to the journal. No native notification was requested.',
  failed: 'The operation failed. Inspect its delivery events before deciding what to do.',
  uncertain: 'The send did not complete cleanly. The recipient may have received it; do not automatically resend.'
};
function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = String(text);
  return el;
}
function harness(p) { return ['claude', 'codex'].includes(p?.harness) ? p.harness : 'other'; }
function agent(p) { return node('span', 'agent ' + harness(p), p?.name || p?.harness || 'Unknown'); }
function badge(status) {
  const el = node('span', 'status ' + (Object.hasOwn(labels, status) ? status : ''), labels[status] || status);
  el.title = explanations[status] || '';
  return el;
}
function clock(value, full = false) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : (full ? date.toLocaleString() : date.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}));
}
async function api(path) {
  const response = await fetch(path, {credentials: 'omit', cache:'no-store', headers:{Authorization:'Bearer '+token}});
  if (response.status === 403) throw new Error('Session expired. Reopen the link printed by agent-msg ui.');
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}
function note(message) { $('list-note').hidden = !message; $('list-note').textContent = message || ''; }
function renderList() {
  $('list').replaceChildren();
  for (const row of state.rows) {
    const item = node('li');
    const button = node('button', 'row'); button.type = 'button';
    button.setAttribute('aria-current', String(row.id === state.selected));
    const route = node('div', 'route');
    const to = agent(row.to); to.classList.add('to');
    route.append(agent(row.from), to, node('time', 'when', clock(row.created_at)));
    button.append(route, node('div','snippet', row.msg.replace(/\s+/g, ' ')), badge(row.status));
    button.addEventListener('click', () => select(row.id));
    item.append(button); $('list').append(item);
  }
  $('count').textContent = state.rows.length;
}
function facts(data) {
  const dl = node('dl', 'facts');
  for (const [label, value] of Object.entries(data)) {
    dl.append(node('dt', '', label), node('dd', label === 'Name' ? '' : 'id', value ?? 'Not observed'));
  }
  return dl;
}
function disclosure(title, content) {
  const el = node('details'); el.append(node('summary','',title), content); return el;
}
function copyButton(label, text) {
  const button = node('button', 'action', label); button.type = 'button';
  button.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(text); button.textContent = 'Copied'; }
    catch (_) { button.textContent = 'Copy unavailable'; }
    setTimeout(() => { button.textContent = label; }, 1500);
  });
  return button;
}
function renderDetail(message, conversation) {
  const detail = $('detail'); detail.replaceChildren();
  const title = node('div', 'detail-title');
  const heading = node('div'); heading.append(node('p', 'eyebrow', 'CONVERSATION'), node('h2','', `${message.from.name || message.from.harness} → ${message.to.name || message.to.harness}`));
  title.append(heading, copyButton('Copy ID', message.id)); detail.append(title);
  detail.append(node('p','subtle', `${clock(message.created_at, true)} · ${conversation.length} message${conversation.length === 1 ? '' : 's'}`));
  const timeline = node('div', 'timeline');
  for (const row of conversation) {
    const card = node('article', 'message-card ' + harness(row.from) + (row.id === message.id ? ' selected' : ''));
    const head = node('div', 'message-head');
    head.append(agent(row.from), node('span','arrow-label','→'), agent(row.to), badge(row.status));
    const choose = node('button','message-time', clock(row.created_at, true)); choose.type='button';
    choose.title = 'Inspect this message'; choose.addEventListener('click', () => select(row.id));
    card.append(head, node('pre', 'message-body', row.msg), choose);
    timeline.append(card);
  }
  detail.append(timeline);
  detail.append(node('h3','','Delivery'), node('p','subtle', explanations[message.status] || message.status));
  const events = node('ol','deliveries');
  for (const event of message.deliveries || []) {
    const row = node('li');
    const description = node('div'); description.append(node('span','',event.transport));
    if (Object.keys(event.detail || {}).length) description.append(disclosure('Receipt', node('pre','json',JSON.stringify(event.detail,null,2))));
    row.append(node('time','',clock(event.created_at,true)),badge(event.status),description); events.append(row);
  }
  detail.append(events);
  const provenance = node('div','provenance');
  const claimed = node('section'); claimed.append(node('h3','','Declared sender'), node('p','explain','Harness attribution is a claim. It is not an authenticated identity.'));
  claimed.append(facts({Harness:message.from.harness,Name:message.from.name,Thread:message.from.thread_id,Workdir:message.from.cwd}));
  const observed = node('section'); const obs = message.observed || {};
  observed.append(node('h3','','Observed process'), node('p','explain','Collected independently by the local Python process at send time.'));
  observed.append(facts({'Identity source':obs.identity_source,PID:obs.pid,'Parent PID':obs.ppid,UID:obs.uid,Executable:obs.executable,Workdir:obs.cwd,'Observed at':obs.observed_at}));
  if (obs.identity_note) observed.append(node('p','subtle',obs.identity_note));
  if (obs.ancestors?.length) {
    const chain = node('ol','chain');
    for (const ancestor of obs.ancestors) chain.append(node('li','id',`${ancestor.pid} · ${ancestor.executable || 'unknown executable'}`));
    observed.append(disclosure('Process ancestry',chain));
  }
  provenance.append(claimed, observed); detail.append(provenance);
  const raw = node('div'); raw.append(copyButton('Copy JSON', JSON.stringify(message,null,2)), node('pre','json',JSON.stringify(message,null,2)));
  detail.append(disclosure('Envelope & full record',raw));
}
async function select(id) {
  state.selected = id; renderList();
  try {
    const [message, conversation] = await Promise.all([api('/api/messages/'+encodeURIComponent(id)),api('/api/messages/'+encodeURIComponent(id)+'/conversation')]);
    if (state.selected === id) renderDetail(message, conversation.messages);
  } catch (error) { note(error.message); }
}
async function load(older = false) {
  if (state.busy) return;
  state.busy = true;
  const revision = state.revision;
  try {
    const query = new URLSearchParams({limit:'60'});
    for (const field of ['q','peer','status']) if ($(field).value) query.set(field,$(field).value);
    if (older && state.rows.length) query.set('before_seq',state.rows.at(-1).seq);
    const [data, stats, peers] = await Promise.all([api('/api/messages?'+query),api('/api/stats'),api('/api/peers')]);
    if (revision !== state.revision) return;
    state.rows = older ? state.rows.concat(data.messages) : data.messages;
    $('older').hidden = data.messages.length < 60;
    $('stats').textContent = `${stats.messages} messages · ${stats.peers} agents`;
    const peer = $('peer').value;
    $('peer').replaceChildren(new Option('All agents',''));
    for (const p of peers.peers) $('peer').append(new Option(`${p.name || p.thread_id || 'Local process'} · ${p.harness}`, p.key));
    $('peer').value = peer;
    renderList(); note(state.rows.length ? '' : 'No messages here yet. Send a message from the CLI, or adjust your filters.');
    if (state.selected) await select(state.selected);
  } catch(error) { note(error.message); }
  finally { state.busy=false; if (revision !== state.revision) load(); }
}
let debounce;
$('filters').addEventListener('submit', e => e.preventDefault());
$('filters').addEventListener('input', () => { clearTimeout(debounce); state.revision++; debounce=setTimeout(()=>load(),180); });
$('older').addEventListener('click', () => { state.live=false; liveButton(); load(true); });
function liveButton() { $('refresh').textContent=state.live?'● Live':'○ Paused'; $('refresh').setAttribute('aria-pressed',String(state.live)); }
$('refresh').addEventListener('click', () => { state.live=!state.live; liveButton(); if(state.live)load(); });
document.addEventListener('keydown', event => {
  if (/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName) || event.metaKey || event.ctrlKey || event.altKey) return;
  if(event.key==='/') {event.preventDefault();$('q').focus();}
  if(['j','k'].includes(event.key) && state.rows.length) {
    event.preventDefault(); const index=state.rows.findIndex(r=>r.id===state.selected);
    select(state.rows[Math.max(0,Math.min(state.rows.length-1,index+(event.key==='j'?1:-1)))].id);
  }
});
load(); setInterval(()=>{if(state.live&&!document.hidden)load();},4000);
