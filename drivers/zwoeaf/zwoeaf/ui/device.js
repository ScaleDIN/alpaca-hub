// ZWO EAF page (device-web-ui kit). Endpoints: hub/devserver.py (status.json = EafApp.status, POST control).
K.init({ storePrefix: 'zwoeaf-' });
const $ = K.$, f = K.f, esc = K.esc;
let last = null, unlocked = false, busy = false;
const post = (url, data) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-Hub-Page': '1' },
  body: new URLSearchParams(data || {}).toString() });
const row = (k, v) => `<tr><td class="mut">${k}</td><td>${v}</td></tr>`;
const steps = (v) => (v == null ? '—' : String(v).replace(/\B(?=(\d{3})+(?!\d))/g, ' '));
const temp = (v) => (v == null ? '—' : f(v, 1, ' °C'));
const onoff = (v) => (v == null ? '—' : v ? 'on' : 'off');
const form = K.edits($('fm'));

async function tick() {
  let s;
  try { s = await K.getJSON('status.json'); }
  catch (e) { $('sub').textContent = 'connection lost'; K.pill('link', 'hub not running', 'bad'); K.pill('client', '…'); return; }
  last = s;
  K.versionWatch(s.build);
  $('name').textContent = s.name + (s.simulated ? ' (simulated)' : '');
  $('sub').textContent = `${s.kind || s.model || 'EAF'}${s.firmware ? ' · firmware ' + s.firmware : ''} · driver ${s.build}`;
  K.pill('link', s.up ? 'connected' : s.lost ? 'not found' : 'not answering', s.up ? 'ok' : 'bad');
  K.pill('client', s.client ? 'Alpaca client connected' : 'no Alpaca client', s.client ? 'ok' : '');
  const st = s.state_class == 'ok' ? '' : s.state_class;
  $('tiles').innerHTML = [
    K.tile('Position', steps(s.position), s.max_step != null ? `of ${steps(s.max_step)} steps` : '', ''),
    K.tile('State', esc(s.state), s.last_move && s.last_move.text != s.state ? esc(s.last_move.text) : '', st),
    K.tile('Temperature', temp(s.temperature), s.temperature == null ? 'no reading' : 'probe, else inside the EAF'),   // the SDK reads the probe when one is plugged in (seen 2026-11-24) and doesn't say which
  ].join('');
  $('pinbar').innerHTML = `<span>${esc(s.name)} <b>${steps(s.position)}</b></span><span class="${st}">${esc(s.state)}</span><span>${temp(s.temperature)}</span>`;
  $('dt').innerHTML = row('Model', esc(s.model || '—') + (s.kind ? ` <span class="mut">· ${esc(s.kind)}</span>` : '')) +
    row('Serial', esc(s.serial) + (s.has_serial ? '' : ' <span class="mut">(no serial in this firmware: made from the SDK ID)</span>')) +
    row('Firmware', esc(s.firmware || '—')) + row('ZWO SDK', esc(s.sdk || '—') + (s.sdk_id != null ? ` <span class="mut">· ID ${s.sdk_id}</span>` : '')) +
    row('Maximum step', steps(s.max_step)) + row('Reverse', onoff(s.reverse)) +
    row('Backlash', s.backlash == null ? '—' : steps(s.backlash) + ' steps') + row('Beep', onoff(s.beep)) +
    row('Stall reporting', s.codes == null ? '—' : s.codes ? `yes (motor code ${esc(s.code || '—')}${s.code_text && s.code_text != s.code ? ': ' + esc(s.code_text) : ''})` : 'not in this firmware: only "not finishing" is watched') +
    row('Measured speed', s.speed == null ? '<span class="mut">after the first longer move</span>' : steps(s.speed) + ' steps/s') +
    row('Alpaca', `Focuser · <span class="mut">${esc(s.uid)}</span>`) +
    (s.error ? row('Last problem', `<span class="warn">${esc(s.error)}</span>`) : '');
  ctl(s);
  if (!chart) makeChart();
}

