// WBPlus stock Alpaca status page (device-web-ui kit). Endpoints: see wbstock/alpaca.py (_route, do_POST).
K.init({ storePrefix: 'wbs-' });
const $ = K.$, f = K.f, esc = K.esc;
const onoff = (b) => (b ? '<span class="ok">on</span>' : '<span class="mut">off</span>');
const kb = (n) => (n >= 1e6 ? (n / 1e6).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1000)) + ' KB');
let last = null;
// Every POST from this page carries this header: a page on another site can't send it to the service (CORS preflight).
const postLocal = (url, data) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-WBStock': '1' },
  body: new URLSearchParams(data || {}).toString() });

// ---------------------------------------------------------------- live status
async function tick() {
  let s;
  try { s = await K.getJSON('status.json'); }
  catch (e) { $('sub').textContent = 'connection lost'; K.pill('link', 'service not running', 'bad'); K.pill('client', '…'); return; }
  last = s;
  K.versionWatch(s.build);
  const shown = (s.device_name_set || s.device_name_default || 'WandererBox Plus V3') + (s.simulated ? ' (simulated)' : '');
  $('name').textContent = shown;
  if (document.title !== shown) document.title = shown;  // the browser tab, too
  nameCard(s);
  wsmCard(s);
  $('sub').textContent = `${s.wsm ? 'WSM' : 'stock'} firmware · driver ${s.service} · up ${Math.round(s.up / 60)} min · ${s.port || 'searching for the box…'}`;
  K.pill('link', s.link ? 'box link up' : s.port ? 'box not responding' : 'searching for the box', s.link ? 'ok' : 'bad');
  K.pill('client', s.client ? 'Alpaca client connected' : 'no Alpaca client', s.client ? 'ok' : '');
  histCard(s);
  phone(s);
  ctl(s);
  tune(s);
  probes(s);
  remoteLock(s);
  $('aNote').textContent = `Port ${s.http_port}. Alpaca has no login: anyone on this network can switch the outputs. Settings: ${s.settings}`;

  const b = s.box;
  if (!s.link || !b) {
    $('tiles').innerHTML = K.tile('Box', '<span class="bad">no link</span>', esc(s.error || 'no status lines'));
    $('pinbar').innerHTML = '<span class="bad">box not responding</span>';
    $('boxt').innerHTML = row('Serial link', `<span class="bad">${esc(s.error || 'no status lines from the box')}</span>`) +
      row('Serial port', esc(s.port || 'searching every COM port'));
    return;
  }
  const d = s.dew, pct = Math.round(b.dew_pwm * 100 / 255);
  const pw = b.voltage != null && b.current != null ? b.voltage * b.current : null;
  const ambOk = b.ambient != null, lensOk = b.lens != null, pr = s.probes || { lens: true, ambient: true };
  const NU = '<span class="mut">not used</span>';
  const lensTxt = !pr.lens ? NU : lensOk ? f(b.lens, 1, ' °C') : '<span class="bad">MISSING</span>';
  const margin = lensOk && b.dewpoint != null ? b.lens - b.dewpoint : null;
  const w = s.wsm && s.wsm.streaming ? s.wsm : null;   // WSM with live X1 fields: real output states and protection
  const outs = w && w.act != null ? [1, 2, 4, 8].map((m) => !!(w.act & m)) : [b.usb, b.dc2, b.dc46, b.dew_pwm > 0];
  const heldTxt = w && w.shed ? `held off: ${w.shed_names.join(', ')}` : '';
  $('tiles').innerHTML = [
    K.tile('Input', f(b.voltage, 2, ' V'), `${f(b.current, 2, ' A')} · ${f(pw, 1, ' W')}`),
    K.tile('DC2', outs[1] ? f(b.dc2_volts, 1, ' V') : '<span class="mut">off</span>', s.wsm ? `set ${f(b.dc2_volts, 1, ' V')}` +
      (s.wsm.dc2_max != null ? ` · max ${f(s.wsm.dc2_max, 1, ' V')}` : '') : `setpoint ${f(b.dc2_volts, 1, ' V')} (the stock firmware doesn't measure DC2)`),
    d.mode == 4 ? K.tile('DC3', b.dew_pwm ? 'on' : '<span class="mut">off</span>', 'plain 12 V output (not a heater)')
      : K.tile('Dew heater', pct ? pct + ' %' : '<span class="mut">off</span>', esc(`${d.mode_name} · ${d.state}`)),
    K.tile('Lens', lensTxt, !pr.lens ? 'lens probe switched off in Temperature probes' : margin == null ? 'dew heater probe' : `${f(margin, 1)} °C above dew point`, margin != null && margin < 1 ? 'warn' : ''),
    K.tile('Ambient', !pr.ambient ? NU : ambOk ? f(b.ambient, 1, ' °C') : '<span class="bad">MISSING</span>',
      !pr.ambient ? 'DHT22 switched off in Temperature probes' : ambOk ? `${f(b.humidity, 0)} % · dew point ${f(b.dewpoint, 1)} °C` : 'DHT22 not reporting'),
    // On a WSM box the second line is always filled, so the tile keeps its height when something is held off.
    K.tile('Outputs', outGrid(outs, d.mode == 4), heldTxt ? `<span class="warn">${esc(heldTxt)}</span>`
      : s.wsm ? `<span class="mut">${w ? 'nothing held off' : '&nbsp;'}</span>` : ''),
  ].join('');
  // Pinned bar: 5 values on phones; from 760 px (class "w") every tile headline. Each field has a fixed width, its
  // worst case (pinW), so nothing moves when a number changes length.
  const MISS = '<span class="bad">MISSING</span>', d1 = (v, u) => (v == null ? '—' : f(v, 1, u));
  pinFill([
    ['v', 0, `<b>${f(b.voltage, 2, ' V')}</b>`],
    ['i', 0, f(b.current, 2, ' A')],
    ['p', 1, f(pw, 1, ' W')],
    ['dc2', 1, `DC2 <b>${b.dc2 ? f(b.dc2_volts, 1, ' V') : 'off'}</b>`],
    pr.lens && ['lens', 0, `lens <b>${lensTxt}</b>`],
    pr.lens && pr.ambient && ['mg', 1, `above dew pt <b>${d1(margin, ' °C')}</b>`, margin != null && margin < 1 ? 'warn' : ''],
    pr.ambient && ['amb', 1, `ambient <b>${ambOk ? f(b.ambient, 1, ' °C') + '</b> ' + f(b.humidity, 0, ' %') : MISS + '</b>'}`],
    pr.ambient && ['dp', 0, `dew pt <b>${d1(b.dewpoint, ' °C')}</b>`],
    d.mode == 4 ? ['h', 0, `DC3 <b>${b.dew_pwm ? 'on' : 'off'}</b>`] : ['h', 0, `heater <b>${pct ? pct + ' %' : 'off'}</b>`],
    ['o', 1, `outputs <b>${outs.filter((x) => x).length} of 4 on</b>`],
  ]);
  const L = s.link_stats;
  $('boxt').innerHTML = [
    row('Firmware', esc(b.fw || '?') + (s.known_firmware ? '' : ' <span class="warn">(not a version this service knows)</span>')),
    row('Outputs', `USB ${onoff(b.usb)} · DC2 ${onoff(b.dc2)} (${f(b.dc2_volts, 1, ' V')}) · DC4-6 ${onoff(b.dc46)} · ${d.mode == 4 ? 'DC3 ' + onoff(b.dew_pwm > 0) : 'dew heater ' + (pct ? pct + ' %' : onoff(false))}`),
    d.mode == 4 ? row('DC3', `plain 12 V output, ${onoff(b.dew_pwm > 0)}`)
      : row('Dew heater', esc(`${d.mode_name} mode, ${d.state}`) + (d.regulating_to != null ? ` · target ${f(d.regulating_to, 1, ' °C')}` : '')),
    row('Sensors', `lens probe ${!pr.lens ? NU : lensOk ? '<span class="ok">OK</span>' : '<span class="bad">MISSING</span>'} · DHT22 ${!pr.ambient ? NU : ambOk ? '<span class="ok">OK</span>' : '<span class="bad">MISSING</span>'}`),
    row('Serial link', `${esc(s.port)} · ${L.lines} status lines · ${L.bad} bad · ${L.resends} resends${s.queued && s.queued.length ? ` · ${s.queued.length} command${s.queued.length == 1 ? '' : 's'} waiting` : ''}`),
    row('Commands sent', `${s.eeprom_writes} <span class="mut">(each one is an EEPROM write in the box)</span>`),
    ...wsmRows(s.wsm),
  ].join('');
}
const row = (k, v, cls) => `<tr${cls ? ` class="${cls}"` : ''}><td class="mut">${k}</td><td>${v}</td></tr>`;
// WSM boxes: what the box's own protection is doing, its faults and its last restart (X1 fields, PROTOCOL.md 2.3).
function wsmRows(w) {
  if (!w) return [];
  const wait = '<span class="mut">waiting for the box…</span>', blank = '<span class="ln">&nbsp;</span>';
  if (!w.streaming) return [row('Protection', `<span class="ln">${wait}</span>`, 'h1'),
    row('Faults', `<span class="ln">${wait}</span>` + blank, 'h2'), row('Box running', `<span class="ln">${wait}</span>` + blank, 'h2')];
  // Fixed size whatever they say (one line, or two for faults and restarts; "…" past that, the full text in the
  // tooltip and the hub log), so the card never moves under the reader.
  const ln = (html, title) => `<span class="ln" title="${esc(title || html.replace(/<[^>]*>/g, ''))}">${html}</span>`;
  const held = w.shed ? `<span class="warn">holding off ${esc(w.shed_names.join(', '))}</span>` : '<span class="ok">nothing held off</span>';
  const nf = (w.faults || []).length, nl = w.latched ? (w.faults || []).filter((x) => x.includes('latched')).length : 0;
  const fl = nf ? ln(`<span class="${nl ? 'bad' : 'warn'}">${nf} fault${nf == 1 ? '' : 's'}${nl ? `, ${nl} latched` : ''}</span>` +
      ` <button type="button" class="d sm" id="wClear">Clear</button>`, w.faults.join(', ')) +
      ln(`<span class="${nl ? 'bad' : 'warn'}">${esc(w.faults.join(' · '))}</span>`)
    : ln('<span class="ok">none</span>') + ln('&nbsp;');
  const r = w.last_restart;
  return [
    row('Protection', ln(held), 'h1'),
    row('Faults', fl, 'h2'),
    row('Box running', ln(`${fmtUp(w.up_s)} · started by ${esc(w.reset_cause || '?')}`) +
      ln(r ? `<span class="warn">restarted ${new Date(r.at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} (${esc(r.cause)}) after ${fmtUp(r.after_s)}</span>` : '&nbsp;'), 'h2'),
  ];
}
const fmtUp = (s) => (s == null ? '?' : s < 120 ? `${s} s` : s < 7200 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);
document.addEventListener('click', async (e) => {
  if (e.target.id !== 'wClear') return;
  e.target.disabled = true;
  const r = await postLocal('setup/box', { action: 'clear_faults' }).catch(() => null);
  $('wMsg').textContent = r && r.ok ? (await r.text()) + '.' : 'Not cleared: ' + (r ? await r.text() : 'no answer from the service');
  await tick();
});
// The Outputs tile: the four outputs (USB, DC2, DC4-6, dew heater = DC3) in a 2x2 grid, each with a filled green dot
// when on and a hollow red ring when off. The shape differs too, so it reads in night mode and for colour-blind eyes.
const OUT_NAMES = ['USB', 'DC2', 'DC4-6', 'Dew'];
const outGrid = (outs, dc3) => '<span class="og">' + OUT_NAMES.map((n, i) => (n == 'Dew' && dc3 ? 'DC3' : n)).map((n, i) =>
  `<span class="${outs[i] ? 'on' : 'off'}" title="${n == 'Dew' ? 'Dew heater' : n == 'DC3' ? 'DC3 (12 V output)' : n} ${outs[i] ? 'on' : 'off'}"><i></i>${n}</span>`).join('') + '</span>';

