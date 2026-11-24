// NiteCrawler Alpaca status page (device-web-ui kit). Endpoints: see ncalpaca/alpaca.py (_route, do_POST).
K.init({ storePrefix: 'nca-' });
const $ = K.$, f = K.f, esc = K.esc;
let last = null, unlocked = false, busy = false;
const post = (url, data) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-NCAlpaca': '1' },
  body: new URLSearchParams(data || {}).toString() });
const row = (k, v) => `<tr><td class="mut">${k}</td><td>${v}</td></tr>`;
const steps = (n) => (n == null ? '—' : Number(n).toLocaleString('en-US').replace(/,/g, ' '));
const deg = (v) => (v == null ? '—' : f(v, 2, '°'));
const moving = (b) => (b ? '<span class="warn">moving</span>' : 'still');

// The hub's Alpaca numbers for this NiteCrawler's devices (Focuser #2 …): the hub counts across every device, so they
// aren't always 0 and 1. They come from the hub's hub.json ("alpaca": type, number, name, slug), read every 30 s;
// until then, or without a hub, a label has no number.
let nums = null, numsAt = 0;
const mySlug = location.pathname.split('/').filter(Boolean)[0] || '';
async function hubNumbers() {
  if (Date.now() - numsAt < 30000) return;
  numsAt = Date.now();
  try {
    const h = await K.getJSON('../hub.json');
    nums = {};
    for (const r of h.alpaca || []) if (r.slug == mySlug) nums[r.type + '|' + r.name] = r.number;
  } catch (e) {}
}
const alpacaName = (type, name) => {
  const n = nums ? nums[type + '|' + name] : null, t = type[0].toUpperCase() + type.slice(1);
  return n == null ? t : `${t} #${n}`;
};

// ---------------------------------------------------------------- live status
async function tick() {
  let s;
  hubNumbers();
  try { s = await K.getJSON('status.json'); }
  catch (e) { $('sub').textContent = 'connection lost'; K.pill('link', 'service not running', 'bad'); K.pill('client', '…'); return; }
  last = s;
  K.versionWatch(s.build);
  const shown = (s.device_name_set || s.device_name_default) + (s.simulated ? ' (simulated)' : '');
  $('name').textContent = shown;
  if (document.title !== shown) document.title = shown;
  $('sub').textContent = `${s.model ? 'Moonlite ' + s.model : 'NiteCrawler'}${s.firmware ? ' · firmware ' + s.firmware : ''} · driver ${s.build} · ${s.port || 'searching…'}`;
  K.pill('link', s.homing ? 'homing' : s.up ? 'link up' : s.port ? 'not responding' : 'searching', s.homing ? 'warn' : s.up ? 'ok' : 'bad');
  K.pill('client', s.client ? 'Alpaca client connected' : 'no Alpaca client', s.client ? 'ok' : '');
  $('rNote').style.display = s.local ? 'none' : '';
  settingsCards(s);
  ctl(s);
  phone(s);
  ncTable(s);
  $('aNote').textContent = `Port ${s.http_port}. Alpaca has no login: anyone on this network can move the motors. Settings: ${s.settings_file}`;
  $('devt').innerHTML = row(alpacaName('focuser', s.device_name + ' focuser'), esc(s.device_name + ' focuser') + ' — focus motor')
    + row(alpacaName('rotator', s.device_name + ' rotator'), esc(s.device_name + ' rotator'))
    + (s.settings.aux_enabled ? row(alpacaName('focuser', s.device_name + ' aux'), esc(s.device_name + ' aux') + ' — aux motor') : '');

  if (!s.up || !s.focus) {
    $('tiles').innerHTML = K.tile('NiteCrawler', '<span class="bad">no link</span>', esc(s.error || 'no answer on the serial port'));
    pinFill([['st', 0, '<span class="bad">NiteCrawler not responding</span>']]);
    return;
  }
  const L = s.limits, r = s.rotator;
  const lim = [L.in && 'IN limit', L.out && 'OUT limit', L.rot_home && 'rotator home'].filter(Boolean);
  $('tiles').innerHTML = [
    K.tile('Focus', steps(s.focus.position), s.focus.moving ? `moving to ${steps(s.focus.target)}` : `of ${steps(s.settings.focus_max)} · still`, s.focus.moving ? 'warn' : ''),
    K.tile('Rotator', deg(r.sky), `${r.moving ? 'moving · ' : ''}mechanical ${deg(r.mechanical)}${s.settings.rot_reverse ? ' · reversed' : ''}`, r.moving ? 'warn' : ''),
    s.settings.aux_enabled ? K.tile('Aux', steps(s.aux.position), s.aux.moving ? 'moving' : 'still', s.aux.moving ? 'warn' : '') : '',
    K.tile('Temperature', s.temperature == null ? '—' : f(s.temperature, 1, ' °C'), 'NiteCrawler probe'),
    K.tile('Supply', s.voltage == null ? '—' : f(s.voltage, 1, ' V'), s.voltage != null && s.voltage < 11 ? 'low: motors may stall' : 'input voltage', s.voltage != null && s.voltage < 11 ? 'warn' : ''),
    K.tile('Switches', lim.length ? esc(lim.join(' · ')) : '<span class="mut">none</span>', s.homing ? 'homing…' : 'limit and home switches', L.in || L.out ? 'warn' : ''),
  ].join('');
  pinFill([
    ['f', 0, `focus <b>${steps(s.focus.position)}</b>`, s.focus.moving ? 'warn' : ''],
    ['r', 0, `rot <b>${deg(r.sky)}</b>`, r.moving ? 'warn' : ''],
    s.settings.aux_enabled && ['a', 1, `aux <b>${steps(s.aux.position)}</b>`],
    ['t', 0, `<b>${s.temperature == null ? '—' : f(s.temperature, 1, ' °C')}</b>`],
    ['v', 1, `${s.voltage == null ? '—' : f(s.voltage, 1, ' V')}`],
    ['h', 1, s.homing ? '<span class="warn">homing</span>' : lim.length ? `<span class="warn">${esc(lim.join(', '))}</span>` : ''],
  ]);
}

