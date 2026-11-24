// Example thermometer page. Its endpoints are DeviceServer's: status.json (ThermoApp.status), POST control
// (ThermoApp.control, with the X-Hub-Page header). Paths are relative: behind the hub the page lives at /<slug>/.
K.init({ storePrefix: 'thermo-' });
const $ = K.$, f = K.f, esc = K.esc;
const row = (k, v) => `<tr><td class="mut">${k}</td><td>${v}</td></tr>`;

async function tick() {
  let s;
  try { s = await K.getJSON('status.json'); }
  catch (e) { $('sub').textContent = 'connection lost'; K.pill('link', 'hub not running', 'bad'); return; }
  K.versionWatch(s.build);
  $('name').textContent = 'Thermometer' + (s.simulated ? ' (simulated)' : '');
  $('sub').textContent = `serial ${s.serial || '?'} · driver ${s.build} · ${s.port}`;
  K.pill('link', s.up ? 'link up' : 'not answering', s.up ? 'ok' : 'bad');
  K.pill('client', s.client ? 'Alpaca client connected' : 'no Alpaca client', s.client ? 'ok' : '');
  $('tiles').innerHTML = [
    K.tile('Temperature', s.temperature == null ? '—' : f(s.temperature, 2, ' °C'), s.up ? 'reported every second' : esc(s.error || 'no reading'), s.up ? '' : 'bad'),
    K.tile('Offset', f(s.offset_c, 1, ' °C'), 'added to the device\'s reading'),
  ].join('');
  $('dt').innerHTML = row('Alpaca', `ObservingConditions · ${esc(s.name)}`) + row('UniqueID', `<span class="mut">${esc(s.uid)}</span>`) +
    row('Raw reading', f(s.raw, 2, ' °C')) + row('Lines received', s.lines) + (s.error ? row('Last problem', esc(s.error)) : '');
  const inp = $('fOff').v;
  if (document.activeElement !== inp) inp.value = s.offset_c;
}

$('fOff').onsubmit = async (e) => {
  e.preventDefault();
  const r = await fetch('control', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-Hub-Page': '1' },
    body: new URLSearchParams({ op: 'offset', value: $('fOff').v.value }).toString() }).catch(() => null);
  $('msg').textContent = r ? await r.text() : 'The hub did not answer';
  tick();
};

tick();
setInterval(tick, 2000);