// Pinned bar fields at fixed widths: each field's worst case is rendered once, off screen, in the bar's own font.
const PIN_WORST = { v: '<b>20.00 V</b>', i: '30.00 A', p: '600.0 W', dc2: 'DC2 <b>13.2 V</b>', lens: 'lens <b>-55.0 °C</b>',
  mg: 'above dew pt <b>-100.0 °C</b>', amb: 'ambient <b>-40.0 °C</b> 100 %', dp: 'dew pt <b>-60.0 °C</b>', h: 'heater <b>100 %</b>',
  o: 'outputs <b>4 of 4 on</b>' };
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
function pinFill(fields) {  // [key, wide only (from 760 px), html, extra class]; false entries are skipped
  pinW = pinW || pinWidths();
  $('pinbar').innerHTML = fields.filter(Boolean).map(([k, w, h, c]) =>
    `<span class="${[w ? 'w' : '', c || ''].join(' ').trim()}" style="width:${pinW[k]}px">${h}</span>`).join('');
}

// ---------------------------------------------------------------- history card
function histCard(s) {
  const h = s.history;
  $('hNow').innerHTML = h.files ? `${h.files} day file${h.files == 1 ? '' : 's'}, ${kb(h.bytes)}` + (h.first ? `, chart from ${esc(K.stampT(h.first))}` : '') + '.'
    : 'No history files yet: the first one is written a minute after the box link comes up.';
  if (!h.write_ok) $('hNow').innerHTML += ' <span class="bad">Writing the files failed: see the log.</span>';
  $('hLocal').style.display = s.local ? '' : 'none';
  $('hf').days.disabled = $('hSave').disabled = !s.local;
  if (document.activeElement !== $('hf').days && !$('hf').days.dataset.edited) $('hf').days.value = h.days;
  $('hNote').textContent = (s.local ? `Files: ${h.folder}. One CSV per day (1-minute averages with their lowest and highest), older ones are deleted automatically. ` +
    'Delete history removes only these files, not the settings or the diagnostic log.'
    : 'Open data folder, Delete history and the retention setting only work on the PC the box is plugged into.') +
    ' The chart shows up to 24 hours since the hub started; the Overview reads the files back.';
}
$('hf').days.oninput = () => { $('hf').days.dataset.edited = '1'; };
$('hf').onsubmit = async (e) => {
  e.preventDefault();
  const n = +$('hf').days.value, h = last && last.history;
  if (!(n >= 1 && n <= 365)) { $('hMsg').textContent = 'Pick 1 to 365 days.'; return; }
  if (h && n < h.days && !confirm(`Keep only ${n} day${n == 1 ? '' : 's'}? Older day files are deleted now.`)) return;
  const r = await postLocal('setup/history', { days: n }).catch(() => null);
  $('hMsg').textContent = r && r.ok ? 'Saved.' : 'Not saved: ' + (r ? await r.text() : 'no answer');
  delete $('hf').days.dataset.edited;
  tick(); loadCoarse(true);
};
$('hOpen').onclick = async () => {
  const r = await postLocal('control/open-folder').catch(() => null);
  $('hMsg').textContent = r && r.ok ? 'Opened in Explorer.' : 'Could not open the folder: ' + (r ? await r.text() : 'no answer');
};
$('hDel').onclick = async () => {
  const h = last && last.history;
  const what = h && h.files ? `${h.files} day file${h.files == 1 ? '' : 's'} (${kb(h.bytes)})` : 'the history';
  if (!confirm(`Delete ${what} and empty the chart?\n\nThis can't be undone. Settings and the diagnostic log are kept.`)) return;
  const r = await postLocal('control/delete-history').catch(() => null);
  $('hMsg').textContent = r && r.ok ? await r.text() : 'Not deleted: ' + (r ? await r.text() : 'no answer');
  fine = []; coarse = []; combine(); tick();
};
$('hZip').onclick = () => { location.href = 'history.zip'; };

