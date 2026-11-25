// Optec FocusLynx / ThirdLynx and Gemini / Hercules page (device-web-ui kit v10). Endpoints: hub/devserver.py.
K.init({ storePrefix: 'optec-' });
const $ = K.$, f = K.f, esc = K.esc;
let last = null, unlocked = false, busy = false, built = null;
const post = (url, data) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-Hub-Page': '1' },
  body: new URLSearchParams(data || {}).toString() });
const row = (k, v) => `<tr><td class="mut">${k}</td><td>${v}</td></tr>`;
const steps = (v) => (v == null ? '—' : String(v).replace(/\B(?=(\d{3})+(?!\d))/g, ' '));
const val = (a, v) => (a.kind == 'rotator' ? (v == null ? '—' : f(v, 2, '°')) : steps(v));
const temp = (v) => (v == null ? '—' : f(v, 1, ' °C'));
const state = (a) => (a.kind == 'focuser' && !a.configured ? ['no type set', 'warn'] : a.homing ? ['homing', 'warn'] : a.moving ? ['moving', 'warn']
  : a.homed === false && a.kind == 'rotator' ? ['not homed', 'warn'] : ['idle', '']);

// The hub's Alpaca numbers for this device's axes (Focuser #1 …): this driver's own server numbers them from 0, the
// hub counts across every device. They come from the hub's hub.json ("alpaca": type, number, name, slug), read every
// 30 s; until then, or without a hub, a label has no number.
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
const alpacaName = (a) => {
  const t = a.kind == 'rotator' ? 'Rotator' : 'Focuser', n = nums ? nums[a.kind + '|' + a.name] : null;
  return n == null ? t : `${t} #${n}`;
};

// ---------------------------------------------------------------- live status
async function tick() {
  let s;
  hubNumbers();
  try { s = await K.getJSON('status.json'); }
  catch (e) { $('sub').textContent = 'connection lost'; K.pill('link', 'hub not running', 'bad'); K.pill('client', '…'); return; }
  last = s;
  K.versionWatch(s.build);
  $('name').textContent = s.name + (s.simulated ? ' (simulated)' : '');
  $('sub').textContent = `${s.model || 'Optec'}${s.firmware ? ' · firmware ' + s.firmware : ''} · driver ${s.build} · ${s.port}`;
  K.pill('link', s.up ? 'link up' : 'not responding', s.up ? 'ok' : 'bad');
  K.pill('client', s.client ? 'Alpaca client connected' : 'no Alpaca client', s.client ? 'ok' : '');
  build(s);
  if (!chart) { makeChart(s); combine(); }
  ctl(s);
  phone(s);
  $('ht').innerHTML = row('Model', esc(s.model || '—')) + row('Firmware', esc(s.firmware || '—')) +
    (s.serial ? row('Serial', esc(s.serial)) : '') + row('Serial port', esc(s.port) + ' · 115200 baud') +
    s.axes.map((a) => row(esc(alpacaName(a)), esc(a.name) + (a.dev_type ? ` <span class="mut">· type ${esc(a.dev_type)}</span>` : '') +
      (a.max ? ` <span class="mut">· max ${steps(a.max)}</span>` : ''))).join('') +
    s.axes.filter((a) => a.settings && a.settings.length).map((a) => row(esc(alpacaName(a)) + ' settings',
      a.settings.map(([k, v]) => `${esc(k)}: <b>${esc(v)}</b>`).join('<br>') + '<br><span class="mut">stored on the device; change them in Optec\'s software</span>')).join('') +
    (s.axes.some((a) => a.kind == 'rotator') ? row('Sky offset', f(s.axes.find((a) => a.kind == 'rotator').sky_offset, 3, '°') + ' <span class="mut">(set by Sync, kept by the hub)</span>') : '') +
    (s.error ? row('Last problem', `<span class="warn">${esc(s.error)}</span>`) : '');
  if (!s.up) {
    $('tiles').innerHTML = K.tile(esc(s.name), '<span class="bad">no link</span>', esc(s.error || 'the Optec hub does not answer on the serial port'));
    pinFill([['st', 0, '<span class="bad">Optec hub not responding</span>']]);
    return;
  }
  const t = s.axes.find((a) => a.temperature != null);
  $('tiles').innerHTML = s.axes.map((a) => {
    const [st, cls] = state(a);
    const sub = a.kind == 'rotator' ? `${st} · mechanical ${val(a, a.mechanical)}` : `${st}${a.moving ? ' to ' + steps(a.target) : ''}${a.max ? ' · max ' + steps(a.max) : ''}`;
    return K.tile(esc(a.name), val(a, a.position), sub, cls);
  }).join('') + K.tile('Temperature', temp(t && t.temperature), t ? 'probe on the focuser' : 'no probe attached');
  pinFill(s.axes.map((a, i) => [`a${i}`, i > 1 ? 1 : 0, `${esc(a.id)} <b>${val(a, a.position)}</b>`, state(a)[1]])
    .concat([['t', 1, temp(t && t.temperature)]]));
}

