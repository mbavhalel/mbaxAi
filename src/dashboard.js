import { createClient } from '@supabase/supabase-js';
import { createStore, emptyState, recordsCsv } from './store.js';
import './style.css';

const $ = selector => document.querySelector(selector);
const esc = value => String(value).replace(/[&<>"']/g, char => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[char]));
let S = emptyState();
let hist = [], link = false, realtimeOnline = false, rx = Date.now(), cfgFilled = false;
let activeUser = null, initialized = false, generation = 0, refreshSequence = 0, historySequence = 0;
let channel, pollTimer, historyPollTimer, refreshTimer, historyTimer;
const age = () => S.climate.age === null ? null : S.climate.age + (Date.now() - rx) / 1000;

function message(text = '', error = false) {
  $('#message').textContent = text;
  $('#message').classList.toggle('error', error);
  $('#message').hidden = !text;
}
function loginError(text = '') {
  $('#loginError').textContent = text;
  $('#loginError').hidden = !text;
}
function configuredClient() {
  const url = (import.meta.env.VITE_SUPABASE_URL || '').trim();
  const key = (import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY || '').trim();
  if (!url || !key || url.includes('YOUR_PROJECT') || key.includes('YOUR_PUBLIC')) return null;
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== 'https:' && !['localhost', '127.0.0.1'].includes(parsed.hostname)) return null;
    if (key.startsWith('sb_secret_')) return null;
    if (key.split('.').length === 3) {
      const payload = JSON.parse(atob(key.split('.')[1].replace(/-/g, '+').replace(/_/g, '/')));
      if (payload.role === 'service_role') return null;
    }
    return createClient(url, key);
  } catch {
    return null;
  }
}
const client = configuredClient();
const store = client && createStore(client);

