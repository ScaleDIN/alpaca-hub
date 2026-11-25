// ZWO EFW page (device-web-ui kit). Endpoints: hub/devserver.py (status.json = EfwApp.status, POST control).
K.init({ storePrefix: 'zwoefw-' });
const $ = K.$, esc = K.esc;
let last = null, unlocked = false, busy = false, filtersShown = '', filtersDirty = false;
const post = (url, data) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-Hub-Page': '1' },
  body: new URLSearchParams(data || {}).toString() });
const row = (k, v) => `<tr><td class="mut">${k}</td><td>${v}</td></tr>`;
const onoff = (v) => (v == null ? '—' : v ? 'on' : 'off');
const slotName = (s, k) => (s.names && s.names[k] != null ? s.names[k] : `Filter ${k + 1}`);

async function tick() {
  let s;
  try { s = await K.getJSON('status.json'); }
  catch (e) { $('sub').textContent = 'connection lost'; K.pill('link', 'hub not running', 'bad'); K.pill('client', '…'); return; }
  last = s;
  K.versionWatch(s.build);
  $('name').textContent = s.name + (s.simulated ? ' (simulated)' : '');
  $('sub').textContent = `EFW${s.slots ? ' · ' + s.slots + ' slots' : ''}${s.firmware ? ' · firmware ' + s.firmware : ''} · driver ${s.build}`;
  K.pill('link', s.up ? 'connected' : s.lost ? 'not found' : 'not answering', s.up ? 'ok' : 'bad');
  K.pill('client', s.client ? 'Alpaca client connected' : 'no Alpaca client', s.client ? 'ok' : '');
  const st = s.state_class == 'ok' ? '' : s.state_class;
  const at = s.moving ? 'turning' : s.position == null ? '—' : `Slot ${s.position + 1}`;
  $('tiles').innerHTML = [
    K.tile('Filter', s.moving || s.position == null ? '—' : esc(slotName(s, s.position)), at, ''),
    K.tile('State', esc(s.state), s.last_move && s.last_move.text != s.state ? esc(s.last_move.text) : '', st),
    K.tile('Slots', s.slots == null ? '—' : String(s.slots), s.slot_time == null ? '' : `about ${s.slot_time} s per slot`),
  ].join('');
  $('pinbar').innerHTML = `<span>${esc(s.name)} <b>${s.moving || s.position == null ? '…' : esc(slotName(s, s.position))}</b></span><span class="${st}">${esc(s.state)}</span>`;
  $('dt').innerHTML = row('Model', esc(s.model || '—')) +
    row('Serial', esc(s.serial) + (s.has_serial ? '' : ' <span class="mut">(no serial in this firmware: made from the SDK ID)</span>')) +
    row('Firmware', esc(s.firmware || '—')) + row('ZWO SDK', esc(s.sdk || '—') + (s.sdk_id != null ? ` <span class="mut">· ID ${s.sdk_id}</span>` : '')) +
    row('Slots', s.slots == null ? 'counting…' : String(s.slots)) + row('One-way rotation', onoff(s.one_way)) +
    row('Hardware error', s.hw_error == null ? '<span class="mut">not reported by this firmware</span>' : s.hw_error ? `<span class="bad">code ${s.hw_error}</span>` : 'none') +
    row('Measured', s.slot_time == null ? '<span class="mut">after the first turn</span>' : `${s.slot_time} s per slot`) +
    row('Alpaca', `FilterWheel · <span class="mut">${esc(s.uid)}</span>`) +
    (s.error ? row('Last problem', `<span class="warn">${esc(s.error)}</span>`) : '');
  ctl(s);
  filters(s);
  if (!chart) makeChart();
}