// ---------------------------------------------------------------- controls (Alpaca switch ids 0-7)
// While an Alpaca client has the Switch connected they're locked; Unlock (with a confirmation) lasts until that
// client disconnects or the page reloads. The service checks the same thing (override=1, else 409).
let unlocked = false, busy = false;
const cf = $('cf'), tf = $('tf');
const OUTS = [[0, 'USB ports', 'usb'], [1, 'DC2', 'dc2'], [3, 'DC4-6', 'dc46']];
const FIELDS = { v2: [2, 'DC2 voltage', ' V'], v4: [4, 'dew heater mode', ''], v5: [5, 'manual power', ' %'], v6: [6, 'target temperature', ' °C'], v7: [7, 'dew point offset', ' °C'] };
$('cOut').innerHTML = OUTS.map(([id, n]) => `<div class="crow"><span>${n}</span><span class="seg" data-id="${id}">` +
  '<button type="button" data-v="1">On</button><button type="button" data-v="0">Off</button></span></div>').join('');
// Switching an output off can cut a running imaging session, so it asks first (per browser, on by default).
const OFFNOTE = { 0: 'The camera, focuser and anything else on the USB ports lose power and disconnect.',
  1: 'Whatever is plugged into DC2 loses power.', 3: 'Whatever is plugged into DC4-6 (often the mount or a camera cooler) loses power.' };
