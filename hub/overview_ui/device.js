// The hub Overview's chart: every device's history on one time axis (device-web-ui kit v16). Endpoints: /hub.json and
// each device's own /<slug>/history.json (res=s: 1 s samples for 10 min; res=m: 10-second averages with each value's
// lowest and highest, <key>_min / <key>_max, drawn as a band; older drivers: 1-minute averages), and before what
// the device holds in memory, its day files (/hub/history…): so the chart goes back past a restart of the hub or PC.
K.init({ storePrefix: 'hubc-' });
const $ = K.$;

// What each kind of device records (its history.json keys, as on its own tab): [label, lane].
const KINDS = {
  nitecrawler: { temp: ['Temperature', 'T'], volt: ['Supply', 'V'], focus: ['Focus position', 'S'], rot: ['Rotator angle', 'D'], aux: ['Aux position', 'S'] },
  wbox: { vin: ['Input voltage', 'V'], iin: ['Input current', 'A'], pwr: ['Input power', 'W'], dc2: ['DC2 setpoint', 'V'],
    lens: ['Lens', 'T'], amb: ['Ambient', 'T'], dp: ['Dew point', 'T'], tgt: ['Dew target', 'T'], hum: ['Humidity', 'pct'], dew: ['DC3/Dew', 'pct'] },
  wcover: { position: ['Cover angle', 'D'], brightness: ['Light', 'B'], heater: ['Heater', 'B'], voltage: ['Supply', 'V'] },
  focuslynx: { pos1: ['Focuser F1', 'S'], pos2: ['Focuser F2', 'S'], temp: ['Temperature', 'T'] },
  gemini: { pos1: ['Focuser', 'S'], rot: ['Rotator', 'D'], temp: ['Temperature', 'T'] },
};
// One scale per unit, shared by every device.
const LANES = { T: { u: '°C', dec: 1, minSpan: 2 }, V: { u: 'V', dec: 2, minSpan: 0.2 }, A: { u: 'A', dec: 2, min: 0, minSpan: 0.5 },
  W: { u: 'W', dec: 1, min: 0, minSpan: 2 }, pct: { u: '%', dec: 0, min: 0, max: 100 }, S: { u: 'steps', ua: 'steps', dec: 0, minSpan: 20 },
  D: { u: '°', dec: 1, min: 0, max: 360, step: 90 }, B: { u: '', ua: 'level', dec: 0, min: 0, max: 255 } };
// Ready-made views: (device kind, key, lane) -> in it? Empty ones are left out. Custom… picks any mix.
const PRESETS = [
  ['temp', 'Temperatures', (k, key, lane) => lane == 'T' && key != 'tgt'],
  ['power', 'Power: voltages, current, watts', (k, key, lane) => lane == 'V' || lane == 'A' || lane == 'W'],
  ['pos', 'Positions: focusers, rotators, cover', (k, key, lane) => lane == 'S' || lane == 'D'],
  ['dew', 'Dew: lens, dew point, humidity, heaters', (k, key) => (k == 'wbox' && ['lens', 'dp', 'tgt', 'hum', 'dew'].includes(key)) || (k == 'wcover' && key == 'heater')],
];
const FINE_S = 600, HOLD_FINE = 2, HOLD_COARSE = 90;

let devs = [], chart = null, sig = '';
const recs = (h) => h.rows.map((r) => { const o = { t: r[0] }; h.keys.forEach((k, i) => { o[k] = r[i + 1]; }); return o; });
const hasData = (d, key) => d.coarse.some((r) => r[key] != null) || d.fine.some((r) => r[key] != null);

/* Records on the union of every device's sample times (bucketed: 1 s, or 1 min for averages; the box dates its
   minutes in the middle). Devices sample at slightly different moments, and a record without a device's value breaks
   that device's line, so each device's last reading is carried for up to `hold` s. A device silent for longer shows as
   a gap, never as zero. */