function resetData() {
  S = emptyState(); hist = []; cfgFilled = false; rx = Date.now();
  link = false; realtimeOnline = false;
  for (const selector of ['#an', '#au', '#as', '#al', '#password']) $(selector).value = '';
  fillCfg(); renderClimate(); renderHist(); renderOverview(); renderStock(); renderConn();
  $('#appView').hidden = true;
}
function accessDenied(error) {
  return ['42501', 'PGRST301', 'PGRST302'].includes(error.code) || [401, 403].includes(error.status);
}
function errorMessage(error) {
  if (error.code === '23505') return 'That tag ID is already registered. Edit the existing item instead.';
  return accessDenied(error)
    ? 'This account does not have access to the storeroom. Ask your administrator to add you, or sign in with another account.'
    : error.message || 'Could not connect. Check your connection and try again.';
}
async function refresh() {
  if (!activeUser) return;
  const current = generation, sequence = ++refreshSequence;
  try {
    const state = await store.state();
    if (current !== generation || sequence !== refreshSequence) return;
    link = true; applyState(state); $('#appView').hidden = false; message();
  } catch (error) {
    if (current !== generation || sequence !== refreshSequence) return;
    link = false;
    if (accessDenied(error)) resetData();
    renderConn(); message(errorMessage(error), true);
  }
}
async function loadHist() {
  if (!activeUser) return;
  const current = generation, sequence = ++historySequence;
  const range = $('#range').value;
  try {
    const rows = await store.history(range);
    if (current !== generation || sequence !== historySequence || range !== $('#range').value) return;
    hist = rows; renderHist();
  } catch (error) {
    if (current === generation && sequence === historySequence) message(errorMessage(error), true);
  }
}
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => { void refresh(); }, 150);
  if (!historyTimer) historyTimer = setTimeout(() => {
    historyTimer = null; void loadHist();
  }, 5000);
}
function listen() {
  const current = generation;
  channel = client.channel('storeroom-' + activeUser);
  // Inventory records stay behind RLS-protected RPC reads; Realtime only sends
  // a data-free version signal that tells authorized dashboards to refresh.
  channel = channel.on('postgres_changes', { event: 'UPDATE', schema: 'public', table: 'change_signal' }, () => {
      if (current === generation) scheduleRefresh();
  });
  channel.subscribe(status => {
    if (current !== generation) return;
    realtimeOnline = status === 'SUBSCRIBED';
    renderConn();
    if (realtimeOnline) { void refresh(); void loadHist(); }
  });
}
function setSession(session) {
  const user = session?.user ?? null;
  if (initialized && activeUser === (user?.id ?? null)) {
    if (user) $('#signedInAs').textContent = user.email;
    return;
  }
  initialized = true; ++generation; ++refreshSequence; ++historySequence;
  clearInterval(pollTimer); clearInterval(historyPollTimer);
  clearTimeout(refreshTimer); clearTimeout(historyTimer); historyTimer = null;
  if (channel) { void client.removeChannel(channel); channel = null; }
  activeUser = user?.id ?? null;
  resetData(); message(); loginError();
  $('#loginView').hidden = !!user;
  $('#account').hidden = !user;
  $('#signedInAs').textContent = user?.email ?? '';
  if (!user) return;
  message('Connecting to your storeroom…');
  listen(); void refresh(); void loadHist();
  pollTimer = setInterval(() => { if (!document.hidden) void refresh(); }, 15000);
  historyPollTimer = setInterval(() => { if (!document.hidden) void loadHist(); }, 60000);
}
async function save(action, data, button) {
  if (!activeUser) return false;
  const current = generation;
  if (button) button.disabled = true;
  try {
    await store.manage(action, data);
    if (current !== generation) return false;
    await refresh();
    // Delivery is optional and independent of saving stock. Failed deliveries
    // remain in the backend outbox for a later device event or scheduled retry.
    void client.functions.invoke('process-alerts', { body: {} }).catch(() => {});
    return true;
  } catch (error) {
    if (current === generation) message(errorMessage(error), true);
    return false;
  } finally {
    if (button) button.disabled = false;
  }
}
/* ---------- climate ---------- */
const verdict=(v,min,max,lo,hi)=>v<min?lo:v>max?hi:'In range';
function band(id,v,min,max,top){
  const pc=x=>Math.max(0,Math.min(100,x/top*100));
  $('#'+id+'z').style.left=pc(min)+'%';$('#'+id+'z').style.width=(pc(max)-pc(min))+'%';$('#'+id+'m').style.left=pc(v)+'%';
}
function spark(id,arr,min,max){
  if(arr.length<2){$('#'+id).innerHTML='';return}
  const lo=Math.min(...arr,min),hi=Math.max(...arr,max),r=hi-lo||1,y=v=>40-((v-lo)/r)*36;
  const pts=arr.map((v,i)=>(i/(arr.length-1)*240).toFixed(1)+','+y(v).toFixed(1)).join(' ');
  $('#'+id).innerHTML=`<rect x="0" y="${y(max)}" width="240" height="${y(min)-y(max)}" fill="var(--ok)" opacity=".15"/><polyline points="${pts}" fill="none" stroke="var(--ink)" stroke-width="1.6" vector-effect="non-scaling-stroke"/>`;
}
function renderClimate(){
  const {t,h}=S.climate,c=S.settings;
  $('#tr').textContent=c.tMin+'–'+c.tMax+'° ideal';$('#hr').textContent=c.hMin+'–'+c.hMax+'% ideal';
  if(t!==null){
    $('#tv').textContent=t.toFixed(1);$('#hv').textContent=h.toFixed(1);
    const tj=verdict(t,c.tMin,c.tMax,'Too cold','Too warm'),hj=verdict(h,c.hMin,c.hMax,'Too dry','Too humid');
    $('#tj').textContent=tj;$('#hj').textContent=hj;
    $('#tj').style.color=tj==='In range'?'var(--ok)':'var(--bad)';$('#hj').style.color=hj==='In range'?'var(--ok)':'var(--bad)';
    band('t',t,c.tMin,c.tMax,40);band('h',h,c.hMin,c.hMax,100);
  }else{$('#tv').textContent=$('#hv').textContent='--';$('#tj').textContent=$('#hj').textContent='';band('t',0,c.tMin,c.tMax,40);band('h',0,c.hMin,c.hMax,100)}
  renderAlerts();
}
function renderHist(){
  const c=S.settings,tt=hist.map(r=>r.t),hh=hist.map(r=>r.h),span=$('#range').selectedOptions[0].text.toLowerCase();
  spark('ts',tt,c.tMin,c.tMax);spark('hs',hh,c.hMin,c.hMax);
  $('#tc').textContent=tt.length?`Last ${span}: ${Math.min(...tt).toFixed(1)} to ${Math.max(...tt).toFixed(1)}°C`:'No readings saved for this period yet.';
  $('#hc').textContent=hh.length?`Last ${span}: ${Math.min(...hh).toFixed(1)} to ${Math.max(...hh).toFixed(1)}%`:'No readings saved for this period yet.';
}