const askOff = K.askOff($('cAsk'));  // kit v5
const curVals = (s) => ({ v2: s.box ? s.box.dc2_volts : null, v4: s.dew.mode, v5: Math.round(s.dew.manual_pct), v6: s.dew.target_c, v7: s.dew.offset_c });
// Edited fields keep the user's value while polling refreshes the rest (kit v5 K.edits).
const ED = new Map([[cf, K.edits(cf, (d) => { $('cRevert').style.display = d ? '' : 'none'; })], [tf, K.edits(tf)]]);
const fill = (form, vals) => ED.get(form).fill(vals), clearEdits = (form) => ED.get(form).clear();

function ctl(s) {
  if (!s.switch_client) unlocked = false;  // the lock comes back for the next connection
  const b = s.box, up = !!(s.link && b), off = (s.switch_client && !unlocked) || !up || busy;
  $('cLock').classList.toggle('on', s.switch_client && !unlocked);
  $('cLockTxt').textContent = !s.switch_client ? 'No Alpaca client connected: the controls are free.'
    : unlocked ? 'Unlocked while an Alpaca program is connected.' : 'An Alpaca program is connected. Change things there, or unlock.';
  $('cUnlock').textContent = unlocked ? 'Lock' : 'Unlock';
  $('cUnlock').style.visibility = s.switch_client ? '' : 'hidden';
  for (const [id, , k] of OUTS) {
    $('cOut').querySelector(`[data-id="${id}"]`).querySelectorAll('button').forEach((x) => {
      x.className = up && (x.dataset.v == '1') == b[k] ? 'on' : ''; x.disabled = off;
    });
  }
  // mode 4: DC3 is a plain 12 V output; its On/Off row takes the Manual power row's place (same height)
  const dc3 = s.dew.mode == 4;
  $('dc3Row').style.display = dc3 ? '' : 'none'; $('manRow').style.display = dc3 ? 'none' : '';
  $('dc3Seg').querySelectorAll('button').forEach((x) => { x.className = up && (x.dataset.v == '1') == !!s.dew.dc3_on ? 'on' : ''; x.disabled = off; });
  fill(cf, curVals(s));
  cf.querySelectorAll('input:not(#cAsk),select,#cApply').forEach((x) => { x.disabled = off; });
  const m = $('cMsg');
  if (!up) { m.textContent = 'No box link: the controls come back when the box answers.'; m.dataset.auto = '1'; }
  else if (m.dataset.auto) { m.textContent = ''; delete m.dataset.auto; }
}
async function run(steps) {  // [[id, value, label]], one after the other; stops at the first refusal
  busy = true; if (last) ctl(last);
  let ok = true, msg = '';
  for (const [id, v, label] of steps) {
    $('cMsg').textContent = `${label}: waiting for the box…`;
    const r = await postLocal('control/box', { id, value: v, override: unlocked ? 1 : 0 }).catch(() => null);
    if (!r || !r.ok) { ok = false; msg = `${label}: not changed. ${r ? await r.text() : 'No answer from the service.'}`; break; }
  }
  busy = false;
  delete $('cMsg').dataset.auto; $('cMsg').textContent = ok ? 'Done: ' + steps.map((x) => x[2]).join(', ') + '.' : msg;
  await tick();
  return ok;
}
$('cOut').onclick = (e) => {
  const x = e.target.closest('button'), b = last && last.box;
  if (!x || x.disabled || !b) return;
  const id = +x.closest('.seg').dataset.id, on = x.dataset.v == '1', o = OUTS.find((q) => q[0] == id);
  if (b[o[2]] == on) return;
  if (!on && !askOff(o[1], OFFNOTE[id])) return;
  if (id == 1 && on && !confirm(`Switch DC2 on at ${f(b.dc2_volts, 1)} V?\n\nCheck that what's plugged into DC2 takes this voltage.`)) return;
  run([[id, on ? 1 : 0, `${o[1]} ${on ? 'on' : 'off'}`]]);
};
cf.onsubmit = async (e) => {
  e.preventDefault();
  const b = last && last.box; if (!b) return;
  const cur = curVals(last), steps = [];
  for (const k of ['v2', 'v6', 'v7', 'v5', 'v4']) {  // the mode last, so the values it uses are already in place
    const el = cf.elements[k], [id, name, unit] = FIELDS[k];
    if (!el.dataset.edited) continue;
    const v = +el.value;
    if (el.value === '' || !isFinite(v)) { $('cMsg').textContent = `${name}: enter a number.`; return; }
    if ((el.min !== '' && v < +el.min) || (el.max !== '' && v > +el.max)) { $('cMsg').textContent = limitNote(k) || `${name}: ${el.min} to ${el.max}.`; return; }
    if (v != cur[k]) steps.push([id, v, `${name} ${k == 'v4' ? el.options[el.selectedIndex].text : el.value + unit}`]);
  }
  if (!steps.length) { clearEdits(cf); fill(cf, cur); $('cMsg').textContent = 'Nothing to change.'; return; }
  const m4 = steps.find((x) => x[0] == 4 && x[1] == 4);
  if (m4 && !confirm('Use DC3 as a plain 12 V output?\n\n' + (last.dew.dc3_on
    ? 'DC3 switches fully on (the input voltage, about 12 V) at once, with no power limit. Check that what is plugged into DC3 takes 12 V.'
    : 'DC3 stays off until you switch it on.') + ' The dew heater settings are kept for when you switch back.')) return;
  const dv = steps.find((x) => x[0] == 2);
  if (dv && !confirm(`Change DC2 from ${f(b.dc2_volts, 1)} V to ${f(dv[1], 1)} V?\n\n` + (b.dc2
    ? 'DC2 is on: the new voltage reaches whatever is plugged into it at once.' : 'DC2 is off and stays off. The new voltage applies when it is switched on.'))) return;
  if (await run(steps)) { clearEdits(cf); fill(cf, curVals(last)); }
};
// Settings capped by another setting say so where the cap bites (device-web-ui GUIDELINES, "Limits set elsewhere").
function limitNote(k) {
  const el = cf.elements[k];
  if (k == 'v5' && el.max !== '' && +el.max < 100)
    return `Manual power: at most ${el.max} %, set by the Power limit in the Dew controller card.`;
  return '';
}
cf.elements.v5.addEventListener('input', () => {
  const el = cf.elements.v5, n = limitNote('v5');
  if (n && el.value !== '' && +el.value >= +el.max) { $('cMsg').textContent = n; delete $('cMsg').dataset.auto; }
});
$('dc3Seg').onclick = (e) => {
  const x = e.target.closest('button'); if (!x || x.disabled || !last) return;
  const on = x.dataset.v == '1'; if (!!last.dew.dc3_on == on) return;
  if (!on && !askOff('DC3', 'Whatever is plugged into DC3 loses power.')) return;
  run([[5, on ? 100 : 0, `DC3 ${on ? 'on' : 'off'}`]]);
};
$('cRevert').onclick = () => { clearEdits(cf); if (last) fill(cf, curVals(last)); $('cMsg').textContent = ''; };
$('cUnlock').onclick = () => {
  if (unlocked) unlocked = false;
  else if (confirm('Unlock the controls while an Alpaca client is connected?\n\nA change here can upset a running imaging sequence. ' +
    'The lock comes back when the client disconnects or this page reloads.')) unlocked = true;
  if (last) ctl(last);
};