function merge(which, hold, bucket) {
  const ts = new Set();
  devs.forEach((d) => d[which].forEach((r) => ts.add(bucket(r.t))));
  const out = [...ts].sort((a, b) => a - b).map((t) => ({ t }));
  devs.forEach((d) => {
    const R = d[which];
    let i = -1;
    out.forEach((o) => {
      while (i + 1 < R.length && bucket(R[i + 1].t) <= o.t) i++;
      if (i < 0 || o.t - bucket(R[i].t) > hold) return;
      d.keys.forEach((k) => ['', '_min', '_max'].forEach((x) => { if (R[i][k + x] != null) o[d.slug + '.' + k + x] = R[i][k + x]; }));
    });
  });
  return out;
}
/* Held values (positions, set levels: the device's res=m "held"): from each one's first logged change on, its exact
   changes (res=c) instead of the steps' values, as records of their own; the chart carries them (kit v16 hold). */
function combine() {
  if (!chart) return;
  const start = Math.min(...devs.map((d) => (d.fine.length ? d.fine[0].t : Infinity)));
  const extra = [];
  devs.forEach((d) => {
    const first = {};
    d.changes.forEach(([t, k, v]) => {
      if (!(k in first)) first[k] = t;
      if (t < start && d.keys.includes(k)) extra.push({ t, [d.slug + '.' + k]: v });
    });
    d.coarseH = d.coarse.map((r) => {
      const c = Object.assign({}, r);
      Object.keys(first).forEach((k) => { if (r.t + 10 > first[k]) { delete c[k]; delete c[k + '_min']; delete c[k + '_max']; } });
      return c;
    });
  });
  const coarse = merge('coarseH', HOLD_COARSE, (t) => Math.floor(t / 10) * 10).filter((r) => r.t < start);   // 10 s steps
  const byT = new Map(coarse.map((r) => [r.t, r]));
  const own = extra.filter((x) => (byT.has(x.t) ? (Object.assign(byT.get(x.t), x), false) : true));   // same time: one record
  chart.set(coarse.concat(own).sort((a, b) => a.t - b.t).concat(merge('fine', HOLD_FINE, Math.floor)));
}

async function loadFine(d) {
  try {
    const since = d.fine.length ? d.fine[d.fine.length - 1].t : 0;
    const add = recs(await K.getJSON(`/${d.slug}/history.json?res=s` + (since ? '&since=' + since : '')));
    const cut = Date.now() / 1000 - FINE_S;
    d.fine = d.fine.concat(add).filter((r) => r.t >= cut);
  } catch (e) {}
}
async function loadCoarse(d) {
  try {
    const since = d.coarse.length ? d.coarse[d.coarse.length - 1].t : 0;
    const h = await K.getJSON(`/${d.slug}/history.json?res=m` + (since ? '&since=' + since : ''));
    d.coarse = d.coarse.concat(recs(h));
    if (h.held && h.held.length) {   // held values: their exact changes too
      d.held = h.held;
      const cs = d.changes.length ? d.changes[d.changes.length - 1][0] : 0;
      d.changes = d.changes.concat((await K.getJSON(`/${d.slug}/history.json?res=c` + (cs ? '&since=' + cs : ''))).rows || []);
    }
  } catch (e) {}
}

/* The ready-made views for series = {id: [label, '', lane]} and order = [[id, kind, key, lane]], plus Custom…
   (extra: views put before it). Gives the colours too: in view order, so each ready-made view gets distinct ones
   (ten colours; Custom… may repeat them). */
function panelsFor(series, order, extra = {}) {
  const panels = {}, members = {};
  for (const [pid, name, test] of PRESETS) {
    const k = order.filter(([, kind, key, lane]) => test(kind, key, lane)).map(([id]) => id);
    if (k.length) { panels[pid] = { n: name, k }; members[pid] = k; }
  }
  Object.assign(panels, extra);
  panels.custom = { n: 'Custom…' };
  let n = 0;
  const give = (id) => { if (!series[id][1]) series[id][1] = `--c${(n++ % 10) + 1}`; };
  Object.values(members).forEach((k) => k.forEach(give));
  order.forEach(([id]) => give(id));
  return panels;
}

/* Before each device's in-memory minutes: its day files, as far back as the longest range (7 d). The box holds its
   own files in memory already (up to 30 days), so for it this adds only what is older than that; a device whose
   driver keeps 24 hours gets the days before, and after a restart everything before it (the time the PC was off
   shows as a gap). Read once when the page starts: the device's memory covers the rest. */