// Pinned bar fields at fixed widths (their worst case, measured once off screen in the bar's own font).
const PIN_WORST = { st: 'NiteCrawler not responding', f: 'focus <b>-99 999 999</b>', r: 'rot <b>359.99°</b>', a: 'aux <b>-99 999 999</b>',
  t: '<b>-55.0 °C</b>', v: '20.0 V', h: 'IN limit, OUT limit, rotator home' };
let pinW = null;
function pinWidths() {
  const m = document.createElement('div');
  m.className = 'pinbar';
  m.style.cssText = 'position:absolute;left:-9999px;top:0;visibility:hidden;max-width:none;display:block';
  m.innerHTML = Object.entries(PIN_WORST).map(([k, h]) => `<span data-k="${k}">${h}</span>`).join('');
  document.body.append(m);
  const w = {};
  for (const el of m.children) w[el.dataset.k] = Math.ceil(el.getBoundingClientRect().width) + 1;
  m.remove();
  return w;
}
function pinFill(fields) {
  pinW = pinW || pinWidths();
  $('pinbar').innerHTML = fields.filter(Boolean).map(([k, w, h, c]) =>
    `<span class="${[w ? 'w' : '', c || ''].join(' ').trim()}" style="width:${pinW[k]}px">${h}</span>`).join('');
}