// ---------------------------------------------------------------- settings: only on the PC the box is plugged into
// (the service refuses them from elsewhere with 403). Phones and other computers see them read-only.
const SETTINGS_FORMS = ['tf', 'prf', 'nf', 'gf', 'pf', 'wf'];
function remoteLock(s) {
  const ro = !s.local;
  $('rNote').style.display = ro ? '' : 'none';
  for (const id of SETTINGS_FORMS)
    for (const el of $(id).querySelectorAll('input,select,button')) {
      if (ro) { el.disabled = true; el.dataset.ro = '1'; }
      else if (el.dataset.ro) { delete el.dataset.ro; el.disabled = false; }
    }
  if (!ro) { probes(s); const gm = $('gf').elements; gm.missing_value.disabled = gm.missing.value == 'minimum'; $('nReset').disabled = !s.device_name_set; }
}

// ---------------------------------------------------------------- WSM firmware settings (stored in the box; shown only for a WSM box)
const W_FIELDS = { dc2_max: ['DC2 ceiling', 'V'], dc2_slew: ['DC2 ramp', 'V/s'] };
function wsmCard(s) {
  $('wGrp').style.display = s.wsm ? '' : 'none';
  if (!s.wsm) return;
  for (const k of Object.keys(W_FIELDS)) {
    const el = $('wf').elements[k], v = s.wsm[k];
    el.placeholder = v == null ? 'reading…' : '';
    if (document.activeElement !== el && !el.dataset.edited) el.value = v == null ? '' : v;
  }
}
for (const k of Object.keys(W_FIELDS)) $('wf').elements[k].oninput = () => { $('wf').elements[k].dataset.edited = '1'; };
$('wf').onsubmit = async (e) => {
  e.preventDefault();
  const data = {};
  for (const [k, [name, unit]] of Object.entries(W_FIELDS)) {
    const el = $('wf').elements[k], v = +el.value;
    if (!el.dataset.edited) continue;
    if (el.value === '' || !isFinite(v) || v < +el.min || v > +el.max) { $('wMsg').textContent = `${name}: ${el.min} to ${el.max} ${unit} (the box's firmware limits).`; return; }
    data[k] = v;
  }
  if (!Object.keys(data).length) { $('wMsg').textContent = 'Nothing to save.'; return; }
  if (data.dc2_max != null && last && last.box && last.box.dc2_volts > data.dc2_max + 0.05 &&
    !confirm(`DC2 is set to ${last.box.dc2_volts} V. A ceiling of ${data.dc2_max} V lowers it to ${data.dc2_max} V` +
      (last.box.dc2 ? ', and DC2 is on now.' : '.'))) return;
  $('wMsg').textContent = 'Waiting for the box…';
  const r = await postLocal('setup/box', data).catch(() => null);
  $('wMsg').textContent = r && r.ok ? (await r.text()) + '.' : 'Not saved: ' + (r ? await r.text() : 'no answer from the service');
  if (r && r.ok) for (const k of Object.keys(data)) delete $('wf').elements[k].dataset.edited;
  await tick();
};