const lvl=p=>p.stock<=0?'out':p.stock<=p.low?'low':'ok';
function renderAlerts(){
  const a=[],c=S.settings,{t,h}=S.climate;
  if(t!==null){
    if(t<c.tMin||t>c.tMax)a.push(`Temperature is ${t.toFixed(1)}°C, outside ${c.tMin}–${c.tMax}°C.`);
    if(h<c.hMin||h>c.hMax)a.push(`Humidity is ${h.toFixed(1)}%, outside ${c.hMin}–${c.hMax}%.`);
  }
  S.products.forEach(p=>{const l=lvl(p);if(l==='out')a.push(`${p.name} is out of stock.`);else if(l==='low')a.push(`${p.name} is low: ${p.stock} left.`)});
  const el=$('#alerts');el.style.display=a.length?'block':'none';el.innerHTML=a.map(esc).join('<br>');
}

/* ---------- stock ---------- */
const ICONS=[['apple','🍎'],['orange','🍊'],['banana','🍌'],['pear','🍐'],['lemon','🍋'],['grape','🍇'],['strawberr','🍓'],['tomato','🍅'],['carrot','🥕'],['potato','🥔'],['onion','🧅'],['egg','🥚'],['milk','🥛'],['bread','🍞'],['cheese','🧀'],['water','💧'],['glove','🧤']];
const icon=n=>{n=String(n).toLowerCase();for(const [k,v] of ICONS)if(n.includes(k))return v;return '📦'};
function renderOverview(){
  const total=S.products.reduce((a,p)=>a+p.stock,0);
  $('#ovt').textContent=S.products.length?`${total} units across ${S.products.length} item${S.products.length>1?'s':''}`:'';
  $('#tiles').innerHTML=S.products.length?S.products.map(p=>{
    const l=lvl(p),lab={ok:'In stock',low:'Running low',out:'Out of stock'}[l];
    return `<div class="tile ${l}"><div class="ic">${icon(p.name)}</div><div class="tn">${esc(p.name)}</div><div class="tq">${p.stock}</div><span class="chip ${l}">${lab}</span></div>`}).join('')
    :'<div class="empty">No items yet. Add one below.</div>';
}
function renderStock(){
  let h='';
  S.pending.forEach(uid=>{
    h+=`<tr class="newtag" data-p="${esc(uid)}"><td><input class="ps" type="number" min="0" max="1000000" value="10" aria-label="Starting stock"></td>
    <td><input class="pn" placeholder="Name this item" aria-label="Item name"><div class="uid">New tag ${esc(uid)}</div></td>
    <td><input class="pl" type="number" min="0" max="1000000" value="3" aria-label="Low-stock level"><div class="uid">Alert when stock is at or below this</div></td>
    <td class="act"><button class="pri" data-action="register">Add item</button> <button data-action="ignore">Ignore</button></td></tr>`;
  });
  S.products.forEach(p=>{
    const c={out:['out','Out'],low:['low','Low'],ok:['ok','OK']}[lvl(p)];
    h+=`<tr data-uid="${esc(p.uid)}"><td class="q"><input class="qty" type="number" min="0" max="1000000" step="1" value="${p.stock}" data-field="stock" aria-label="Stock for ${esc(p.name)}"></td>
    <td>${icon(p.name)} ${esc(p.name)}<div class="uid">${esc(p.uid)} · alert at or below <input class="lw" type="number" min="0" max="1000000" step="1" value="${p.low}" data-field="low" aria-label="Alert level for ${esc(p.name)}"></div></td>
    <td><span class="chip ${c[0]}">${c[1]}</span></td>
    <td class="act"><button data-action="remove">Remove</button></td></tr>`;
  });
  $('#stock').innerHTML=h||'<tr><td colspan="4" class="empty">No items yet. Hold a tag against the reader to register it.</td></tr>';
  $('#log').innerHTML=S.scans.length?S.scans.map(l=>{
    const d=l.kind==='manual'?`set to ${l.after} by hand`:l.after===null?'not registered':`${l.after} left`;
    return `<li><span>${l.after===null&&l.kind!=='manual'?'':icon(l.name)+' '}${esc(l.name)} <span class="uid">${esc(l.uid)}</span></span><span class="sub">${d} · ${new Date(l.ts*1000).toLocaleString([],{dateStyle:'short',timeStyle:'short'})}</span></li>`}).join(''):'<li class="empty">No scans yet.</li>';
}

