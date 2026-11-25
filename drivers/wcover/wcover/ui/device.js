// WandererCover V4-EC flat panel page (device-web-ui kit v10). Endpoints: hub/devserver.py.
K.init({ storePrefix: 'wcv-' });
const $ = K.$, f = K.f, esc = K.esc;
let last = null, unlocked = false, busy = false;
const post = (url, data) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-Hub-Page': '1' },
  body: new URLSearchParams(data || {}).toString() });
const row = (k, v) => `<tr><td class="mut">${k}</td><td>${v}</td></tr>`;
const deg = (v) => (v == null ? '—' : f(v, 1, '°'));
const HEAT = { 0: 'off', 50: 'low', 100: 'high', 150: 'max' };
const COVER_CLS = { closed: '', open: '', moving: 'warn', error: 'bad', unknown: 'warn', 'not present': 'bad' };

// ---------------------------------------------------------------- live status
async function tick() {
  let s;
  try { s = await K.getJSON('status.json'); }
  catch (e) { $('sub').textContent = 'connection lost'; K.pill('link', 'hub not running', 'bad'); K.pill('client', '…'); return; }
  last = s;
  K.versionWatch(s.build);
  const shown = 'Flat panel' + (s.simulated ? ' (simulated)' : '');
  $('name').textContent = shown;
  $('sub').textContent = `${s.model || 'WandererCover'}${s.firmware ? ' · firmware ' + s.firmware : ''} · driver ${s.build} · ${s.port}`;
  const moving = s.cover == 'moving';
  K.pill('link', moving ? 'moving (quiet)' : s.up ? 'link up' : 'not responding', moving ? 'warn' : s.up ? 'ok' : 'bad');
  K.pill('client', s.client ? 'Alpaca client connected' : 'no Alpaca client', s.client ? 'ok' : '');
  ctl(s);
  phone(s);
  $('pt').innerHTML = row('Alpaca', `CoverCalibrator · ${esc(s.name)}`) + row('UniqueID', `<span class="mut">${esc(s.uid)}</span>`) +
    row('Closed angle', deg(s.close_set)) + row('Open angle', deg(s.open_set)) + row('Serial port', esc(s.port) + ' · 19200 baud') +
    (s.error ? row('Last problem', `<span class="warn">${esc(s.error)}</span>`) : '');
  if (!s.up && !moving) {
    $('tiles').innerHTML = K.tile('Flat panel', '<span class="bad">no link</span>', esc(s.error || 'no status lines on the serial port'));
    pinFill([['st', 0, '<span class="bad">Flat panel not responding</span>']]);
    return;
  }
  const light = s.brightness > 0 ? `${s.brightness}` : 'off';
  const heat = s.heater == null ? '—' : (HEAT[s.heater] || s.heater);
  $('tiles').innerHTML = [
    K.tile('Cover', esc(s.cover), moving ? 'the panel is quiet while it moves' : `at ${deg(s.position)}`, COVER_CLS[s.cover] || ''),
    K.tile('Light', s.brightness > 0 ? `${s.brightness} <span class="mut" style="font-size:60%">/ ${s.max_brightness}</span>` : 'off',
      s.calibrator == 'not ready' ? 'waiting for the panel to confirm' : s.cover == 'open' ? 'only lights while closed' : 'calibrator ' + esc(s.calibrator), s.calibrator == 'error' ? 'bad' : ''),
    K.tile('Heater', esc(heat), 'dew heater on the panel'),
    K.tile('Supply', s.voltage == null ? '—' : f(s.voltage, 2, ' V'), s.heater ? 'swings with the heater switching' : 'panel input', s.voltage != null && s.voltage < 11 ? 'warn' : ''),
    K.tile('Angles', `${deg(s.close_set)} <span class="mut" style="font-size:60%">/</span> ${deg(s.open_set)}`, 'closed / open, stored on the panel'),
  ].join('');
  pinFill([
    ['c', 0, `cover <b>${esc(s.cover)}</b>`, COVER_CLS[s.cover] || ''],
    ['l', 0, `light <b>${esc(light)}</b>`],
    ['h', 1, `heater <b>${esc(heat)}</b>`],
    ['v', 1, s.voltage == null ? '—' : f(s.voltage, 2, ' V')],
  ]);
}

