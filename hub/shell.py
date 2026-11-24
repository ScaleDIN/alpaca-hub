"""The hub's status page: an Overview tab plus one tab per device that is present (its own page, in a frame)."""

_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Alpaca Hub</title>
<style>
:root{--bg:#0b0d10;--bar:#14181e;--card:#171b21;--line:#2a313b;--fg:#e8ecf1;--mut:#8a94a3;--acc:#4ea1ff;--ok:#3fbf6f;--warn:#ffb340;--bad:#ef5350}
@media (prefers-color-scheme:light){:root{--bg:#f4f6f8;--bar:#fff;--card:#fff;--line:#d9dee5;--fg:#14181e;--mut:#5b6573;--acc:#1f6fd1}}
:root[data-night]{--bg:#000;--bar:#0b0000;--card:#120000;--line:#3a0000;--fg:#ff3b28;--mut:#a01a0c;--acc:#ff3b28;--ok:#c0200e;--warn:#ff5a30;--bad:#ff2a10}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,-apple-system,"Segoe UI",sans-serif}
body{display:flex;flex-direction:column}
/* The bar's background spans the window; its contents keep to the device pages' column (kit main.cols: 1600 px,
   16 px padding, 12 px under 1000 px), so the name and the 🌙 line up with the page below. --sb is the pages'
   scrollbar width (reserved on every tab, see fitBar), so the column is centred in the same space as theirs. */
header{padding:6px var(--sb,0px) 6px 0;background:var(--bar);border-bottom:1px solid var(--line)}
.hb{display:flex;align-items:center;gap:6px;max-width:1600px;margin:0 auto;padding:0 16px;overflow-x:auto;scrollbar-width:none}
.hb::-webkit-scrollbar{display:none}
@media (max-width:999px){.hb{padding:0 12px}}
.brand{font-weight:700;margin-right:8px;white-space:nowrap}
.tab{border:1px solid transparent;background:none;color:var(--mut);padding:7px 12px;border-radius:8px;font:inherit;cursor:pointer;white-space:nowrap;display:flex;align-items:center;gap:6px}
.tab:hover{color:var(--fg)}.tab.dragging{opacity:.4}.tab[aria-selected=true]{color:var(--fg);background:var(--card);border-color:var(--line)}
.dot{width:8px;height:8px;border-radius:50%;background:var(--ok);flex:none}.dot.warn{background:var(--warn)}.dot.bad{background:var(--bad)}
.sp{flex:1}.icon{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:8px;padding:6px 10px;font:inherit;cursor:pointer;text-decoration:none;line-height:normal}#help{font-weight:600;min-width:2.4em;text-align:center}
main{flex:1;position:relative;min-height:0}
iframe{position:absolute;inset:0;width:100%;height:100%;border:0;background:var(--bg)}
/* The Overview scrolls as a whole (its scrollbar at the window edge, space always kept, as on the device pages) and
   holds the same column as the bar and the device pages; the device list keeps a readable width inside it. */
#ov{position:absolute;inset:0;overflow:auto;scrollbar-gutter:stable}
.ovc{max-width:1600px;margin:0 auto;padding:16px}
@media (max-width:999px){.ovc{padding:16px 12px}}   /* the same side margin as the device pages and the bar */
.ovc>.scan,#list{max-width:900px}
#ovChart{position:static;display:block;width:100%;height:470px;border:0;margin:16px 0 0;background:var(--bg)}
.dev{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:0 0 10px;display:grid;grid-template-columns:auto 1fr auto;gap:4px 12px;align-items:center;cursor:pointer}
.dev:hover{border-color:var(--acc)}.dev h3{margin:0;font-size:16px}.dev .s{color:var(--mut);font-variant-numeric:tabular-nums}
.dev .al{grid-column:2/-1;color:var(--mut);font-size:13px}
.empty{color:var(--mut);padding:30px 0;text-align:center}
.note{color:var(--mut);font-size:13px;margin-top:16px}
.scan{display:flex;align-items:center;gap:12px;margin:0 0 12px}
.dh{font-size:14px;color:var(--mut);font-weight:600;margin:22px 0 8px;max-width:900px}
.drv{max-width:900px;box-sizing:border-box;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 14px;margin:0 0 8px;display:grid;grid-template-columns:auto 1fr auto;gap:2px 12px;align-items:center}
.drv h3{margin:0;font-size:15px;grid-column:2;grid-row:1}.drv .s{color:var(--mut);font-size:13px;font-weight:400;font-variant-numeric:tabular-nums;margin-left:6px}
.drv>.dot{grid-column:1;grid-row:1}
.drv .st{grid-column:2;grid-row:2;color:var(--mut);font-size:13px}.drv .bt{grid-column:3;grid-row:1/3;display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.drv .why,.drv details{grid-column:2/-1;font-size:13px}.drv .why{color:var(--fg)}
.drv .dot.off{background:var(--mut)}
.drv button{border:1px solid var(--line);background:var(--bg);color:var(--fg);border-radius:8px;padding:4px 12px;font:inherit;font-size:13px;cursor:pointer;white-space:nowrap}
.drv button:hover:not(:disabled){border-color:var(--acc)}.drv button:disabled{opacity:.6}
.drv pre{white-space:pre-wrap;font-size:12px;max-height:240px;overflow:auto;margin:6px 0 0}
.ports{max-width:900px}.ports>summary{cursor:pointer;list-style-position:inside}.ports .s{font-weight:400;color:var(--mut);font-size:13px;margin-left:6px}
.prt{display:grid;grid-template-columns:70px 1fr auto;gap:2px 12px;align-items:center;padding:7px 4px;border-bottom:1px solid var(--line);font-size:13px}
.prt .pn{font-weight:600;font-variant-numeric:tabular-nums}.prt .pd{overflow-wrap:anywhere}.prt .ps{grid-column:2;color:var(--mut)}.prt .ps.warn{color:var(--warn,#d9a23a)}
.prt .bt{grid-column:3;grid-row:1/3}
.prt button{border:1px solid var(--line);background:var(--bg);color:var(--fg);border-radius:8px;padding:4px 12px;font:inherit;font-size:13px;cursor:pointer;white-space:nowrap}
.prt button:hover:not(:disabled){border-color:var(--acc)}
@media (max-width:600px){.drv{grid-template-columns:auto 1fr}.drv .bt{grid-column:1/-1;grid-row:auto;justify-content:flex-start;margin-top:6px}.drv .why,.drv details{grid-column:1/-1}}
.upd{max-width:900px;background:var(--card);border:1px solid var(--acc);border-radius:12px;padding:10px 14px;margin:0 0 12px}
.upd>summary{cursor:pointer;font-weight:600}.upd .s{color:var(--mut);font-size:13px;font-weight:400;margin-left:6px}
.upd .it{margin:10px 0 0;font-size:14px}.upd .it b{font-weight:600}.upd .why{color:var(--warn);font-size:13px}
.upd button{border:1px solid var(--line);background:var(--bg);color:var(--fg);border-radius:8px;padding:2px 10px;font:inherit;font-size:13px;cursor:pointer;margin-left:6px}
.upd button:hover:not(:disabled){border-color:var(--acc)}.upd button:disabled{opacity:.6}
.upd a{color:var(--acc);white-space:nowrap;font-size:13px;margin-left:6px}
.upd pre{white-space:pre-wrap;font:inherit;font-size:13px;color:var(--mut);margin:4px 0 0}
.uset{max-width:900px;display:flex;align-items:center;gap:8px 12px;flex-wrap:wrap;font-size:14px}
.uset label{display:flex;align-items:center;gap:6px;cursor:pointer}.uset .s{color:var(--mut);font-size:13px}
.uset button{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:8px;padding:4px 12px;font:inherit;font-size:13px;cursor:pointer}
.uset button:hover:not(:disabled){border-color:var(--acc)}.uset button:disabled{opacity:.6}
.scan button{border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:8px;padding:7px 14px;font:inherit;cursor:pointer;white-space:nowrap}
.scan button:hover:not(:disabled){border-color:var(--acc)}.scan button:disabled{opacity:.6;cursor:default}
/* two lines reserved, so a longer message never moves the list below */
.scan .msg{color:var(--mut);font-size:13px;line-height:1.4;min-height:2.8em;display:flex;align-items:center}
</style></head><body>
<header><div class="hb"><span class="brand">Alpaca Hub</span><nav id="tabs" style="display:flex;gap:4px"></nav><span class="sp"></span>
<a class="icon" id="help" href="/hub/help" target="_blank" rel="noopener" title="User guide" aria-label="User guide">?</a>
<button class="icon" id="night" title="Red night mode">🌙</button></div></header>
<main><div id="ov"><div class="ovc"><div class="scan"><button id="scanBtn" title="Look at the serial ports now instead of waiting for the next automatic pass (every 15 s). Also tries again ports nothing was recognised on">Scan now</button>
<span class="msg" id="scanMsg" aria-live="polite"></span></div><div id="list"></div>
<iframe id="ovChart" title="All devices" scrolling="no"></iframe></div></div></main>
<script>
const $ = (i) => document.getElementById(i);
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
let hub = null, cur = (location.hash || '#overview').slice(1), frames = {};
// One night-mode switch for the whole hub. Each device page (also usable on its own, as the standalone services
// are) has its own 🌙 and remembers its own setting. In the hub, this toggle drives every tab instead: the pages
// come from the same address, so the shell switches them through their kit (K.setNight, which also redraws charts)
// and hides their own button.
const isNight = () => document.documentElement.hasAttribute('data-night');
function frameNight(f, gutter = true) {
  try {
    const w = f.contentWindow, d = f.contentDocument;
    if (!d || !d.head) return;
    if (!d.getElementById('hub-night')) {
      // also: a tab page always keeps the scrollbar's space, so every page's column (and the bar above, fitBar) stays
      // put. Not the Overview's chart, which never scrolls on its own.
      const s = d.createElement('style'); s.id = 'hub-night'; s.textContent = '#night{display:none!important}' + (gutter ? 'html{scrollbar-gutter:stable}' : ''); d.head.appendChild(s);
    }
    const on = isNight();
    if (w.K && w.K.setNight) w.K.setNight(on);
    else if (on) d.documentElement.setAttribute('data-night', ''); else d.documentElement.removeAttribute('data-night');
  } catch (e) {}
}
// The browser's scrollbar width (0 with overlay scrollbars), which every page reserves (frameNight), so the bar's
// column is centred where theirs is. Measured on a probe, so it's right before any page has loaded.
function fitBar() {
  const p = document.createElement('div');
  p.style.cssText = 'position:absolute;top:-999px;width:100px;height:50px;overflow:scroll';
  document.body.appendChild(p);
  document.documentElement.style.setProperty('--sb', Math.max(0, p.offsetWidth - p.clientWidth) + 'px');
  p.remove();
}
fitBar();
addEventListener('resize', fitBar);           // page zoom changes the scrollbar's width in CSS pixels
function night(on) {
  if (on) document.documentElement.setAttribute('data-night', ''); else document.documentElement.removeAttribute('data-night');
  try { localStorage.setItem('hubNight', on ? '1' : ''); } catch (e) {}
  for (const f of Object.values(frames)) frameNight(f);
  frameNight($('ovChart'), false);
}
$('night').onclick = () => night(!isNight());
try { if (localStorage.getItem('hubNight')) night(true); } catch (e) {}
// The Overview's chart of every device (/hub/chart/, a kit page), with the saved history files below it: shown
// with no device too (the files stay downloadable and plottable). Its frame takes the page's height, so the
// Overview scrolls as one; the page reloads itself when the devices change, and this follows its new height.
(function chartFrame() {
  const f = $('ovChart');
  f.onload = () => {
    frameNight(f, false);
    try {
      const d = f.contentDocument, fit = () => { f.style.height = d.body.offsetHeight + 'px'; };
      new f.contentWindow.ResizeObserver(fit).observe(d.body);   // the frame's own: this window's never fired for it
      fit();
    } catch (e) {}
  };
  f.src = '/hub/chart/';
})();
function show(id) {
  cur = id; history.replaceState(null, '', '#' + id);
  document.querySelectorAll('.tab').forEach((t) => t.setAttribute('aria-selected', t.dataset.id == id));
  $('ov').style.display = id == 'overview' ? '' : 'none';
  for (const [s, f] of Object.entries(frames)) f.style.display = s == id ? '' : 'none';
  if (id != 'overview' && !frames[id] && hub && hub.devices.some((d) => d.slug == id)) {
    const f = document.createElement('iframe'); f.src = '/' + id + '/'; f.title = id; frames[id] = f;
    f.onload = () => frameNight(f);          // after the page's own start-up, which applied its own saved setting
    document.querySelector('main').appendChild(f);
  }
}
// ---- The tabs: Overview, then the devices in the hub's order (Hub.ordered_instances). Device tabs can be dragged
// like a browser's, or moved with Ctrl+Shift+←/→; the new order is saved in the hub, for every browser, until
// Overview's Reset order.
let dragging = null;                 // the tab being dragged
function renderTabs() {
  if (dragging) return;              // rebuilt after the drop
  const tabs = [['overview', 'Overview', null]].concat(hub.devices.map((d) => [d.slug, d.title, d.up ? d.summary.state : 'bad']));
  const html = tabs.map(([id, t, st]) => `<button class="tab" data-id="${id}" role="tab"${st ? ' draggable="true" title="Drag to move this tab"' : ''}>${st ? `<span class="dot ${st == 'ok' ? '' : st}"></span>` : ''}${esc(t)}</button>`).join('');
  const nav = $('tabs');
  if (nav.dataset.html == html) return;        // unchanged: keep the buttons (and their focus)
  const focused = document.activeElement && document.activeElement.classList.contains('tab') ? document.activeElement.dataset.id : null;
  nav.innerHTML = html; nav.dataset.html = html;
  nav.querySelectorAll('.tab').forEach((b) => {
    b.onclick = () => show(b.dataset.id);
    b.setAttribute('aria-selected', b.dataset.id == cur);
    if (b.dataset.id == 'overview') return;
    b.ondragstart = (e) => { dragging = b; b.classList.add('dragging'); e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', b.dataset.id); };
    b.ondragend = () => { b.classList.remove('dragging'); dragging = null; saveTabOrder(); };
    b.onkeydown = (e) => {
      if (!e.ctrlKey || !e.shiftKey || (e.key != 'ArrowLeft' && e.key != 'ArrowRight')) return;
      e.preventDefault();
      const to = e.key == 'ArrowLeft' ? b.previousElementSibling : b.nextElementSibling;
      if (!to || to.dataset.id == 'overview') return;
      to.insertAdjacentElement(e.key == 'ArrowLeft' ? 'beforebegin' : 'afterend', b);
      b.focus(); saveTabOrder();
    };
  });
  if (focused) { const f = nav.querySelector(`.tab[data-id="${focused}"]`); if (f) f.focus(); }
}
// While dragging, the tab moves among the others as the pointer passes their middles (Overview stays first).
$('tabs').ondragover = (e) => {
  if (!dragging) return;
  e.preventDefault(); e.dataTransfer.dropEffect = 'move';
  const over = e.target.closest && e.target.closest('.tab');
  if (!over || over == dragging || over.dataset.id == 'overview') return;
  const r = over.getBoundingClientRect(), after = e.clientX > r.left + r.width / 2;
  if (after ? over.nextElementSibling != dragging : over.previousElementSibling != dragging) over.insertAdjacentElement(after ? 'afterend' : 'beforebegin', dragging);
};
$('tabs').ondrop = (e) => { if (dragging) e.preventDefault(); };
async function saveTabOrder() {
  const order = [...$('tabs').querySelectorAll('.tab')].map((b) => b.dataset.id).filter((id) => id != 'overview');
  if (!hub || order.join() == hub.devices.map((d) => d.slug).join()) { renderTabs(); return; }
  hub.devices.sort((a, b) => order.indexOf(a.slug) - order.indexOf(b.slug));   // at once, before the hub's answer
  hub.tab_order_saved = true;
  render();
  await fetch('hub/tab-order', { method: 'POST', headers: { 'X-Hub-Page': '1', 'Content-Type': 'application/x-www-form-urlencoded' },
    body: 'order=' + encodeURIComponent(order.join(',')) }).catch(() => null);
  tick();
}
function render() {
  const devs = hub.devices;
  renderTabs();
  for (const s of Object.keys(frames)) if (!devs.some((d) => d.slug == s)) { frames[s].remove(); delete frames[s]; }
  if (cur != 'overview' && !devs.some((d) => d.slug == cur)) cur = 'overview';
  const al = (slug) => hub.alpaca.filter((a) => a.slug == slug).map((a) => `${a.type} #${a.number}`).join(' · ');
  $('list').innerHTML = updatesLine() + (devs.length ? devs.map((d) => `<div class="dev" data-id="${d.slug}"><span class="dot ${d.up ? (d.summary.state == 'ok' ? '' : d.summary.state) : 'bad'}"></span>
    <h3>${esc(d.title)}</h3><span class="s">${esc(d.port)}${d.missing ? ' · unplugged?' : ''}</span>
    <span class="al">${esc(d.up ? d.summary.text : 'not answering')}${al(d.slug) ? ' — Alpaca ' + esc(al(d.slug)) : ''}</span></div>`).join('')
      + (hub.tab_order_saved ? '<div class="uset" style="margin:0 0 10px"><span class="s">The tabs are in the order you dragged them.</span><button id="tabReset">Reset order</button></div>' : '')
    : !(hub.drivers || []).length ? '<p class="empty">No drivers installed yet. Install the driver for each of your devices (AlpacaHub-&lt;device&gt;-Setup.exe): it appears below within seconds, and its device here on the next scan.</p>'
    : hub.last_scan && !(hub.ports || []).length ? '<p class="empty">No serial ports on this PC. Is the device plugged in by USB, and powered? It appears here within about 15 s.</p>'
    : '<p class="empty">Looking for devices on the serial ports… Plug one in and it appears here within about 15 s.</p>')
    + (hub.stuck || []).map((p) => `<p class="note">⚠ ${esc(p)} is stuck inside Windows' serial driver. The hub tries it again when the driver lets go; if that takes more than a few minutes, unplug that device's USB cable and plug it back in.</p>`).join('')
    + driversList()
    + portsList()
    + updatesSetting()
    + Object.entries(hub.refused || {}).map(([k, why]) => `<p class="note">⚠ Driver ${esc(k)} is not used: ${esc(why)}.</p>`).join('')
    + `<p class="note">${esc(hub.name)} ${esc(hub.version)}${hub.simulated ? ' · SIMULATED devices' : ''}. Alpaca programs find every device here at this one address.</p>`;
  document.querySelectorAll('.dev').forEach((e) => { e.onclick = () => show(e.dataset.id); });
  document.querySelectorAll('.drv button').forEach((b) => { b.onclick = async () => {
    const p = (hub.drivers || []).find((x) => x.name == b.dataset.name);
    if (b.dataset.act == 'off' && p && p.devices.length && !confirm(`Turn off ${p.title}? Its devices (${p.devices.join(', ')}) disconnect until you turn it on again.`)) return;
    if (b.dataset.act == 'switch' && !confirm(`Switch ${p.title} to version ${p.update} now? Connected programs lose its devices for a few seconds.`)) return;
    b.disabled = true;
    await fetch('hub/drivers/' + encodeURIComponent(b.dataset.name) + '/' + b.dataset.act, { method: 'POST', headers: { 'X-Hub-Page': '1' } }).catch(() => null);
    tick();
  }; });
  document.querySelectorAll('.drv details').forEach((d) => { d.ontoggle = () => { if (d.open) openLogs.add(d.dataset.name); else openLogs.delete(d.dataset.name); }; });
  const tr = $('tabReset'); if (tr) tr.onclick = async () => {
    tr.disabled = true;
    await fetch('hub/tab-order', { method: 'POST', headers: { 'X-Hub-Page': '1', 'Content-Type': 'application/x-www-form-urlencoded' }, body: 'reset=1' }).catch(() => null);
    tick();
  };
  const pd = $('ports'); if (pd) pd.ontoggle = () => { portsOpen = pd.open; };
  const ud = $('updLine'); if (ud) ud.ontoggle = () => { updOpen = ud.open; };
  const uc = $('updOn'); if (uc) uc.onchange = () => updPost('hub/updates', 'on=' + (uc.checked ? '1' : '0'));
  const un = $('updNow'); if (un) un.onclick = () => { un.disabled = true; updPost('hub/updates/check', ''); };
  document.querySelectorAll('.uins').forEach((b) => { b.onclick = () => installUpdate(b); });
  document.querySelectorAll('.prt button').forEach((b) => { b.onclick = async () => {
    if (b.dataset.act == 'ignore' && !confirm(`Ignore ${b.dataset.dev}? The hub won't open or probe it again, even after a restart, until you stop ignoring it here.`)) return;
    b.disabled = true;
    const body = b.dataset.act == 'ignore' ? { device: b.dataset.dev } : { key: b.dataset.key };
    const r = await fetch('hub/ports/' + b.dataset.act, { method: 'POST', headers: { 'X-Hub-Page': '1', 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams(body).toString() }).catch(() => null);
    if (r && !r.ok) { try { alert((await r.json()).error); } catch (e) {} }
    tick();
  }; });
  show(cur);
}
// Overview's Serial ports list: every port the hub saw and what it made of it; Ignore keeps the hub away from one
// (a meter's cable, a device another program owns): never opened, never probed, remembered across restarts.
let portsOpen = false;
const PORT_STATE = { 'in use': 'in use', 'ignored': 'ignored', 'ignored, not plugged in': 'ignored · not plugged in', 'stuck': 'stuck in Windows\' serial driver',
  'no driver': 'no installed driver uses its USB chip: never opened', 'not recognised': 'no driver recognised it', 'not looked at yet': 'not looked at yet' };
function portsList() {
  const ps = hub.ports || [];
  if (!ps.length) return '';
  const ign = ps.filter((p) => p.key).length;
  return `<details class="ports" id="ports"${portsOpen ? ' open' : ''}><summary class="dh">Serial ports <span class="s">${ps.length}${ign ? ' · ' + ign + ' ignored' : ''}</span></summary>`
    + ps.map((p) => {
      const ids = [p.usb, p.serial].filter(Boolean).join(' · ');
      const what = p.state == 'in use' ? `in use: ${esc((hub.devices.find((d) => d.slug == p.slug) || {}).title || p.slug)}` : (PORT_STATE[p.state] || p.state);
      const canIgnore = ['not recognised', 'stuck', 'not looked at yet'].includes(p.state);
      const btn = p.key ? `<button data-act="unignore" data-key="${esc(p.key)}">Stop ignoring</button>`
        : canIgnore ? `<button data-act="ignore" data-dev="${esc(p.device)}">Ignore</button>` : '';
      return `<div class="prt"><span class="pn">${esc(p.device)}</span><span class="pd">${esc(p.description)}${ids ? ` <span class="s">${esc(ids)}</span>` : ''}</span>`
        + `<span class="ps ${p.state == 'stuck' ? 'warn' : ''}">${esc(what)}</span><span class="bt">${btn}</span></div>`;
    }).join('')
    + `<p class="note" style="margin-top:6px">Ignore a port the hub should leave alone, for example a meter's cable or a device another program uses. One with a USB serial number stays ignored on any COM port.</p></details>`;
}

// Updates (hub/updates.py): a line at the top when the published list has a newer hub or a newer version of a
// driver installed here, with what's new; the setting and the last check at the bottom.
let updOpen = false;
const when = (t) => t ? new Date(t * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : '';
const UPD_BUSY = ['downloading', 'checking', 'starting'];
function installState(x, st) {
  if (!st || st.version != x.version) return '';
  const t = st.state == 'downloading' ? `Downloading… ${st.total ? Math.floor(100 * st.done / st.total) : 0} %`
    : st.state == 'checking' ? 'Checking the download…'
    : st.state == 'starting' ? 'Starting the installer: answer Windows\' administrator prompt.'
    : st.state == 'started' ? (x.kind == 'hub' ? 'The installer is running. The hub stops, updates and starts again; this page reloads when it answers.'
      : 'The installer is running. The driver switches to the new version when it has finished (when no program is connected to its devices).')
    : st.state == 'failed' ? `Not installed: ${st.error}.` : '';
  return t ? `<div class="${st.state == 'failed' ? 'why' : 's'}">${esc(t)}</div>` : '';
}
function updatesLine() {
  const u = hub.updates;
  if (!u || !u.enabled || !(u.available || []).length) return '';
  const a = u.available, ins = u.installs || {}, busy = Object.values(ins).some((s) => UPD_BUSY.includes(s.state));
  return `<details class="upd" id="updLine"${updOpen ? ' open' : ''}><summary>Updates: ${esc(a.map((x) => x.title + ' ' + x.version).join(', '))}<span class="s">What's new</span></summary>`
    + a.map((x) => {
      const st = ins[x.key] && ins[x.key].version == x.version ? ins[x.key] : null;
      const can = hub.local && !x.blocked && x.file && !(st && (UPD_BUSY.includes(st.state) || st.state == 'started'));
      return `<div class="it"><b>${esc(x.title)} ${esc(x.version)}</b> <span class="s">${esc(x.installed)} here${x.date ? ' · released ' + esc(x.date) : ''}</span>`
        + (can ? ` <button class="uins" data-key="${esc(x.key)}"${busy ? ' disabled' : ''}>${st && st.state == 'failed' ? 'Try again' : 'Install'}</button>` : '')
        + (/^https:\/\//.test(x.url || '') && !x.blocked ? ` <a href="${esc(x.url)}" rel="noopener">Download</a>` : '')
        + (x.blocked ? `<div class="why">${esc(x.blocked[0].toUpperCase() + x.blocked.slice(1))}.</div>` : '')
        + installState(x, st)
        + (x.notes ? `<pre>${esc(x.notes)}</pre>` : '') + '</div>';
    }).join('')
    + `<p class="note" style="margin:10px 0 0">${a.some((x) => x.kind == 'hub') && a.length > 1 ? 'Install the hub first, then the drivers. ' : ''}`
    + (hub.local ? 'Install downloads the installer, checks it against the published list and runs it here; Windows asks for administrator rights. A driver switches over by itself once no program uses its devices.'
      : 'Install on the hub\'s PC: open this page there, or download each installer and run it there.') + '</p></details>';
}
async function installUpdate(b) {
  const x = ((hub.updates || {}).available || []).find((y) => y.key == b.dataset.key);
  if (!x) return;
  const msg = x.kind == 'hub'
    ? `Install ${x.title} ${x.version}? The hub stops, updates and starts again: ${x.in_use ? 'the Alpaca programs connected now lose' : 'Alpaca programs lose'} their devices for about a minute.`
    : `Install ${x.title} ${x.version}?` + (x.in_use ? ' An Alpaca program is connected to its devices: the new version takes over when it disconnects (or with Switch now under Drivers).' : '');
  if (!confirm(msg)) return;
  b.disabled = true; updOpen = true;
  const r = await fetch('hub/updates/install', { method: 'POST', headers: { 'X-Hub-Page': '1', 'Content-Type': 'application/x-www-form-urlencoded' },
    body: 'key=' + encodeURIComponent(x.key) }).catch(() => null);
  if (r && !r.ok) { try { alert((await r.json()).error || r.statusText); } catch (e) { alert(await r.text()); } }
  tick();
}
function updatesSetting() {
  const u = hub.updates;
  if (!u) return '';
  let st;
  if (!u.address) st = 'No update address in this version of the hub yet.';
  else if (!u.enabled) st = u.on ? 'Not in a simulated hub.' : 'Off: the hub sends nothing.';
  else if (u.checking) st = 'Checking…';
  else if (!u.last_check) st = 'Not checked yet: the first check is about a minute after the hub starts, then once a day.';
  else st = `Checked ${when(u.last_check)}` + (u.error ? `: couldn't read the list (${u.error})` + (u.last_ok ? `. Last read ${when(u.last_ok)}.` : '.')
    : (u.available || []).length ? '.' : ': up to date.');
  return '<h2 class="dh">Updates</h2><div class="uset">'
    + `<label title="Once a day the hub reads the list of published versions. It sends only its own version number."><input type="checkbox" id="updOn"${u.on ? ' checked' : ''}${!u.address || (hub.simulated && u.on && !u.enabled) ? ' disabled' : ''}> Check for updates online</label>`
    + (u.enabled && u.address ? `<button id="updNow"${u.checking ? ' disabled' : ''}>Check now</button>` : '')
    + `<span class="s">${esc(st)}</span></div>`;
}
async function updPost(path, body) {
  await fetch(path, { method: 'POST', headers: { 'X-Hub-Page': '1', 'Content-Type': 'application/x-www-form-urlencoded' }, body }).catch(() => null);
  tick(); setTimeout(tick, 2000);
}

// Overview's Drivers list: each driver's version, where it came from, its state, and its buttons.
const openLogs = new Set();
const STATE = { running: 'running', starting: 'starting', restarting: 'restarting', updating: 'updating', failed: 'failed', off: 'off', refused: 'refused', stopped: 'stopped' };
function driversList() {
  const ds = hub.drivers || [];
  if (!ds.length) return '';
  return '<h2 class="dh">Drivers</h2>' + ds.map((p) => {
    const dot = p.state == 'running' ? (p.update ? 'warn' : '') : (p.state == 'off' ? 'off' : p.state == 'failed' || p.state == 'refused' ? 'bad' : 'warn');
    const parts = [STATE[p.state] || p.state];
    if (p.state == 'running') parts.push(p.devices.length ? `${p.devices.length} device${p.devices.length > 1 ? 's' : ''}` : 'no device found');
    if (p.restarts_1h) parts.push(`restarted ${p.restarts_1h}× in the last hour`);
    const why = p.why && p.state != 'running' ? `<span class="why">${esc(p.why)}</span>` : '';
    const upd = p.update ? `<span class="why">Version ${esc(p.update)} is installed. It takes over when no program is connected to ${p.devices.length > 1 ? 'these devices' : 'this device'}.</span>` : '';
    const btn = (act, label) => `<button data-name="${esc(p.name)}" data-act="${act}">${label}</button>`;
    const buttons = (p.update ? btn('switch', 'Switch now') : '') + (p.state == 'failed' ? btn('retry', 'Try again') : '')
      + (p.state == 'refused' ? '' : p.enabled ? btn('off', 'Turn off') : btn('on', 'Turn on'));
    const logs = p.last_lines && p.last_lines.length ? `<details data-name="${esc(p.name)}"${openLogs.has(p.name) ? ' open' : ''}><summary>Its last log lines</summary><pre>${esc(p.last_lines.join('\n'))}</pre></details>` : '';
    return `<div class="drv"><span class="dot ${dot}"></span><h3>${esc(p.title || p.name)} <span class="s">${esc(p.version)} · ${esc(p.source)}</span></h3>
      <span class="bt">${buttons}</span><span class="st">${esc(parts.join(' · '))}</span>${why}${upd}${logs}</div>`;
  }).join('');
}
// The hub's version when this page loaded: a hub updated from here (or any other way) has a new page, so reload.
let pageVersion = null;
async function tick() {
  try {
    hub = await (await fetch('hub.json', { cache: 'no-store' })).json();
    if (pageVersion && hub.version != pageVersion) { location.reload(); return; }
    pageVersion = hub.version;
    render();
  } catch (e) {}
  scanState();
  // while an installer downloads or starts, follow it every second
  const ins = (hub && hub.updates && hub.updates.installs) || {};
  if (!fastTick && Object.values(ins).some((s) => UPD_BUSY.includes(s.state))) fastTick = setTimeout(() => { fastTick = null; tick(); }, 1000);
}
let fastTick = null;

// ---- Scan now: ask for a pass, then follow it in hub.json until a pass that started after the click has finished
let scan = null;                     // {t: the hub's time of the request, before: slugs present at the click}
function scanState() {
  if (!hub) return;
  const auto = `Plug a device in and it appears within about ${Math.round(hub.scan_every || 15)} s, or scan now.`;
  if (!scan) { $('scanBtn').disabled = false; if (!$('scanMsg').dataset.keep) $('scanMsg').textContent = auto; return; }
  if (scan.t == null || hub.scanning || hub.scan_started < scan.t || hub.last_scan < hub.scan_started) { $('scanBtn').disabled = true; $('scanMsg').textContent = 'Scanning the serial ports…'; return; }
  const added = hub.devices.filter((d) => !scan.before.includes(d.slug)).map((d) => d.title);
  $('scanMsg').textContent = added.length ? `Found ${added.join(', ')}.`
    : 'Nothing new found. A device that is still starting up, or whose port another program has open, shows up on a later pass.';
  $('scanMsg').dataset.keep = '1';
  setTimeout(() => { delete $('scanMsg').dataset.keep; scanState(); }, 15000);
  scan = null;
  $('scanBtn').disabled = false;
}
$('scanBtn').onclick = async () => {
  $('scanBtn').disabled = true; delete $('scanMsg').dataset.keep;
  $('scanMsg').textContent = 'Scanning the serial ports…';
  scan = { t: null, before: hub ? hub.devices.map((d) => d.slug) : [] };
  const r = await fetch('hub/rescan', { method: 'POST', headers: { 'X-Hub-Page': '1' } }).catch(() => null);
  if (r && r.ok) scan.t = (await r.json()).requested;      // a pass that started after this has seen the new ports
  if (!r || !r.ok) { scan = null; $('scanBtn').disabled = false; $('scanMsg').textContent = 'The hub did not take the request' + (r ? ': ' + await r.text() : '.'); $('scanMsg').dataset.keep = '1'; return; }
  const poll = setInterval(async () => { await tick(); if (!scan) clearInterval(poll); }, 1000);
};
window.addEventListener('hashchange', () => { const id = location.hash.slice(1) || 'overview'; if (id != cur && hub) show(id); });
tick(); setInterval(tick, 5000);
</script></body></html>
"""


def shell_page():
    return _PAGE