// Pinned bar fields at fixed widths (their worst case, measured once off screen in the bar's own font).
const PIN_WORST = { st: 'Optec hub not responding', a0: 'F1 <b>999 999</b>', a1: 'F2 <b>359.99°</b>', a2: 'R <b>359.99°</b>', t: '-20.0 °C' };
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

// ---------------------------------------------------------------- controls: one group per motor, built once
const forms = {};
function build(s) {
  const key = s.axes.map((a) => a.id).join(',');
  if (built === key) return;
  built = key;
  $('axes').innerHTML = s.axes.map((a) => {
    const rot = a.kind == 'rotator', u = rot ? '°' : 'steps';
    const max = rot ? 359.99 : (a.max || 999999), step = rot ? 0.01 : 1;
    return `<form class="cgrp ctl" data-axis="${esc(a.id)}" autocomplete="off"><h3>${esc(a.name)}</h3>
<label title="${rot ? 'Sky angle (after Sync). The rotator takes the short way round' : 'Absolute position, 0 to the maximum set in Optec\'s software'}">Go to <span class="nu"><input name="go" type="number" min="0" max="${max}" step="${step}" inputmode="decimal"><i class="u">${u}</i></span></label>
<div class="crow"><span>Move</span><span class="seg"><button type="button" data-op="goto">Go</button><button type="button" data-op="halt">Halt</button></span></div>
<label title="${rot ? 'Turn by this many degrees from where it is now, either way' : 'Move by this many steps from where it is now: In lowers the position, Out raises it'}">Step size <span class="nu"><input name="step" type="number" min="${step}" max="${rot ? 180 : max}" step="${step}" inputmode="decimal" placeholder="${rot ? '1' : '100'}"><i class="u">${u}</i></span></label>
<div class="crow"><span>Step</span><span class="seg"><button type="button" data-op="stepdown">${rot ? '−' : 'In'}</button><button type="button" data-op="stepup">${rot ? '+' : 'Out'}</button><button type="button" data-op="center" title="${rot ? 'Turn to mechanical 0°, as Optec\'s Center does (its sky angle depends on Sync)' : 'Move to the middle of the travel (half the maximum)'}">Center</button></span></div>
<div class="crow"><span title="${rot ? 'Drives to the home switch; Optec\'s firmware needs this once after power-up before the rotator moves' : 'Drives to the home position'}">Home</span><span class="seg"><button type="button" data-op="home">Home</button></span></div>
<label title="${rot ? 'Say what sky angle the rotator is at now. The motor does not move; the hub keeps the offset' : 'Say what position the focuser is at now (sync). The motor does not move'}">Sync <span class="nu"><input name="sync" type="number" min="0" max="${max}" step="${step}" inputmode="decimal"><i class="u">${u}</i></span></label>
<div class="crow"><span>Set position</span><span class="seg"><button type="button" data-op="sync">Sync</button></span></div></form>`;
  }).join('');
  document.querySelectorAll('#axes form').forEach((fm) => {
    const id = fm.dataset.axis;
    forms[id] = K.edits(fm);
    fm.querySelectorAll('[data-op]').forEach((b) => { b.onclick = () => op(fm, b.dataset.op); });
    fm.onsubmit = (e) => { e.preventDefault(); op(fm, 'goto'); };
  });
}
function op(fm, o) {
  const id = fm.dataset.axis, a = last && last.axes.find((x) => x.id == id);
  const name = a ? a.name : id;
  const rot = a && a.kind == 'rotator';
  if (o == 'stepup' || o == 'stepdown') {
    const n = Math.abs(parseFloat(fm.step.value || (rot ? '1' : '100')));
    if (!(n > 0)) { $('cMsg').textContent = 'Step: enter a size first.'; return; }
    const v = o == 'stepup' ? n : -n;
    act({ op: 'step', axis: id, value: rot ? v : Math.round(v) }, `${name}: step ${v > 0 ? '+' : ''}${rot ? v : Math.round(v)}${rot ? '°' : ''}`);
    return;
  }
  if (o == 'center') {
    if (!confirm(rot ? `Turn ${name} to mechanical 0°?` : `Move ${name} to the middle of its travel${a && a.max ? ' (' + steps(Math.floor(a.max / 2)) + ')' : ''}?`)) return;
    act({ op: 'center', axis: id }, `${name}: center`);
    return;
  }
  if (o == 'goto' || o == 'sync') {
    const v = fm[o == 'goto' ? 'go' : 'sync'].value;
    if (v === '') { $('cMsg').textContent = `${o == 'goto' ? 'Go to' : 'Sync'}: enter a ${a && a.kind == 'rotator' ? 'angle' : 'position'} first.`; return; }
    if (o == 'sync' && !confirm(`Set ${name} to read ${v} without moving it?`)) return;
    act({ op: o, axis: id, value: v }, `${name}: ${o == 'goto' ? 'go to' : 'sync to'} ${v}`).then(() => forms[id].clear());
    return;
  }
  act({ op: o, axis: id }, `${name}: ${o}`);
}
function ctl(s) {
  if (!s.client) unlocked = false;
  const locked = s.client && !unlocked;
  const off = locked || !s.up || busy;
  $('cLock').classList.toggle('on', locked);
  $('cLockTxt').textContent = !s.client ? 'No Alpaca client connected: the controls are free.'
    : unlocked ? 'Unlocked while an Alpaca program is connected.' : 'An Alpaca program is connected. Use the motors there, or unlock.';
  $('cUnlock').textContent = unlocked ? 'Lock' : 'Unlock';
  $('cUnlock').style.visibility = s.client ? '' : 'hidden';
  s.axes.forEach((a) => {
    const fm = document.querySelector(`#axes form[data-axis="${CSS.escape(a.id)}"]`);
    if (!fm) return;
    forms[a.id].fill({ go: a.target != null ? a.target : '', sync: a.position != null ? a.position : '' });
    fm.querySelectorAll('input, button').forEach((x) => { x.disabled = off; });
    const noType = a.kind == 'focuser' && !a.configured;
    fm.querySelector('[data-op="halt"]').disabled = off;          // halt stays available whatever the state
    if (noType) fm.querySelectorAll('[data-op="goto"], [data-op="home"]').forEach((b) => { b.disabled = true; b.title = 'No focuser type is set for this port: set it in Optec\'s FocusLynx Commander'; });
  });
  const m = $('cMsg');
  if (!s.up) { m.textContent = 'The Optec hub does not answer: the controls come back when it does.'; m.dataset.auto = '1'; }
  else if (m.dataset.auto) { m.textContent = ''; delete m.dataset.auto; }
}
async function act(data, label) {
  busy = true; if (last) ctl(last);
  $('cMsg').textContent = label + '…';
  const r = await post('control', Object.assign({ override: unlocked ? 1 : 0 }, data)).catch(() => null);
  $('cMsg').textContent = r ? (r.ok ? `${await r.text()}.` : `${label}: not done. ${await r.text()}`) : `${label}: no answer from the hub.`;
  delete $('cMsg').dataset.auto;
  busy = false;
  tick();
}
$('cUnlock').onclick = () => {
  if (!unlocked && !confirm('An Alpaca program is connected. Moving the focuser or rotator from here can spoil a running sequence (autofocus, framing). Unlock anyway?')) return;
  unlocked = !unlocked;
  if (last) ctl(last);
};