function ctl(s) {
  if (!s.client) unlocked = false;
  const locked = s.client && !unlocked, off = locked || !s.up || busy || s.moving || !s.slots;
  $('cLock').classList.toggle('on', locked);
  $('cLockTxt').textContent = !s.client ? 'No Alpaca client connected: the controls are free.'
    : unlocked ? 'Unlocked while an Alpaca program is connected.' : 'An Alpaca program is connected. Use the wheel there, or unlock.';
  $('cUnlock').textContent = unlocked ? 'Lock' : 'Unlock';
  $('cUnlock').style.visibility = s.client ? '' : 'hidden';
  const want = Array.from({ length: s.slots || 0 }, (_, k) => k);
  const html = want.map((k) => `<button type="button" data-slot="${k}" class="${!s.moving && s.position == k ? 'cur' : ''}" title="Turn to slot ${k + 1}">${k + 1} · ${esc(slotName(s, k))}</button>`).join('');
  if ($('slots').dataset.html != html) {
    $('slots').innerHTML = html; $('slots').dataset.html = html;
    $('slots').querySelectorAll('button').forEach((b) => { b.onclick = () => act({ op: 'goto', value: b.dataset.slot }, `Slot ${+b.dataset.slot + 1}`); });
  }
  $('slots').querySelectorAll('button').forEach((b) => { b.disabled = off; });
  const m = $('cMsg');
  if (!s.up) { m.textContent = `The filter wheel is ${s.lost || 'not answering'}: the controls come back when it does.`; m.dataset.auto = '1'; }
  else if (!s.slots) { m.textContent = 'The wheel is counting its slots…'; m.dataset.auto = '1'; }
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
  if (!unlocked && !confirm('An Alpaca program is connected. Turning the wheel from here can spoil a running sequence. Unlock anyway?')) return;
  unlocked = !unlocked;
  if (last) ctl(last);
};

// ---------------------------------------------------------------- the filters' names and offsets
function filters(s, force) {
  const key = JSON.stringify([s.slots, s.names, s.offsets]);
  if (!force && (filtersDirty || key == filtersShown)) return;
  filtersShown = key; filtersDirty = false;
  $('ffRows').innerHTML = Array.from({ length: s.slots || 0 }, (_, k) =>
    `<tr><td>${k + 1}</td><td><input name="name${k}" maxlength="32" value="${esc(slotName(s, k))}"></td>` +
    `<td><span class="nu"><input name="offset${k}" type="number" step="1" inputmode="numeric" value="${s.offsets && s.offsets[k] != null ? s.offsets[k] : 0}"><i class="u">steps</i></span></td></tr>`).join('');
  $('filters').style.display = s.slots ? '' : 'none';
}
$('ff').oninput = () => { filtersDirty = true; $('fMsg').textContent = 'Not saved yet.'; };
$('ffReset').onclick = () => { if (last) filters(last, true); $('fMsg').textContent = ''; };
$('ff').onsubmit = async (e) => {
  e.preventDefault();
  const data = { op: 'filters', override: 1 };   // names don't move anything: allowed while a client is connected
  new FormData($('ff')).forEach((v, k) => { data[k] = v; });
  $('fMsg').textContent = 'Saving…';
  const r = await post('control', data).catch(() => null);
  $('fMsg').textContent = r ? (r.ok ? 'Saved. A connected imaging program sees them when it connects again.' : `Not saved: ${await r.text()}`) : 'No answer from the hub.';
  if (r && r.ok) filtersDirty = false;
  tick();
};

// ---------------------------------------------------------------- chart
let chart = null;
function makeChart() {
  chart = K.chart({
    series: { pos: ['Slot', '--c1', 'P'] },
    lanes: { P: { u: '', dec: 0, minSpan: 2 } },
    panels: { pos: { n: 'Slot', k: ['pos'] }, custom: { n: 'Custom…' } },
    ranges: [[1 / 6, '10 min'], [1, '1 h'], [6, '6 h'], [24, '24 h']],
    range: 1,
    gapS: 150,
    store: 'chart-zwoefw',
    bands: false,
    hold: ['pos'],               // held values: exact changes, drawn as steps (kit v16)
    empty: () => 'No samples yet: the hub logs once a second while the wheel answers.',
    png: { name: 'zwoefw' },
    csv: { name: 'zwoefw', cols: [['pos', 'slot']] },
  });
  combine();
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
