<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="csrf-token" content="{{ csrf_token() }}">
<title>ClickBridge — Control Panel</title>
<style>
:root{
  --bg:#0d1117; --panel:#161b22; --panel2:#1c2330; --line:#2a3343;
  --text:#e6edf3; --dim:#8b98a9; --ok:#3fb950; --warn:#d29922; --bad:#f85149;
  --accent:#4493f8; --radius:14px;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.45 "Segoe UI",system-ui,-apple-system,sans-serif}
.wrap{max-width:1060px;margin:0 auto;padding:26px 18px 60px}
header{display:flex;align-items:center;gap:14px;margin-bottom:22px}
header h1{font-size:21px;margin:0;letter-spacing:.2px}
header .tag{color:var(--dim);font-size:12.5px;margin-top:3px}
.pill{padding:3px 11px;border-radius:999px;font-size:12px;font-weight:600;
  border:1px solid var(--line);color:var(--dim);white-space:nowrap}
.pill.on{color:var(--ok);border-color:#2ea04355;background:#12331e33}
.pill.off{color:var(--warn);border-color:#d2992255;background:#3b2f0e33}
.pill.down{color:var(--bad);border-color:#f8514955;background:#3d151733}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:18px}
.card h2{margin:0 0 12px;font-size:13px;letter-spacing:.8px;
  text-transform:uppercase;color:var(--dim)}
.row{display:flex;justify-content:space-between;align-items:center;
  padding:7px 0;font-size:14px}
.row .k{color:var(--dim)}
.row .v{font-weight:600;text-align:right}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;
  margin-right:8px;vertical-align:1px}
.dot.ok{background:var(--ok);box-shadow:0 0 8px #3fb95088}
.dot.warn{background:var(--warn)}
.dot.bad{background:var(--bad)}
.battery{height:9px;border-radius:6px;background:#0a0d12;overflow:hidden;
  border:1px solid var(--line);margin-top:10px}
.battery>i{display:block;height:100%;background:linear-gradient(90deg,#2ea043,#3fb950);
  border-radius:6px;transition:width .4s}
.battery.low>i{background:linear-gradient(90deg,#d29922,#f0883e)}
.controls{display:flex;gap:14px;align-items:center;justify-content:space-between;
  flex-wrap:wrap}
.switch{display:flex;align-items:center;gap:12px;cursor:pointer;user-select:none}
.switch .track{width:54px;height:30px;border-radius:999px;background:#2f3a4d;
  border:1px solid var(--line);position:relative;transition:background .2s}
.switch.on .track{background:#1d5c33;border-color:#2ea04388}
.track>i{position:absolute;top:3px;left:3px;width:22px;height:22px;border-radius:50%;
  background:#aab6c5;transition:transform .2s,background .2s}
.switch.on .track>i{transform:translateX(24px);background:#7ee2a8}
.switch .label{font-weight:700;font-size:15px}
.switch .label small{display:block;font-weight:400;color:var(--dim);font-size:12px}
button.btn{background:var(--panel2);color:var(--text);border:1px solid var(--line);
  padding:9px 15px;border-radius:10px;font-size:13.5px;font-weight:600;cursor:pointer}
button.btn:hover{border-color:var(--accent)}
button.btn:disabled{opacity:.45;cursor:default}
button.danger{color:#ffb1ab;border-color:#f8514966}
button.danger:hover{background:#3d1517aa}
.hint{color:var(--dim);font-size:12.5px;margin-top:12px;line-height:1.5}
.events{max-height:330px;overflow-y:auto}
.ev{display:flex;gap:10px;padding:6px 0;border-bottom:1px solid #21303f55;
  font-size:13px}
.ev time{color:var(--dim);font-variant-numeric:tabular-nums;min-width:58px}
.ev .kind{min-width:64px;font-weight:700;font-size:11.5px;text-transform:uppercase;
  letter-spacing:.5px}
.kind.click{color:#6ee7b7}.kind.shift{color:#93c5fd}.kind.app{color:#c4b5fd}
.kind.battery{color:#fcd34d}.kind.pairing{color:#fda4af}.kind.bridge,.kind.control{color:#9aa5b1}
.kind.onboarding{color:#93c5fd}
.offline{padding:26px;text-align:center;color:var(--warn);font-size:14.5px}
.map{font-size:13px;color:var(--dim)}
.map b{color:var(--text);font-weight:600}
.tagchip{display:inline-block;margin:2px;padding:1px 8px;border-radius:999px;
  background:#212b3b;border:1px solid var(--line);font-size:12px}
footer{margin-top:26px;color:var(--dim);font-size:12px;line-height:1.7}
footer code{color:#9aa5b1;background:#1a2230;padding:1px 6px;border-radius:6px}
@media (max-width:640px){.row{font-size:13.5px}.wrap{padding-top:18px}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div style="flex:1">
      <h1>ClickBridge <span style="color:var(--dim);font-weight:400">Control Panel</span></h1>
      <div class="tag">Zwift Click → MyWhoosh via OpenBikeControl</div>
    </div>
    <span class="pill" id="pill-online">…</span>
    <span class="pill" id="pill-uptime"></span>
  </header>

  <div class="card" style="margin-bottom:14px" id="pairing-card">
    <h2>Pairing</h2>
    <div class="controls">
      <div class="switch" id="pairing-switch" role="switch" aria-checked="true" tabindex="0">
        <div class="track"><i></i></div>
        <div class="label" id="pairing-label">Automatic pairing ON
          <small id="pairing-sub">scanning for the Click + advertising to MyWhoosh</small>
        </div>
      </div>
      <button class="btn danger" id="btn-disconnect">Disconnect everything</button>
    </div>
    <div class="hint" id="pairing-hint">
      OFF stops automatic pairing on <b>both sides</b>: the bridge stops scanning for the Click and hides
      itself from MyWhoosh's discovery. A session that is running stays alive — pressing again resumes scanning.
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <h2>Zwift Click</h2>
      <div class="row"><span class="k">Connection</span><span class="v" id="click-conn">…</span></div>
      <div class="row"><span class="k">Device</span><span class="v" id="click-label">…</span></div>
      <div class="row"><span class="k">Protocol</span><span class="v" id="click-proto">…</span></div>
      <div class="row"><span class="k">Battery</span><span class="v" id="click-batt">…</span></div>
      <div class="battery" id="batt-bar"><i style="width:0%"></i></div>
      <div id="click-units" style="margin-top:10px"></div>
    </div>

    <div class="card">
      <h2>Trainer App</h2>
      <div class="row"><span class="k">Sessions</span><span class="v" id="app-count">…</span></div>
      <div class="row" style="align-items:flex-start">
        <span class="k">Connected apps</span>
        <span class="v" id="app-ids" style="max-width:60%"></span>
      </div>
      <div class="row"><span class="k">OpenBikeControl port</span><span class="v" id="obc-port">…</span></div>
      <div class="row"><span class="k">Discovery (mDNS)</span><span class="v" id="mdns">…</span></div>
    </div>

    <div class="card">
      <h2>Mapping</h2>
      <div class="map" id="mapping">…</div>
    </div>
  </div>

  <div class="card" style="margin-top:14px">
    <h2>Event journal <span style="text-transform:none;color:var(--dim)">(live, last 200)</span></h2>
    <div class="events" id="events">
      <div class="offline" id="events-empty">waiting…</div>
    </div>
  </div>

  <footer>
    This panel talks to the ClickBridge control API (<code id="ctl-url"></code>) over localhost via this Laravel app.
    The bridge keeps running without the panel — closing this page changes nothing. Config:
    <code>clickbridge config.json</code> · Console window shows the raw log.
  </footer>
</div>

<script>
const TOKEN = document.querySelector('meta[name="csrf-token"]').content;
const $ = id => document.getElementById(id);
const KIND_COLORS = {click:'ok', shift:'ok', app:'ok', battery:'warn', pairing:'warn',
                     control:'', bridge:''};

function esc(s){const d=document.createElement('div');d.textContent=String(s);return d.innerHTML;}

async function api(path, body){
  const opt = body === undefined
    ? {}
    : {method:'POST', headers:{'Content-Type':'application/json',
        'X-CSRF-TOKEN':TOKEN,'Accept':'application/json'},
       body: JSON.stringify(body)};
  const res = await fetch(path, opt);
  return res.json();
}

let online = null, pairing = null;

function setToggle(enabled){
  pairing = enabled;
  const sw = $('pairing-switch');
  sw.classList.toggle('on', enabled);
  sw.setAttribute('aria-checked', enabled);
  $('pairing-label').firstChild.textContent = enabled
    ? 'Automatic pairing ON ' : 'Automatic pairing PAUSED ';
  $('pairing-sub').textContent = enabled
    ? 'scanning for the Click + advertising to MyWhoosh'
    : 'no new connections — current session keeps running';
}

async function refresh(){
  let st;
  try { st = await api('/api/status'); }
  catch { st = {online:false}; }

  const up = st.online;
  $('pill-online').textContent = up ? 'bridge online' : 'bridge OFFLINE';
  $('pill-online').className = 'pill ' + (up ? 'on' : 'down');
  if (!online && online !== null && up) {} // (state change visuals below)
  online = up;

  if (!up){
    $('click-conn').innerHTML = '<span class="dot bad"></span>unreachable';
    $('click-label').textContent = '—';
    $('click-proto').textContent = '—';
    $('click-batt').textContent = '—';
    $('batt-bar').firstElementChild.style.width = '0%';
    $('click-units').innerHTML = '';
    $('app-count').textContent = '—';
    $('app-ids').textContent = '—';
    $('mapping').innerHTML = '<span style="color:var(--warn)">ClickBridge is not running — start it with run_clickbridge.bat (or run_panel.bat) and this panel comes back automatically.</span>';
    $('events-empty').textContent = 'no connection to the bridge';
    $('ctl-url').textContent = '';
    return;
  }
  $('ctl-url').textContent = 'http://127.0.0.1:' + st.control_port;
  if (pairing !== st.pairing_enabled) setToggle(st.pairing_enabled);

  const mm = Math.floor(st.uptime_s / 60), hh = Math.floor(mm / 60);
  $('pill-uptime').textContent = hh ? hh + 'h ' + (mm % 60) + 'm' : (mm % 60) + 'm ' + (st.uptime_s % 60) + 's';

  const click = st.click;
  $('click-conn').innerHTML = click.connected
      ? '<span class="dot ok"></span>connected' : '<span class="dot warn"></span>not connected';
  $('click-label').textContent = click.label || '—';
  $('click-proto').textContent = click.protocol || '—';
  if (click.battery === null || click.battery === undefined){
    $('click-batt').textContent = 'unknown';
    $('batt-bar').firstElementChild.style.width = '0%';
  } else {
    $('click-batt').textContent = click.battery + '%';
    $('batt-bar').classList.toggle('low', click.battery < 25);
    $('batt-bar').firstElementChild.style.width = click.battery + '%';
  }

  let unitsHtml = '';
  for (const c of (st.clicks || [])){
    const dot = c.connected ? '<span class="dot ok"></span>yes'
                            : '<span class="dot bad"></span>no';
    unitsHtml += '<div class="row"><span class="k">' + esc(String(c.address || '?').toLowerCase())
      + '</span><span class="v">' + dot + '</span></div>';
  }
  unitsHtml = '<div style="color:var(--dim);font-size:12px;margin-bottom:4px">each controller</div>' + unitsHtml;
  $('click-units').innerHTML = unitsHtml;

  $('app-count').textContent = st.apps.count;
  $('app-ids').innerHTML = (st.apps.ids && st.apps.ids.length)
    ? st.apps.ids.map(esc).join(' ') : '<span style="color:var(--dim)">none (connect via the OpenBikeControl icon in the app)</span>';
  $('obc-port').textContent = st.config.obc_port;
  $('mdns').textContent = st.mdns_advertising ? 'advertising' : 'hidden';

  const BTN_LABELS = {
    'up': 'Shifter up (+)', 'down': 'Shifter down (−)',
    'nav_up': 'D-pad up', 'nav_down': 'D-pad down',
    'nav_left': 'D-pad left', 'nav_right': 'D-pad right',
    'a': 'A button', 'b': 'B button', 'y': 'Y button', 'z': 'Z button'
  };
  function btnLabel(name){
    if (BTN_LABELS[name]) return BTN_LABELS[name];
    return name.replace(/_/g, ' ');
  }

  const m = st.config.buttons;
  let html = '';
  for (const btn in m){
    const parts = [];
    if (m[btn].press.length) parts.push('<b>' + esc(m[btn].press.join(' + ')) + '</b>');
    if (m[btn].long_press.length) parts.push('hold ≥ ' + st.config.long_press_ms + 'ms: <b>' + esc(m[btn].long_press.join(' + ')) + '</b>');
    html += '<div class="row"><span class="k">' + esc(btnLabel(btn)) + '</span><span class="v">' + parts.join(' · ') + '</span></div>';
  }
  html += '<div class="row"><span class="k">Haptics</span><span class="v">' + (st.config.haptics ? 'forwarded to Click' : 'off') + '</span></div>';
  $('mapping').innerHTML = html;

  // events, newest first
  const evs = (st.events || []).slice().reverse();
  const box = $('events');
  box.innerHTML = '';
  if (!evs.length){ box.innerHTML = '<div class="offline">no events yet</div>'; }
  for (const e of evs){
    const div = document.createElement('div');
    div.className = 'ev';
    const t = document.createElement('time'); t.textContent = e.t;
    const k = document.createElement('span');
    k.className = 'kind ' + (e.kind || '');
    k.textContent = e.kind;
    const msg = document.createElement('span'); msg.textContent = e.msg;
    div.append(t, k, msg);
    box.appendChild(div);
  }
}

async function setPairing(enabled){
  const sw = $('pairing-switch');
  sw.style.opacity = '.5';
  try { await api('/api/pairing', {enabled}); } finally {
    sw.style.opacity = '';
    await refresh();
  }
}

$('pairing-switch').addEventListener('click', () => {
  if (online === null || !online) return;
  setPairing(!pairing);
});
$('pairing-switch').addEventListener('keydown', e => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); $('pairing-switch').click(); }
});
$('btn-disconnect').addEventListener('click', async () => {
  if (!confirm('Disconnect the Click and all connected app sessions now? (Auto-pairing will re-connect afterwards.)')) return;
  const b = $('btn-disconnect'); b.disabled = true;
  try { await api('/api/disconnect', {}); } finally { b.disabled = false; await refresh(); }
});

refresh();
setInterval(refresh, 1500);
</script>
</body>
</html>