// ---------------------------------------------------------------- controls
// Locked while an Alpaca client is connected; Unlock (with a confirmation) lasts until that client disconnects or
// the page reloads. The service checks the same thing (override=1, else 409). Stop is never locked.
const FORMS = { focus: $('fFocus'), rotator: $('fRot'), aux: $('fAux') };
const ED = Object.fromEntries(Object.entries(FORMS).map(([k, fm]) => [k, K.edits(fm)]));
function ctl(s) {
  if (!s.client) unlocked = false;
  const up = !!(s.up && s.focus), locked = s.client && !unlocked;
  const off = locked || !up || busy || s.homing;
  $('cLock').classList.toggle('on', locked);
  $('cLockTxt').textContent = !s.client ? 'No Alpaca client connected: the controls are free.'
    : unlocked ? 'Unlocked while an Alpaca program is connected.' : 'An Alpaca program is connected. Move things there, or unlock.';
  $('cUnlock').textContent = unlocked ? 'Lock' : 'Unlock';
  $('cUnlock').style.visibility = s.client ? '' : 'hidden';
  $('auxNote').textContent = up ? `at ${steps(s.aux.position)}${s.settings.aux_enabled ? ' · also ' + alpacaName('focuser', s.device_name + ' aux') + ' in Alpaca' : ''}` : '';
  if (up) {
    ED.focus.fill({ v: s.focus.target != null ? s.focus.target : s.focus.position });
    ED.rotator.fill({ v: (s.rotator.target != null ? s.rotator.target : s.rotator.sky).toFixed(2) });
    ED.aux.fill({ v: s.aux.target != null ? s.aux.target : s.aux.position });
    if (!$('rCount').dataset.edited) $('rCount').value = s.rotator.position;
    dial(s);
  }
  document.querySelectorAll('#ctl form input, #ctl form button:not(.halt), #home').forEach((x) => { x.disabled = off; });
  document.querySelectorAll('#ctl .halt, #stopAll').forEach((x) => { x.disabled = !up; });
  const m = $('cMsg');
  if (!up) { m.textContent = 'No link to the NiteCrawler: the controls come back when it answers.'; m.dataset.auto = '1'; }
  else if (s.homing) { m.textContent = 'Homing… the NiteCrawler answers nothing else until it has finished. Stop all motors still works.'; m.dataset.auto = '1'; }
  else if (m.dataset.auto) { m.textContent = ''; delete m.dataset.auto; }
}
async function move(data, label) {
  busy = true; if (last) ctl(last);
  $('cMsg').textContent = label + '…';
  const r = await post('control/move', Object.assign({ override: unlocked ? 1 : 0 }, data)).catch(() => null);
  $('cMsg').textContent = r ? (r.ok ? `${label}: ${await r.text()}.` : `${label}: not done. ${await r.text()}`) : `${label}: no answer from the service.`;
  delete $('cMsg').dataset.auto;
  busy = false;
  tick();
}
for (const [axis, fm] of Object.entries(FORMS)) {
  fm.onsubmit = (e) => {
    e.preventDefault();
    move({ op: 'goto', axis, value: fm.v.value }, `${axis[0].toUpperCase() + axis.slice(1)} to ${fm.v.value}${axis == 'rotator' ? '°' : ''}`);
    ED[axis].clear();
  };
}
document.querySelectorAll('.jog').forEach((seg) => seg.addEventListener('click', (e) => {
  const b = e.target.closest('button'); if (!b) return;
  move({ op: 'step', axis: seg.dataset.axis, value: b.dataset.d }, `${seg.dataset.axis} ${b.textContent}`);
}));
document.querySelectorAll('.halt').forEach((b) => { b.onclick = () => move({ op: 'halt', axis: b.dataset.axis }, `Stop ${b.dataset.axis}`); });
$('stopAll').onclick = () => move({ op: 'haltall' }, 'Stop all');
$('rSync').onclick = () => {
  const v = $('fRot').v.value;
  if (!confirm(`Call the rotator's current angle ${v}°?\n\nThe motor doesn't move; the position angle in Alpaca programs and on this page shifts by the difference.`)) return;
  move({ op: 'sync', axis: 'rotator', value: v }, `Sync rotator to ${v}°`);
  ED.rotator.clear();
};
// Set position (Moonlite's SET POS): xSP n#, the motor doesn't move
document.querySelectorAll('.setpos').forEach((b) => { b.onclick = () => {
  const axis = b.dataset.axis, v = FORMS[axis].v.value;
  if (!confirm(`Tell the NiteCrawler the ${axis} is at ${v} steps?\n\nThe motor doesn't move; its position count changes to ${v}.`)) return;
  move({ op: 'setpos', axis, value: v }, `Set ${axis} position to ${v}`);
  ED[axis].clear();
}; });
$('rCount').oninput = () => { $('rCount').dataset.edited = '1'; };
$('rSet').onclick = () => {
  const v = $('rCount').value;
  if (!confirm(`Set the NiteCrawler's rotator count to ${v} steps?\n\nThe motor doesn't move. Home (0) moves with the count; the sky angle on this page stays the same.`)) return;
  move({ op: 'setpos', axis: 'rotator', value: v }, `Set rotator count to ${v}`);
  delete $('rCount').dataset.edited;
};