const BACK_S = 168 * 3600;
async function loadOld() {
  let units;
  try { units = (await K.getJSON('/hub/history.json')).units; } catch (e) { return; }
  const from = Date.now() / 1000 - BACK_S, day = (t) => { const x = new Date(t * 1000); return `${x.getFullYear()}-${K.p2(x.getMonth() + 1)}-${K.p2(x.getDate())}`; };
  await Promise.all(devs.map(async (d) => {
    const u = d.unit != null && units.find((x) => x.driver == d.kind && x.unit == d.unit);
    if (!u) return;
    const first = d.coarse.length ? d.coarse[0].t : d.fine.length ? d.fine[0].t : Infinity;
    const lo = day(from), hi = isFinite(first) ? day(first) : '9999';
    const names = u.files.map((f) => f.name).filter((n) => { const m = n.match(/(\d{4}-\d{2}-\d{2})/); return m && m[1] >= lo && m[1] <= hi; });
    const base = `/hub/history/${enc(u.driver)}/${enc(u.unit)}/`;
    const old = [];
    await Promise.all(names.map(async (n) => {
      try {
        const r = await fetch(base + enc(n));
        if (!r.ok) return;
        const p = parseCsv(await r.text(), d.kind);
        // the file's columns -> this device's history keys (a box day file's headers, or the keys themselves)
        const cols = p.used.filter((c) => c.d[2] == d.kind && d.keys.includes(c.d[3]));
        p.recs.forEach((rec) => {
          if (rec.t < from || rec.t >= first) return;
          const o = { t: rec.t };
          cols.forEach((c) => ['', '_min', '_max'].forEach((x) => { if (rec['c' + c.i + x] != null) o[c.d[3] + x] = rec['c' + c.i + x]; }));
          if (Object.keys(o).length > 1) old.push(o);
        });
      } catch (e) {}
    }));
    old.sort((a, b) => a.t - b.t);
    d.coarse = old.concat(d.coarse);
  }));
}

// Series from the devices present, only for values a device has actually reported (no probe, no line).
function build() {
  const series = {}, members = {}, order = [];
  devs.forEach((d) => Object.entries(KINDS[d.kind] || {}).forEach(([key, [label, lane]]) => {
    if (!hasData(d, key)) return;
    const id = d.slug + '.' + key;
    series[id] = [`${d.title} · ${label}`, '', lane, d.title];   // grouped by device in the Custom picker (kit v14)
    order.push([id, d.kind, key, lane]);
  }));
  const panels = panelsFor(series, order);
  sig = JSON.stringify(Object.keys(series));
  if (!order.length) {
    $('chNote').textContent = 'No samples yet: the devices log once a second while they answer.';
    return;
  }
  chart = K.chart({
    series, lanes: LANES, panels,
    ranges: [[1 / 6, '10 min'], [1, '1 h'], [6, '6 h'], [24, '24 h'], [72, '3 d'], [168, '7 d']],
    range: 1,
    store: 'hubchart',
    bands: true,                                     // each step's lowest-highest around its line (kit v12)
    hold: devs.flatMap((d) => d.held.map((k) => d.slug + '.' + k)).filter((id) => series[id]),   // exact steps (kit v16)
    empty: () => 'No samples in this range yet.',
    png: { name: 'alpaca-hub' },
    csv: { name: 'alpaca-hub', cols: order.map(([id]) => [id, id.replace('.', '_')]) },
  });
  combine();
}

// The device list from the hub. When it changes (a device found or gone), or a device reports a value it hadn't
// before, the page starts over: series and colours are fixed per chart.
async function devices() {
  const h = await K.getJSON('/hub.json');
  return h.devices.filter((d) => KINDS[d.kind]).map((d) => ({ slug: d.slug, kind: d.kind, title: d.title, unit: d.unit }));
}
async function start() {
  try { devs = await devices(); } catch (e) { devs = []; }
  devs.forEach((d) => { d.keys = Object.keys(KINDS[d.kind]); d.fine = []; d.coarse = []; d.coarseH = []; d.changes = []; d.held = []; });
  $('live').style.display = devs.length ? '' : 'none';     // no device: only the saved files below
  await Promise.all(devs.map((d) => Promise.all([loadCoarse(d), loadFine(d)])));
  await loadOld();
  build();
  setInterval(async () => { await Promise.all(devs.map(loadFine)); combine(); }, 3000);
  setInterval(async () => { await Promise.all(devs.map(loadCoarse)); combine(); }, 60000);
  setInterval(async () => {
    let now;
    try { now = await devices(); } catch (e) { return; }
    const was = JSON.stringify(devs.map((d) => d.slug)), is = JSON.stringify(now.map((d) => d.slug));
    const newKeys = devs.some((d) => d.keys.some((k) => !sig.includes(`"${d.slug}.${k}"`) && hasData(d, k)));
    // While a file is open, the reload waits until it is closed.
    if (was != is || newKeys) { if (fileOpen()) stale = true; else location.reload(); }
  }, 10000);
}