// ---------------------------------------------------------------- Alpaca device name (shared networks)
function nameCard(s) {
  const el = $('nf').elements.name, n = s.device_name_set || s.device_name_default;
  el.placeholder = s.device_name_default;
  if (document.activeElement !== el && !el.dataset.edited) el.value = s.device_name_set || '';
  $('nOut').textContent = $('nSen').textContent = s.device_name || n;
  $('nReset').disabled = !s.device_name_set;
}
$('nf').elements.name.oninput = () => { $('nf').elements.name.dataset.edited = '1'; };
async function saveName(name) {
  const r = await postLocal('setup/name', { name }).catch(() => null);
  $('nMsg').textContent = r && r.ok ? (await r.text()) + '.' : 'Not saved: ' + (r ? await r.text() : 'no answer from the service');
  if (r && r.ok) delete $('nf').elements.name.dataset.edited;
  await tick();
}
$('nf').onsubmit = (e) => { e.preventDefault(); saveName($('nf').elements.name.value); };
$('nReset').onclick = () => { $('nf').elements.name.value = ''; saveName(''); };

// ---------------------------------------------------------------- temperature probes (not an Alpaca switch: never locked)
// A probe set to not used reads as "not used" everywhere; the dew modes that need it can't be picked.
const MODE_NEEDS = { 2: ['lens'], 3: ['lens', 'ambient'] };
let prBusy = false;
function probes(s) {
  const pr = s.probes || { lens: true, ambient: true };
  if (!prBusy) { $('pLens').checked = pr.lens; $('pAmb').checked = pr.ambient; }
  for (const o of cf.elements.v4.options) {
    const need = (MODE_NEEDS[o.value] || []).filter((k) => !pr[k]);
    o.disabled = need.length > 0;
    if (!o.dataset.n) o.dataset.n = o.text;
    const what = need.map((x) => (x == 'lens' ? 'lens probe' : 'DHT22')).join(' and ');
    o.text = need.length ? `${o.dataset.n} (needs the ${what})` : o.dataset.n;
    o.title = need.length ? `Needs the ${what}: switched off in Temperature probes` : '';
  }
  for (const el of $('gauges').querySelectorAll('input')) {
    const off = (el.dataset.needs || '').split(' ').filter((k) => k && !pr[k]).length > 0;
    el.disabled = off; el.closest('label').classList.toggle('mut', off);
  }
}
$('prf').onchange = async () => {
  prBusy = true;
  const r = await postLocal('setup/probes', { lens: $('pLens').checked ? 1 : 0, ambient: $('pAmb').checked ? 1 : 0 }).catch(() => null);
  $('prMsg').textContent = r && r.ok ? (await r.text()) + '.' : 'Not changed: ' + (r ? await r.text() : 'no answer from the service');
  prBusy = false;
  await tick();
};