// ---------------------------------------------------------------- phone
let qrFor = null;
function phone(s) {
  const u = (s.urls || [])[0];
  $('pUrl').textContent = u ? u + ` (the hub; this is the "${s.name}" tab)` : 'Only reachable on this PC.';
  if (u && qrFor !== u) { qrFor = u; $('pQr').innerHTML = '<img class="qr" src="qr.svg?i=0" alt="QR code for ' + esc(u) + '" width="185" height="185">'; }
}

// ---------------------------------------------------------------- chart
// Built after the first status, with only the series this hub has (a ThirdLynx with one focuser has no
// "Focuser 2", a Gemini has a rotator). History rows always carry pos1, pos2, rot, temp.
let chart = null;
function makeChart(s) {
  const ids = s.axes.map((a) => a.id), gem = s.kind == 'gemini', two = ids.includes('F2');
  const series = { pos1: [gem ? 'Focuser' : 'Focuser F1', '--c1', 'S'] };
  if (two) series.pos2 = ['Focuser F2', '--c2', 'S'];
  if (gem) series.rot = ['Rotator', '--c3', 'D'];
  series.temp = ['Temperature', '--c4', 'T'];
  const panels = { pos: { n: 'Focus and temperature', k: ['pos1'].concat(two ? ['pos2'] : [], ['temp']) } };
  if (gem) panels.rot = { n: 'Rotator', k: ['rot'] };
  panels.custom = { n: 'Custom…' };
  const cols = Object.keys(series).map((k) => [k, { pos1: gem ? 'focuser_steps' : 'F1_steps', pos2: 'F2_steps', rot: 'rotator_deg', temp: 'temperature_C' }[k]]);
  chart = K.chart({
    series,
    lanes: { S: { u: 'steps', dec: 0, minSpan: 100 }, D: { u: '°', dec: 1, min: 0, max: 360, step: 90 }, T: { u: '°C', dec: 1, minSpan: 2 } },
    panels,
    ranges: [[1 / 6, '10 min'], [1, '1 h'], [6, '6 h'], [24, '24 h']],
    range: 1,
    gapS: 150,
    store: 'chart-' + s.kind,
    bands: true,                   // each 10 s step's lowest-highest around its line (kit v12)
    hold: ['pos1', 'pos2', 'rot'], // held values: exact changes, drawn as steps (kit v16)
    empty: () => 'No samples yet: the hub logs once a second while the Optec hub answers.',
    png: { name: s.kind },
    csv: { name: s.kind, cols },
  });
}
let fine = [], coarse = [], changes = [];
const recs = (h) => h.rows.map((r) => { const o = { t: r[0] }; h.keys.forEach((k, i) => { o[k] = r[i + 1]; }); return o; });
function combine() { if (!chart) return; chart.set(K.histJoin(coarse, fine, changes)); }
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
    coarse = coarse.concat(recs(await K.getJSON('history.json?res=m' + (since ? '&since=' + since : ''))));
    const cs = changes.length ? changes[changes.length - 1][0] : 0;   // held values' changes (res=c)
    changes = changes.concat((await K.getJSON('history.json?res=c' + (cs ? '&since=' + cs : ''))).rows || []);
    combine();
  } catch (e) {}
}

tick(); loadCoarse(); loadFine();
setInterval(tick, 1000);
setInterval(loadFine, 3000);
setInterval(loadCoarse, 60000);