function ctl(s) {
  if (!s.client) unlocked = false;
  const locked = s.client && !unlocked, off = locked || !s.up || busy;
  $('cLock').classList.toggle('on', locked);
  $('cLockTxt').textContent = !s.client ? 'No Alpaca client connected: the controls are free.'
    : unlocked ? 'Unlocked while an Alpaca program is connected.' : 'An Alpaca program is connected. Use the focuser there, or unlock.';
  $('cUnlock').textContent = unlocked ? 'Lock' : 'Unlock';
  $('cUnlock').style.visibility = s.client ? '' : 'hidden';
  $('fm').go.max = s.max_step == null ? '' : s.max_step;
  form.fill({ go: s.target != null ? s.target : s.position != null ? s.position : '' });
  $('fm').querySelectorAll('input, button').forEach((x) => { x.disabled = off || s.hand; });
  $('fm').querySelector('[data-op="halt"]').disabled = !s.up || s.hand;
  const m = $('cMsg');
  if (!s.up) { m.textContent = `The focuser is ${s.lost || 'not answering'}: the controls come back when it does.`; m.dataset.auto = '1'; }
  else if (s.hand) { m.textContent = 'The hand controller is moving the focuser.'; m.dataset.auto = '1'; }
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
function op(o) {
  const fm = $('fm');
  if (o == 'halt') { act({ op: 'halt' }, 'Halt'); return; }
  if (o == 'goto') {
    if (fm.go.value === '') { $('cMsg').textContent = 'Go to: enter a position first.'; return; }
    act({ op: 'goto', value: fm.go.value }, `Go to ${fm.go.value}`).then(() => form.clear());
    return;
  }
  const n = Math.round(Math.abs(parseFloat(fm.step.value || '100')));
  if (!(n > 0)) { $('cMsg').textContent = 'Step: enter a size first.'; return; }
  act({ op: 'step', value: o == 'out' ? n : -n }, `Step ${o == 'out' ? '+' : '−'}${n}`);
}
$('fm').querySelectorAll('[data-op]').forEach((b) => { if (b.type == 'button') b.onclick = () => op(b.dataset.op); });
$('fm').onsubmit = (e) => { e.preventDefault(); op('goto'); };
$('cUnlock').onclick = () => {
  if (!unlocked && !confirm('An Alpaca program is connected. Moving the focuser from here can spoil a running sequence (autofocus). Unlock anyway?')) return;
  unlocked = !unlocked;
  if (last) ctl(last);
};

// ---------------------------------------------------------------- chart
let chart = null;
function makeChart() {
  chart = K.chart({
    series: { pos: ['Position', '--c1', 'S'], temp: ['Temperature', '--c4', 'T'] },
    lanes: { S: { u: 'steps', dec: 0, minSpan: 100 }, T: { u: '°C', dec: 1, minSpan: 2 } },
    panels: { pos: { n: 'Position and temperature', k: ['pos', 'temp'] }, custom: { n: 'Custom…' } },
    ranges: [[1 / 6, '10 min'], [1, '1 h'], [6, '6 h'], [24, '24 h']],
    range: 1,
    gapS: 150,
    store: 'chart-zwoeaf',
    bands: true,
    empty: () => 'No samples yet: the hub logs once a second while the focuser answers.',
    png: { name: 'zwoeaf' },
    csv: { name: 'zwoeaf', cols: [['pos', 'position_steps'], ['temp', 'temperature_C']] },
  });
  combine();
}
let fine = [], coarse = [];
const recs = (h) => h.rows.map((r) => { const o = { t: r[0] }; h.keys.forEach((k, i) => { o[k] = r[i + 1]; }); return o; });
function combine() { if (!chart) return; const start = fine.length ? fine[0].t : Infinity; chart.set(coarse.filter((r) => r.t < start).concat(fine)); }
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
    combine();
  } catch (e) {}
}

tick(); loadCoarse(); loadFine();
setInterval(tick, 1000);
setInterval(loadFine, 3000);
setInterval(loadCoarse, 60000);