// ---------------------------------------------------------------- dew controller tuning (not an Alpaca switch: never locked)
function tune(s) { fill(tf, s.dew.tuning || {}); }
const tName = (el) => el.closest('label').firstChild.textContent.trim();
tf.onsubmit = async (e) => {
  e.preventDefault();
  const data = {};
  for (const el of tf.querySelectorAll('input[data-edited]')) {
    const v = +el.value;
    if (el.value === '' || !isFinite(v) || v < +el.min || v > +el.max) { $('tMsg').textContent = `${tName(el)}: ${el.min} to ${el.max}.`; return; }
    data[el.name] = v;
  }
  if (!Object.keys(data).length) { $('tMsg').textContent = 'Nothing to save.'; return; }
  if (data.min_interval_s != null && data.min_interval_s < 30 && !confirm(`Allow an automatic heater command every ${data.min_interval_s} s?\n\n` +
    "Every command is a write to the box's EEPROM, which wears out after about 100,000 writes.")) return;
  const r = await postLocal('setup/dew', data).catch(() => null);
  $('tMsg').textContent = r && r.ok ? (await r.text()) + '.' : 'Not saved: ' + (r ? await r.text() : 'no answer from the service');
  if (r && r.ok) { clearEdits(tf); await tick(); loadSetup(); }
};
$('tReset').onclick = async () => {
  if (!confirm('Put every dew controller setting back to its default?')) return;
  const r = await postLocal('setup/dew', { reset: 1 }).catch(() => null);
  $('tMsg').textContent = r && r.ok ? (await r.text()) + '.' : 'Not reset: ' + (r ? await r.text() : 'no answer from the service');
  clearEdits(tf); await tick(); loadSetup();
};

// ---------------------------------------------------------------- phone card
let qrFor = null;
function phone(s) {
  $('d-phone').style.display = s.local ? '' : 'none';  // only useful on the PC itself
  const u = s.urls || [];
  if (!u.length) {
    $('pNow').textContent = s.bind == '127.0.0.1' ? 'The service only listens on this PC (bind 127.0.0.1), so phones can\'t reach it.' : 'This PC has no network address right now.';
    $('pQr').innerHTML = ''; qrFor = null; return;
  }
  $('pNow').innerHTML = 'Scan the code, or type the address into the phone\'s browser: ' + u.map((x) => `<b>${esc(x)}</b>`).join(' or ');
  if (qrFor != u[0]) { qrFor = u[0]; $('pQr').innerHTML = `<img class="qr" src="qr.svg?i=0&amp;v=${encodeURIComponent(u[0])}" alt="QR code for ${esc(u[0])}" width="180" height="180">`; }
}