// ---------------------------------------------------------------- saved history (hub/host.py: /hub/history…)
// Day files in the drivers' <unit>-history folders (hub/recorder.py writes them for every device but the box, which
// writes its own), there with or without the device; and any CSV opened from this PC (a day file, or a chart's CSV
// download), plotted below.
// Saved history's fallback names, for folders without a device.json (written before 2026-11-24, or the box's own)
const NAMES = { wbox: 'Power box', nitecrawler: 'NiteCrawler', wcover: 'Flat panel', focuslynx: 'FocusLynx', gemini: 'Gemini',
  zwoeaf: 'ZWO EAF', zwoefw: 'ZWO EFW' };
const enc = encodeURIComponent, esc = K.esc;
const size = (b) => (b >= 1048576 ? (b / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(b / 1024)) + ' kB');
let savedSig = '', stale = false;
async function saved() {
  let h;
  try { h = await K.getJSON('/hub/history.json'); } catch (e) { return; }
  const s = JSON.stringify(h), list = h.units;
  if (s == savedSig) return;
  const sel = $('hDays');
  if (![...sel.options].some((o) => +o.value == h.days)) sel.add(new Option(`${h.days} days`, h.days));   // set in hub.json
  if (document.activeElement != sel) sel.value = h.days;
  // Open folder: Explorer on the hub's PC, so only when this page is open on that PC
  const opener = (attrs) => (h.local ? `<button type="button" class="opendir" ${attrs} title="Open this folder in Explorer">Open folder</button>` : '');
  $('hFolder').innerHTML = `Files: <code>${esc(h.folder)}</code> ${opener('')}`;
  const open = new Set([...document.querySelectorAll('#savedList details[open]')].map((e) => e.dataset.u));
  savedSig = s;
  $('savedList').innerHTML = list.length ? list.map((u) => {
    const base = `/hub/history/${enc(u.driver)}/${enc(u.unit)}`, id = u.driver + '/' + u.unit;
    const files = u.files.slice().reverse();                 // newest first
    const total = u.files.reduce((a, f) => a + f.bytes, 0);
    return `<details data-u="${esc(id)}"${open.has(id) || list.length == 1 ? ' open' : ''}><summary><b>${esc(u.title || NAMES[u.driver] || u.driver)}</b> · ${esc(u.unit)}
      <span class="mut">— ${files.length} file${files.length == 1 ? '' : 's'}, ${size(total)}</span></summary>
      <p class="hrow"><a href="${base}.zip" download>Download all (.zip)</a><code class="mut">${esc(u.folder)}</code>${opener(`data-driver="${esc(u.driver)}" data-unit="${esc(u.unit)}"`)}</p>
      ${files.map((f) => `<p class="hrow"><span>${esc(f.name)}</span><span class="mut">${size(f.bytes)}</span>
        <a href="${base}/${enc(f.name)}" download>Download</a><button type="button" data-plot="${esc(base + '/' + enc(f.name))}" data-name="${esc(f.name)}" data-kind="${esc(u.driver)}">Plot</button></p>`).join('')}
    </details>`;
  }).join('') : '<p class="mut" style="margin:6px 0 0">No saved files on this PC yet.</p>';
}
$('saved').addEventListener('click', async (e) => {
  const b = e.target.closest('button.opendir');
  if (!b) return;
  const body = b.dataset.driver ? `driver=${enc(b.dataset.driver)}&unit=${enc(b.dataset.unit)}` : '';
  const r = await fetch('/hub/history/open', { method: 'POST', body,
    headers: { 'X-Hub-Page': '1', 'Content-Type': 'application/x-www-form-urlencoded' } }).catch(() => null);
  if (!r || !r.ok) alert('Cannot open the folder' + (r ? ': ' + ((await r.json().catch(() => ({}))).error || r.statusText) : ''));
});
$('savedList').onclick = async (e) => {
  const b = e.target.closest('button[data-plot]');
  if (!b) return;
  b.disabled = true;
  try {
    const r = await fetch(b.dataset.plot);
    if (!r.ok) throw new Error(await r.text());
    showFile(b.dataset.name, await r.text(), b.dataset.kind);
  } catch (err) { alert(`Cannot open ${b.dataset.name}: ${err.message || err}`); }
  b.disabled = false;
};
$('openCsv').onclick = () => $('csvFile').click();
let daysWas = null;
$('hDays').onfocus = () => { daysWas = +$('hDays').value; };
$('hDays').onchange = async () => {
  const n = +$('hDays').value;
  if (daysWas && n < daysWas && !confirm(`Keep only ${n} day${n == 1 ? '' : 's'}? Older day files of every device, the power box's too, are deleted now.`)) {
    $('hDays').value = daysWas; return;
  }
  daysWas = n;
  await fetch('/hub/history/days', { method: 'POST', headers: { 'X-Hub-Page': '1', 'Content-Type': 'application/x-www-form-urlencoded' },
    body: 'days=' + enc($('hDays').value) }).catch(() => null);
  savedSig = ''; saved();
};
$('csvFile').onchange = () => {
  const f = $('csvFile').files[0];
  $('csvFile').value = '';
  if (!f) return;
  f.text().then((t) => showFile(f.name, t), (err) => alert(`Cannot read ${f.name}: ${err.message || err}`));
};