// Rotator dial: the needle is the current sky angle, the dashed one the Go to angle. Tap or drag to pick an angle
// (it fills Go to; Go moves). Arrow keys nudge by 1° (Shift: 10°).
const NS = 'http://www.w3.org/2000/svg';
function el(tag, attrs, parent) { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.appendChild(e); return e; }
const D = $('dial');
(() => {
  el('circle', { r: 96, class: 'ring' }, D);
  for (let a = 0; a < 360; a += 10) {
    const big = a % 90 == 0, r0 = big ? 82 : (a % 30 == 0 ? 87 : 91), t = (a - 90) * Math.PI / 180;
    el('line', { x1: r0 * Math.cos(t), y1: r0 * Math.sin(t), x2: 96 * Math.cos(t), y2: 96 * Math.sin(t), class: 'tk' + (big ? ' b' : '') }, D);
  }
  for (const a of [0, 90, 180, 270]) { const t = (a - 90) * Math.PI / 180; el('text', { x: 70 * Math.cos(t), y: 70 * Math.sin(t), class: 'lbl' }, D).textContent = a; }
  D._tgt = el('line', { x1: 0, y1: 0, class: 'tgt' }, D);
  D._now = el('line', { x1: 0, y1: 0, class: 'now' }, D);
  el('circle', { r: 34, class: 'hub' }, D);
  D._val = el('text', { y: -4, class: 'val' }, D);
  D._sub = el('text', { y: 18, class: 'sub' }, D);
})();
function needle(line, deg, r) { const t = (deg - 90) * Math.PI / 180; line.setAttribute('x2', r * Math.cos(t)); line.setAttribute('y2', r * Math.sin(t)); }
function dial(s) {
  const now = s.rotator.sky, v = parseFloat($('fRot').v.value);
  needle(D._now, now, 78);
  D._val.textContent = now.toFixed(1) + '°';
  const show = Number.isFinite(v) && Math.abs(((v - now + 540) % 360) - 180) > 0.05;
  D._tgt.style.display = show ? '' : 'none';
  if (show) needle(D._tgt, v, 90);
  D._sub.textContent = show ? '→ ' + v.toFixed(1) + '°' : (s.rotator.moving ? 'moving' : '');
}
function dialPick(e) {
  const r = D.getBoundingClientRect(), x = e.clientX - (r.left + r.width / 2), y = e.clientY - (r.top + r.height / 2);
  if (Math.hypot(x, y) < r.width * 0.12) return;   // the hub: ignore
  const a = (Math.atan2(y, x) * 180 / Math.PI + 90 + 360) % 360;
  const step = e.shiftKey ? 1 : 0.5;
  setDial(Math.round(a / step) * step % 360);
}
function setDial(a) {
  if ($('fRot').v.disabled) return;
  $('fRot').v.value = a.toFixed(2);
  $('fRot').v.dispatchEvent(new Event('input', { bubbles: true }));   // marks Go to as edited, so polling leaves it alone
  if (last) dial(last);
}
let dragging = false;
D.addEventListener('pointerdown', (e) => { dragging = true; D.setPointerCapture(e.pointerId); dialPick(e); });
D.addEventListener('pointermove', (e) => { if (dragging) dialPick(e); });
D.addEventListener('pointerup', () => { dragging = false; });
D.addEventListener('keydown', (e) => {
  const d = { ArrowRight: 1, ArrowUp: 1, ArrowLeft: -1, ArrowDown: -1 }[e.key];
  if (!d) return;
  e.preventDefault();
  setDial(((parseFloat($('fRot').v.value) || 0) + d * (e.shiftKey ? 10 : 1) + 360) % 360);
});