const fmtAge = a => a < 60 ? Math.round(a) + ' s' : a < 3600 ? Math.round(a / 60) + ' min' : (a / 3600).toFixed(1) + ' h';
function renderConn() {
  const connection = $('#conn'), elapsed = age();
  connection.className = 'pill ' + (!link ? 'bad' : realtimeOnline ? 'on' : 'warn');
  connection.lastChild.textContent = !link ? 'Reconnecting' : realtimeOnline ? 'Live' : 'Connected';
  const device = link ? S.device : 'unknown';
  $('#dev').className = 'pill ' + (device === 'online' ? 'on' : device === 'offline' ? 'bad' : '');
  $('#dev').lastChild.textContent = device === 'online' ? 'Device online' : device === 'offline' ? 'Device offline' : 'Device unknown';
  $('#age').textContent = elapsed === null ? 'No readings yet.' : `Last reading ${fmtAge(elapsed)} ago.`;
}
function applyState(state) {
  S = state; rx = Date.now();
  if (!cfgFilled) { fillCfg(); cfgFilled = true; }
  renderClimate(); renderHist(); renderConn(); renderOverview();
  if (!document.activeElement?.closest('#stock')) renderStock();
}
function fillCfg() {
  const values = S.settings;
  $('#c1').value = values.tMin; $('#c2').value = values.tMax;
  $('#c3').value = values.hMin; $('#c4').value = values.hMax;
}
$('#addForm').addEventListener('submit', async event => {
  event.preventDefault();
  const name = $('#an'), uid = $('#au'), stock = $('#as'), low = $('#al');
  if (await save('add', { name: name.value.trim(), uid: uid.value.trim(),
    stock: Number(stock.value), low: low.value === '' ? 3 : Number(low.value) }, $('#addBtn'))) {
    name.value = uid.value = stock.value = low.value = ''; name.focus();
  }
});
$('#lastBtn').addEventListener('click', () => {
  const uid = S.pending[0] || S.scans[0]?.uid;
  if (uid) { $('#au').value = uid; $('#an').focus(); }
  else message('No tag has been scanned yet. Hold a tag against the reader first.');
});
$('#stock').addEventListener('click', async event => {
  const button = event.target.closest('button[data-action]');
  if (!button) return;
  const row = button.closest('tr'), uid = row.dataset.uid || row.dataset.p;
  if (button.dataset.action === 'register') {
    const name = row.querySelector('.pn');
    if (!name.value.trim()) { name.focus(); return; }
    await save('add', { uid, name: name.value.trim(), stock: Number(row.querySelector('.ps').value),
      low: Number(row.querySelector('.pl').value) }, button);
  } else if (button.dataset.action === 'ignore') {
    await save('ignore', { uid }, button);
  } else if (button.dataset.action === 'remove') {
    const product = S.products.find(item => item.uid === uid);
    if (product && confirm('Remove ' + product.name + '?')) await save('delete', { uid }, button);
  }
});
$('#stock').addEventListener('change', async event => {
  const input = event.target.closest('input[data-field]');
  if (!input || !input.reportValidity()) return;
  const uid = input.closest('tr').dataset.uid;
  await save('update', { uid, [input.dataset.field]: Number(input.value) }, input);
});
$('#stock').addEventListener('focusout', () => setTimeout(() => {
  if (!document.activeElement?.closest('#stock')) renderStock();
}, 0));
$('#saveCfg').addEventListener('click', () => {
  void save('settings', { tMin: Number($('#c1').value), tMax: Number($('#c2').value),
    hMin: Number($('#c3').value), hMax: Number($('#c4').value) }, $('#saveCfg'));
});
$('#reset').addEventListener('click', () => {
  if (confirm('Delete all items and scan history? Temperature and humidity history is kept.')) {
    void save('clear', {}, $('#reset'));
  }
});
$('#range').addEventListener('change', () => { void loadHist(); });
for (const kind of ['readings', 'scans']) {
  const button = $(kind === 'readings' ? '#exportReadings' : '#exportScans');
  button.addEventListener('click', async () => {
    if (!activeUser) return;
    const current = generation; button.disabled = true;
    try {
      const records = await store.records(kind, () => current === generation && !!activeUser);
      if (current !== generation) return;
      const blob = new Blob(['\uFEFF', recordsCsv(kind, records)], { type: 'text/csv;charset=utf-8' });
      const url = URL.createObjectURL(blob), anchor = document.createElement('a');
      anchor.href = url; anchor.download = kind + '.csv'; anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      if (current === generation) message(errorMessage(error), true);
    } finally {
      button.disabled = false;
    }
  });
}
$('#loginForm').addEventListener('submit', async event => {
  event.preventDefault();
  if (!client) return;
  const button = $('#loginButton'); button.disabled = true; loginError();
  try {
    const { error } = await client.auth.signInWithPassword({
      email: $('#email').value.trim(), password: $('#password').value,
    });
    if (error) loginError(error.message);
  } catch {
    loginError('Could not sign in. Check your connection and try again.');
  } finally {
    button.disabled = false;
  }
});
$('#signOut').addEventListener('click', async () => {
  // Hide private data immediately, even if the network is unavailable.
  setSession(null);
  const { error } = await client.auth.signOut({ scope: 'local' });
  if (error) loginError('Could not finish signing out. Close this tab and try again.');
});
function reconnect() {
  if (activeUser) { void refresh(); void loadHist(); }
}
window.addEventListener('online', reconnect);
window.addEventListener('focus', reconnect);
document.addEventListener('visibilitychange', () => { if (!document.hidden) reconnect(); });
setInterval(() => { if (activeUser) renderConn(); }, 1000);
resetData();
if (!client) {
  $('#loginButton').disabled = true;
  message('This dashboard has not been connected yet. Contact the person who set it up.', true);
} else {
  client.auth.onAuthStateChange((_event, session) => {
    // Keep Supabase calls outside the synchronous authentication callback.
    queueMicrotask(() => setSession(session));
  });
  client.auth.getSession().then(({ data, error }) => {
    if (error) loginError('Your saved session could not be restored. Sign in again.');
    setSession(data.session);
  }).catch(() => { setSession(null); loginError('Could not restore your session. Sign in again.'); });
}