// The box's day files: CSV header -> its history key (drivers/wbox/wbox/history.py COLS).
const BOX_COLS = { input_V: 'vin', input_A: 'iin', input_W: 'pwr', lens_C: 'lens', ambient_C: 'amb', humidity_pct: 'hum',
  dew_point_C: 'dp', heater_pct: 'dew', dc2_set_V: 'dc2', dew_target_C: 'tgt' };
const TIME_COLS = ['unix_s', 'time_utc', 'time_local', 'time_s'];
// A column of a saved file -> [label, lane, kind, key]: a box day file's header, a key of the device the hub's day
// file is from (kind), a chart download's <slug>_<key>, else a guess from a unit at the end of the name (its own
// scale when there is none).
function describe(h, i, lanes, kind) {
  if (BOX_COLS[h]) { const k = BOX_COLS[h]; return [KINDS.wbox[k][0], KINDS.wbox[k][1], 'wbox', k]; }
  if (kind && KINDS[kind] && KINDS[kind][h]) return [KINDS[kind][h][0], KINDS[kind][h][1], kind, h];
  const m = h.match(/^(.+)_([a-z0-9]+)$/);
  if (m) for (const [kind, keys] of Object.entries(KINDS)) if (keys[m[2]]) return [`${m[1]} · ${keys[m[2]][0]}`, keys[m[2]][1], kind, m[2]];
  const u = h.match(/_(C|degC|V|A|W|pct)$|(°C|%)$/);
  const lane = u ? { C: 'T', degC: 'T', '°C': 'T', V: 'V', A: 'A', W: 'W', pct: 'pct', '%': 'pct' }[u[1] || u[2]] : 'x' + i;
  if (!LANES[lane] && !lanes[lane]) lanes[lane] = { u: '', ua: h.slice(0, 12), dec: 2 };
  return [h.replace(/_/g, ' '), lane, null, h];
}
function localTime(s) {
  const m = String(s).match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?/);
  return m ? new Date(+m[1], m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0)).getTime() / 1000 : NaN;
}
function parseCsv(text, kind) {
  const split = (l) => l.split(',').map((c) => c.trim().replace(/^"(.*)"$/, '$1'));
  const lines = text.split(/\r?\n/).filter((l) => l.trim());
  if (lines.length < 2) throw new Error('there are no rows in it');
  const head = split(lines[0]);
  let ti = head.indexOf('unix_s'), tv = (v) => +v;
  if (ti < 0 && (ti = head.indexOf('time_utc')) >= 0) tv = (v) => Date.parse(v) / 1000;
  else if (ti < 0 && (ti = head.indexOf('time_local')) >= 0) tv = localTime;
  if (ti < 0) throw new Error(head.includes('time_s') ? 'its times are from the start of a run, not dates'
    : 'it has no time column (unix_s, time_utc or time_local)');
  // <col>_min / <col>_max next to <col> (day files, chart downloads): its band, not lines of their own
  const ext = (h) => { const m = h.match(/^(.+)_(min|max)$/); return m && head.includes(m[1]) ? [head.indexOf(m[1]), '_' + m[2]] : null; };
  const bands = head.map((h, i) => [i, ext(h)]).filter(([, e]) => e);
  const lanes = {}, cols = head.map((h, i) => (TIME_COLS.includes(h) || !h || ext(h) ? null : { i, h, d: describe(h, i, lanes, kind) })).filter(Boolean);
  const recs = [];
  for (const l of lines.slice(1)) {
    const c = split(l), t = tv(c[ti]);
    if (!isFinite(t)) continue;
    const r = { t };
    cols.forEach((col) => { const v = c[col.i]; if (v !== undefined && v !== '' && isFinite(+v)) r['c' + col.i] = +v; });
    bands.forEach(([i, [b, x]]) => { const v = c[i]; if (v !== undefined && v !== '' && isFinite(+v)) r['c' + b + x] = +v; });
    recs.push(r);
  }
  recs.sort((a, b) => a.t - b.t);
  const used = cols.filter((col) => recs.some((r) => r['c' + col.i] != null));   // text-only or empty columns: no line
  if (!recs.length || !used.length) throw new Error('there are no numbers in it');
  return { recs, used, lanes };
}