$('home').onclick = () => {
  if (!confirm('Find home for focus and rotator?\n\nFocus runs to its in-limit and the rotator to its home switch. Nothing else answers until it has finished (Stop all motors still works).')) return;
  move({ op: 'home', value: 3 }, 'Find home');
};
$('cUnlock').onclick = () => {
  if (!unlocked && !confirm('An Alpaca program is connected. Moving the motors from here can spoil a running sequence (autofocus, framing). Unlock anyway?')) return;
  unlocked = !unlocked;
  if (last) ctl(last);
};

// ---------------------------------------------------------------- settings cards
const MF = K.edits($('mf')), DF = K.edits($('df')), NF = K.edits($('nf'));
function settingsCards(s) {
  const st = s.settings;
  MF.fill({ focus_max: st.focus_max, aux_max: st.aux_max, focus_backlash: st.focus_backlash, aux_backlash: st.aux_backlash, focus: s.focus ? s.focus.step_delay : '', rotator: s.rotator ? s.rotator.step_delay : '', aux: s.aux ? s.aux.step_delay : '' });
  if (!$('mAux').dataset.edited) $('mAux').checked = st.aux_enabled;
  if (!$('mRev').dataset.edited) $('mRev').checked = st.rot_reverse;
  DF.fill({ brightness: st.brightness, sleep: st.sleep_brightness, dim_after: st.dim_after });
  NF.fill({ name: s.device_name_set || s.device_name_default });
  for (const id of ['mf', 'df', 'nf', 'pf']) $(id).querySelectorAll('input,select,button').forEach((x) => { x.disabled = !s.local; });
}
['mAux', 'mRev'].forEach((id) => { $(id).onchange = () => { $(id).dataset.edited = '1'; }; });
async function save(url, data, msgId, after) {
  const r = await post(url, data).catch(() => null);
  $(msgId).textContent = r ? (r.ok ? await r.text() : 'Not saved: ' + await r.text()) : 'No answer from the service.';
  if (r && r.ok && after) after();
  tick();
}
$('mf').onsubmit = async (e) => {
  e.preventDefault();
  const F = $('mf'), sp = {};
  for (const k of ['focus', 'rotator', 'aux']) if (F[k].dataset.edited) sp[k] = F[k].value;
  await save('setup/motion', { focus_max: F.focus_max.value, aux_max: F.aux_max.value, focus_backlash: F.focus_backlash.value, aux_backlash: F.aux_backlash.value, aux_enabled: $('mAux').checked ? 1 : 0, rot_reverse: $('mRev').checked ? 1 : 0 }, 'mMsg');
  if (Object.keys(sp).length) await save('setup/speed', sp, 'mMsg');
  MF.clear(); delete $('mAux').dataset.edited; delete $('mRev').dataset.edited;
};
$('df').onsubmit = (e) => {
  e.preventDefault();
  const F = $('df'), d = { brightness: F.brightness.value, sleep: F.sleep.value };
  if (F.dim_after.value !== '') d.dim_after = F.dim_after.value;   // blank: not sent (stock firmware has no PW)
  save('setup/display', d, 'dMsg', () => DF.clear());
};
$('nf').onsubmit = (e) => { e.preventDefault(); save('setup/name', { name: $('nf').name.value }, 'nMsg', () => NF.clear()); };
$('nReset').onclick = () => save('setup/name', { name: '' }, 'nMsg', () => NF.clear());
$('pf').onsubmit = (e) => { e.preventDefault(); save('setup/port', { port: $('ports').value }, 'pMsg'); };
async function loadPorts() {
  try {
    const d = await K.getJSON('setup.json');
    $('ports').innerHTML = d.ports.map((p) => `<option value="${esc(p.device)}"${p.device == d.port ? ' selected' : ''}>${esc(p.device == 'auto' ? p.desc : p.device + ' — ' + p.desc)}</option>`).join('');
  } catch (e) {}
}