// Pinned bar fields at fixed widths (their worst case, measured once off screen in the bar's own font).
const PIN_WORST = { st: 'Flat panel not responding', c: 'cover <b>not present</b>', l: 'light <b>255</b>', h: 'heater <b>high</b>', v: '13.90 V' };
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
// Locked while an Alpaca client is connected; Unlock (with a confirmation) lasts until it disconnects or the page reloads.
const LF = K.edits($('fLight'));
const AF = K.edits($('fAng'), (dirty) => { $('aSet').dataset.dirty = dirty ? '1' : ''; if (last) ctl(last); });
function ctl(s) {
  if (!s.client) unlocked = false;
  const locked = s.client && !unlocked, moving = s.cover == 'moving';
  const off = locked || !s.up || busy || moving;
  $('cLock').classList.toggle('on', locked);
  $('cLockTxt').textContent = !s.client ? 'No Alpaca client connected: the controls are free.'
    : unlocked ? 'Unlocked while an Alpaca program is connected.' : 'An Alpaca program is connected. Use the panel there, or unlock.';
  $('cUnlock').textContent = unlocked ? 'Lock' : 'Unlock';
  $('cUnlock').style.visibility = s.client ? '' : 'hidden';
  LF.fill({ b: s.brightness > 0 ? s.brightness : 50 });
  if (!$('heat').dataset.edited && s.heater != null) $('heat').value = String(s.heater);
  AF.fill({ c: s.close_set, o: s.open_set });
  document.querySelectorAll('#ctl form input, #ctl form button, #ctl select').forEach((x) => { x.disabled = off; });
  $('aSet').disabled = off || !$('aSet').dataset.dirty;
  $('lOn').disabled = off || s.cover != 'closed';
  $('lOn').title = s.cover != 'closed' ? 'The panel only lights while the cover is closed' : '';
  jogLabels(s);
  const m = $('cMsg');
  if (moving) { m.textContent = 'The cover is moving: the panel takes no commands until it reports "done" (about 18 s).'; m.dataset.auto = '1'; }
  else if (!s.up) { m.textContent = 'No status lines from the panel: the controls come back when it talks again.'; m.dataset.auto = '1'; }
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
document.querySelectorAll('#fCover [data-op]').forEach((b) => { b.onclick = () => act({ op: b.dataset.op }, b.textContent); });
$('heat').onchange = () => { $('heat').dataset.edited = '1'; act({ op: 'heater', value: $('heat').value }, 'Heater ' + HEAT[$('heat').value]).then(() => delete $('heat').dataset.edited); };
$('lOn').onclick = () => { const b = $('fLight').b.value; act({ op: 'light', value: b }, 'Light on at ' + b); LF.clear(); };
$('lOff').onclick = () => act({ op: 'light', value: 0 }, 'Light off');
$('fLight').onsubmit = (e) => { e.preventDefault(); $('lOn').click(); };
// Positions: sends only the angles that were changed, one after the other; the cap doesn't move.
$('fAng').onsubmit = async (e) => {
  e.preventDefault();
  const f = $('fAng'), todo = [['close', f.c, 'Closed angle'], ['open', f.o, 'Open angle']].filter(([, el]) => el.dataset.edited);
  for (const [which, el, label] of todo) {
    if (!el.checkValidity()) { $('cMsg').textContent = `${label}: ${el.min} to ${el.max}°.`; return; }
  }
  for (const [which, el, label] of todo) await act({ op: 'angle', which, value: el.value }, `${label} ${el.value}°`);
  AF.clear();
};
// Jog: from the end the cap rests at, store that end's angle ± the step and move there (the panel has no relative move).
function jogLabels(s) {
  const st = $('jStep').value, atEnd = s.cover == 'open' || s.cover == 'closed';
  $('jLbl').textContent = `Jog ${st}°`;
  const off = !atEnd || $('jOpen').disabled;
  ['jClose', 'jOpen'].forEach((id) => {
    $(id).disabled = off;
    $(id).title = atEnd ? `Moves the cap ${st}° and stores that as the ${s.cover == 'open' ? 'open' : 'closed'} angle` : 'Open or close the cover first: a jog starts from either end';
  });
}
$('jStep').onchange = () => { if (last) ctl(last); };
$('jClose').onclick = () => act({ op: 'jog', value: -$('jStep').value }, `Jog ${$('jStep').value}° closed`);
$('jOpen').onclick = () => act({ op: 'jog', value: $('jStep').value }, `Jog ${$('jStep').value}° open`);
$('cUnlock').onclick = () => {
  if (!unlocked && !confirm('An Alpaca program is connected. Moving the cover or changing the light from here can spoil a running flat sequence. Unlock anyway?')) return;
  unlocked = !unlocked;
  if (last) ctl(last);
};

// ---------------------------------------------------------------- phone
let qrFor = null;
function phone(s) {
  const u = (s.urls || [])[0];
  $('pUrl').textContent = u ? u + ' (the hub; the panel is the "Flat panel" tab)' : 'Only reachable on this PC.';
  if (u && qrFor !== u) { qrFor = u; $('pQr').innerHTML = '<img class="qr" src="qr.svg?i=0" alt="QR code for ' + esc(u) + '" width="185" height="185">'; }
}

// ---------------------------------------------------------------- chart
const chart = K.chart({
  series: { position: ['Cover angle', '--c1', 'D'], brightness: ['Light', '--c2', 'B'], heater: ['Heater', '--c3', 'B'], voltage: ['Supply', '--c5', 'V'] },
  lanes: { D: { u: '°', dec: 0, min: 0, max: 360, step: 90 }, B: { u: '/255', dec: 0, min: 0, max: 255 }, V: { u: 'V', dec: 2, minSpan: 1 } },
  panels: { cov: { n: 'Cover and light', k: ['position', 'brightness'] }, pw: { n: 'Supply and heater', k: ['voltage', 'heater'] }, custom: { n: 'Custom…' } },
  ranges: [[1 / 6, '10 min'], [1, '1 h'], [6, '6 h'], [24, '24 h']],
  range: 1,
  gapS: 150,
  store: 'chart',
  bands: true,                     // each 10 s step's lowest-highest around its line (kit v12)
  hold: ['brightness', 'heater'], // held values: exact changes, drawn as steps (kit v16)
  empty: () => 'No samples yet: the hub logs once a second while the panel talks.',
  png: { name: 'flat-panel' },
  csv: { name: 'flat-panel', cols: [['position', 'cover_deg'], ['brightness', 'light_of_255'], ['heater', 'heater_level'], ['voltage', 'supply_V']] },
});
let fine = [], coarse = [], changes = [];
const recs = (h) => h.rows.map((r) => { const o = { t: r[0] }; h.keys.forEach((k, i) => { o[k] = r[i + 1]; }); return o; });
function combine() { chart.set(K.histJoin(coarse, fine, changes)); }
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