// One chart per opened file, each with its own element ids (fN-…): a chart can't be taken down, so the one before
// is emptied and its card hidden rather than removed (its page-wide listeners still look for its elements).
let fileN = 0, fileChart = null;
const fileOpen = () => !!fileChart;
function showFile(name, text, kind) {
  // a file opened from this PC: the device it is from by its name (<driver key>-history-…, as the hub names them)
  if (!kind) { const m = name.match(/^([a-z0-9]+)-history-/); kind = m && KINDS[m[1]] ? m[1] : null; }
  let p;
  try { p = parseCsv(text, kind); } catch (e) { alert(`Cannot plot ${name}: ${e.message}`); return; }
  closeFile(false);
  const pre = `f${++fileN}-`, card = document.createElement('section');
  card.className = 'card fcard';
  card.innerHTML = `<div class="bar"><b>${esc(name)}</b><span class="sp"></span><span class="seg" id="${pre}ranges"></span>
    <select id="${pre}panel" aria-label="Chart of the file"></select><button id="${pre}zr" style="display:none" title="Show the whole range again (or double-click the chart)">Reset zoom</button>
    <button id="${pre}brng" class="rng">Range: auto</button><button id="${pre}bpng" title="Save the chart as shown as a PNG image">PNG</button><button id="${pre}bcsv" title="The samples in view as CSV">CSV</button>
    <button class="fclose" title="Close this file">Close</button></div>
    <div class="cw"><canvas id="${pre}ch"></canvas><div class="tip" id="${pre}tip"></div></div>
    <div class="lg" id="${pre}cust" style="display:none;margin:6px 0 0"></div><div class="lg" id="${pre}lg"></div><div class="mut" id="${pre}chNote" style="font-size:12px;margin-top:4px"></div>`;
  $('saved').after(card);
  card.querySelector('.fclose').onclick = () => closeFile(true);
  const series = {}, order = [];
  p.used.forEach((col) => { const [label, lane, kind, key] = col.d; series['c' + col.i] = [label, '', lane]; order.push(['c' + col.i, kind, key, lane]); });
  const panels = panelsFor(series, order, order.length > 1 ? { all: { n: 'Everything', k: order.map(([id]) => id) } } : {});
  const stem = name.replace(/\.csv$/i, '');
  fileChart = K.chart({
    ids: pre, series, lanes: Object.assign({}, LANES, p.lanes), panels, persist: false, bands: true,
    ranges: [[0, 'All'], [1, '1 h'], [6, '6 h'], [24, '24 h'], [72, '3 d'], [168, '7 d']], range: 0,
    empty: () => 'No samples in this range.',
    png: { name: stem, title: () => name },
    csv: { name: stem, cols: p.used.map((col) => ['c' + col.i, col.h]) },
  });
  fileChart.card = card;
  fileChart.set(p.recs);
  card.scrollIntoView({ block: 'nearest' });
}
function closeFile(user) {
  if (!fileChart) return;
  fileChart.set([]);
  fileChart.card.style.display = 'none';
  fileChart = null;
  if (user && stale) location.reload();
}

saved();
setInterval(saved, 30000);
start();