function ncTable(s) {
  const S = s.stats || {};
  $('nct').innerHTML = [
    row('Model', esc(s.model || '?') + (s.steps_per_rev ? ` · ${steps(s.steps_per_rev)} rotator steps per turn` : '')),
    row('Firmware', esc(s.firmware || '?')),
    row('Serial link', s.up ? `<span class="ok">up</span> on ${esc(s.port)}` : `<span class="bad">${esc(s.error || 'down')}</span>`),
    row('Traffic', `${S.polls || 0} polls · ${S.commands || 0} commands · ${S.timeouts || 0} unanswered · opened ${S.opens || 0}×`),
    s.rotator ? row('Rotator', `sky ${deg(s.rotator.sky)} = mechanical ${deg(s.rotator.mechanical)} + offset ${f(s.settings.rot_offset, 2, '°')} (raw ${steps(s.rotator.position)} steps)`) : '',
  ].join('');
}

let qrFor = '';
function phone(s) {
  $('pPort').textContent = s.http_port;
  const u = (s.urls || [])[0];
  $('pNow').innerHTML = u ? `Open <b>${esc(u)}</b> on a phone, or scan:` : 'The service only listens on this PC (bind address), so phones can\'t reach it.';
  if (u && qrFor !== u) { qrFor = u; $('pQr').innerHTML = '<img class="qr" src="qr.svg?i=0" alt="QR code for ' + esc(u) + '" width="185" height="185">'; }
  if (!u) { qrFor = ''; $('pQr').innerHTML = ''; }
}

// ---------------------------------------------------------------- chart
const chart = K.chart({
  series: { temp: ['Temperature', '--c3', 'T'], focus: ['Focus position', '--c1', 'S'], rot: ['Rotator angle', '--c2', 'D'],
    aux: ['Aux position', '--c4', 'S2'], volt: ['Supply', '--c5', 'V'] },
  lanes: { T: { u: '°C', dec: 1, minSpan: 2 }, S: { u: 'steps', ua: 'steps', dec: 0, minSpan: 20 }, S2: { u: 'steps', dec: 0, minSpan: 20 },
    D: { u: '°', dec: 1, min: 0, max: 360, step: 90 }, V: { u: 'V', dec: 1, minSpan: 1 } },
  panels: { ft: { n: 'Focus and temperature', k: ['focus', 'temp'] }, rot: { n: 'Rotator angle', k: ['rot'] },
    pw: { n: 'Supply voltage and temperature', k: ['volt', 'temp'] }, aux: { n: 'Aux position', k: ['aux'] }, custom: { n: 'Custom…' } },
  ranges: [[1 / 6, '10 min'], [1, '1 h'], [6, '6 h'], [24, '24 h']],
  range: 1,
  gapS: 150,
  store: 'chart',
  bands: true,                     // each 10 s step's lowest-highest around its line (kit v12)
  empty: () => 'No samples yet: the service logs once a second while the NiteCrawler answers.',
  png: { name: 'nitecrawler' },
  csv: { name: 'nitecrawler', cols: [['focus', 'focus_steps'], ['rot', 'rotator_deg'], ['aux', 'aux_steps'], ['temp', 'temp_C'], ['volt', 'supply_V']] },
});
let fine = [], coarse = [];
const recs = (h) => h.rows.map((r) => { const o = { t: r[0] }; h.keys.forEach((k, i) => { o[k] = r[i + 1]; }); return o; });
function combine() { const start = fine.length ? fine[0].t : Infinity; chart.set(coarse.filter((r) => r.t < start).concat(fine)); }
async function loadFine() {
  try {
    const since = fine.length ? fine[fine.length - 1].t : 0;
    const add = recs(await K.getJSON('history.json?res=s' + (since ? '&since=' + since : '')));
    const cut = Date.now() / 1000 - 600;
    fine = fine.concat(add).filter((r) => r.t >= cut);
    combine();
  } catch (e) {}
}
async function loadCoarse() {
  try {
    const since = coarse.length ? coarse[coarse.length - 1].t : 0;
    const add = recs(await K.getJSON('history.json?res=m' + (since ? '&since=' + since : '')));
    coarse = coarse.concat(add);
    combine();
  } catch (e) {}
}

tick(); loadPorts(); loadCoarse(); loadFine();
setInterval(tick, 1000);
setInterval(loadFine, 3000);
setInterval(loadCoarse, 60000);