// ---------------------------------------------------------------- settings cards (loaded once)
async function loadSetup() {
  let c;
  try { c = await K.getJSON('setup.json'); } catch (e) { return; }
  $('ports').innerHTML = c.ports.map((p) => `<option value="${esc(p)}"${p == c.port ? ' selected' : ''}>${p == 'auto' ? 'Find automatically' : esc(p)}</option>`).join('');
  for (const x of c.controls || []) {
    const el = cf.elements['v' + x.id];
    if (el && el.type == 'number') { el.min = x.min; el.max = x.max; el.closest('label').title = x.desc; }
  }
  for (const [k, t] of Object.entries(c.tuning || {})) {
    const el = tf.elements[k];
    if (el) { el.min = t.min; el.max = t.max; el.placeholder = t.default; }
  }
  $('gauges').innerHTML = c.gauges.map((g) => `<label title="${esc(g.desc)}"><span>${esc(g.name)}</span><input type="checkbox" name="gauge" value="${esc(g.key)}" data-needs="${esc((g.needs || []).join(' '))}"${g.on ? ' checked' : ''}></label>`).join('');
  if (last) probes(last);
  const gm = $('gf').elements, mm = c.missing || { mode: 'hold', value: 0 };
  gm.missing.value = mm.mode; gm.missing_value.value = mm.value;
  const fixedOn = () => { gm.missing_value.disabled = gm.missing.value == 'minimum'; };
  gm.missing.onchange = fixedOn; fixedOn();
  K.stepper(gm.missing_value);
}

// ---------------------------------------------------------------- chart
const chart = K.chart({
  series: { vin: ['Input voltage', '--c1', 'V'], dc2: ['DC2 setpoint', '--c4', 'V'], iin: ['Input current', '--c10', 'A'], pwr: ['Input power', '--c2', 'W'],
    lens: ['Lens', '--c3', 'T'], amb: ['Ambient', '--c5', 'T'], dp: ['Dew point', '--c9', 'T'], tgt: ['Dew target', '--c8', 'T'],
    hum: ['Humidity', '--c7', 'pct'], dew: ['DC3/Dew', '--c6', 'pct'] },  // the DC3 jack: heater power, or 100/0 in mode 4
  lanes: { V: { u: 'V', dec: 2, minSpan: 0.2 }, A: { u: 'A', dec: 2, min: 0, minSpan: 0.5 }, W: { u: 'W', dec: 1, min: 0, minSpan: 2 },
    T: { u: '°C', dec: 1, minSpan: 2 }, pct: { u: '%', dec: 0, min: 0, max: 100 } },
  panels: { dew: { n: 'Dew: lens, ambient, dew point, DC3/Dew', k: ['lens', 'amb', 'dp', 'tgt', 'dew'] }, power: { n: 'Power: voltage, current, watts', k: ['vin', 'iin', 'pwr'] },
    volt: { n: 'Voltages: input and DC2 setpoint', k: ['vin', 'dc2'] }, hum: { n: 'Humidity and DC3/Dew', k: ['hum', 'dew'] }, custom: { n: 'Custom…' } },
  ranges: [[1 / 6, '10 min'], [1, '1 h'], [6, '6 h'], [24, '24 h']],
  range: 6,
  gapS: 150,
  store: 'chart',
  bands: true,                     // each 10 s step's lowest-highest around its line (kit v12)
  empty: () => 'No samples yet: the service logs once a second while the box link is up.',
  png: { name: 'wbplus' },
  csv: { name: 'wbplus', cols: [['vin', 'input_V'], ['iin', 'input_A'], ['pwr', 'input_W'], ['dc2', 'dc2_set_V'], ['lens', 'lens_C'], ['amb', 'ambient_C'],
    ['dp', 'dew_point_C'], ['tgt', 'dew_target_C'], ['hum', 'humidity_pct'], ['dew', 'heater_pct']] },
});

// 1 s samples (last 10 min) and 10-second averages with their lowest and highest (24 h), both timestamped by the PC's clock.
// Both are fetched incrementally (?since=), the minutes once a minute and the seconds every 5 s.
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
async function loadCoarse(all) {
  try {
    const since = !all && coarse.length ? coarse[coarse.length - 1].t : 0;
    const add = recs(await K.getJSON('history.json?res=min' + (since ? '&since=' + since : ''), 30000));
    coarse = since ? coarse.concat(add) : add;
    combine();
  } catch (e) {}
}

tick(); setInterval(tick, 2000); loadSetup();
loadCoarse(true).then(loadFine); setInterval(loadFine, 5000); setInterval(() => loadCoarse(false), 60000);
