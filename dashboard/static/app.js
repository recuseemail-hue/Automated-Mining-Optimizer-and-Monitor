/* rig-overseer dashboard client. No build step, no external calls; everything comes from /api/*. */
(function () {
  'use strict';
  const $ = (s, el = document) => el.querySelector(s);
  const h = (tag, attrs = {}, ...kids) => {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class') el.className = v; else if (k === 'html') el.innerHTML = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v); else if (v != null) el.setAttribute(k, v);
    }
    for (const k of kids.flat()) if (k != null) el.append(k.nodeType ? k : document.createTextNode(String(k)));
    return el;
  };
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  async function api(path) {
    const r = await fetch(path, { credentials: 'same-origin', headers: { Accept: 'application/json' } });
    if (r.status === 401) { location.href = '/login?next=' + encodeURIComponent(location.pathname); return null; }
    if (!r.ok) throw new Error(path + ' -> ' + r.status);
    return r.json();
  }

  // ---- formatting -----------------------------------------------------------------------------
  const HR_UNITS = [[1e12, 'TH/s'], [1e9, 'GH/s'], [1e6, 'MH/s'], [1e3, 'kH/s'], [1, 'H/s']];
  function hrUnit(max) { for (const [f, u] of HR_UNITS) if (max >= f) return [f, u]; return [1, 'H/s']; }
  const fmtHR = v => { if (v == null) return '—'; const [f, u] = hrUnit(v); return (v / f).toFixed(v / f >= 100 ? 0 : 2) + ' ' + u; };
  const fmtW = v => v == null ? '—' : Math.round(v) + ' W';
  const fmtC = v => v == null ? '—' : Math.round(v) + ' °C';
  const fmtPct = v => v == null ? '—' : Math.round(v) + ' %';
  const fmtNum = (v, d = 0) => v == null ? '—' : Number(v).toLocaleString(undefined, { maximumFractionDigits: d });
  const fmtUSD = v => v == null ? '—' : '$' + Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const fmtDur = s => { if (s == null) return '—'; s = Math.round(s); const d = Math.floor(s / 86400), hh = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    return d ? `${d}d ${hh}h` : hh ? `${hh}h ${m}m` : `${m}m`; };
  const fmtAge = ts => ts == null ? '—' : fmtDur(Date.now() / 1000 - ts) + ' ago';
  const fmtTime = ts => ts == null ? '—' : new Date(ts * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  const fmtEff = (hr, w) => (hr && w) ? fmtHR(hr / w).replace('/s', '/s/W') : '—';
  const sevClass = s => ({ critical: 'crit', high: 'serious', medium: 'warn', low: '' }[s] || 'warn');
  const sevIcon = s => ({ critical: '⛔', high: '⚠', medium: '△' }[s] || 'ℹ');

  // ---- theme & clock -----------------------------------------------------------------------------
  const themeBtn = $('#theme');
  if (themeBtn) themeBtn.onclick = () => {
    const cur = document.documentElement.dataset.theme || 'auto';
    const next = { auto: 'dark', dark: 'light', light: 'auto' }[cur];
    if (next === 'auto') delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = next;
    try { localStorage.setItem('theme', next); } catch (e) { /* private mode */ }
    document.dispatchEvent(new Event('themechange'));
  };
  const clock = $('#clock');
  if (clock) setInterval(() => { clock.textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); }, 1000);

  // ---- shared renderers ----------------------------------------------------------------------------
  function gpuCard(g) {
    const s = g.sample || {};
    const stale = s.ts && (Date.now() / 1000 - s.ts) > 600;
    const hot = s.core_c >= 85 || s.memj_c >= 105 ? 'crit' : (s.core_c >= 75 || s.memj_c >= 100 ? 'hot' : '');
    const badges = [];
    if (g.ladder) badges.push(h('span', { class: 'badge serious' }, '⚠ ' + g.ladder));
    if (stale) badges.push(h('span', { class: 'badge warn' }, '⏱ stale'));
    if (s.extra && s.extra.paused) badges.push(h('span', { class: 'badge' }, 'paused'));
    if (g.open_trial) badges.push(h('span', { class: 'badge' }, 'trial'));
    if (g.profile && g.profile.status === 'converged') badges.push(h('span', { class: 'badge good' }, '✓ converged'));
    const kv = (label, val) => h('div', {}, h('span', {}, label), h('b', {}, val));
    return h('a', { class: 'gpu', href: '/gpu/' + g.id },
      h('div', { class: 'name' }, h('b', {}, `${g.host} · GPU ${g.idx}`), h('span', { class: 'muted' }, (g.model || '').replace('NVIDIA GeForce ', ''))),
      badges.length ? h('div', { class: 'badges', style: 'justify-content:flex-start;margin-bottom:6px' }, badges) : null,
      h('div', { class: 'kv' },
        kv('hashrate', fmtHR(s.hashrate)), kv('power', fmtW(s.power_w) + (s.pl_w ? ' / ' + Math.round(s.pl_w) : '')), kv('efficiency', fmtEff(s.hashrate, s.power_w)),
        kv('core', fmtC(s.core_c)), kv('memory', fmtC(s.memj_c)), kv('hot spot', fmtC((s.extra || {}).hotspot_c)),
        kv('fan', fmtPct(s.fan_pct)),
        kv('core clock', fmtNum(s.core_mhz) + (s.core_offset_mhz ? ` (${s.core_offset_mhz > 0 ? '+' : ''}${s.core_offset_mhz})` : '')),
        kv('mem clock', fmtNum(s.mem_mhz) + (s.mem_offset_mhz ? ` (+${s.mem_offset_mhz})` : '')),
        kv('shares', `${fmtNum(s.accepted)} / ${fmtNum(s.rejected)}`)),
      h('div', { class: 'meter ' + hot, title: 'core temperature vs 95 °C hard limit' }, h('i', { style: `width:${Math.min(100, (s.core_c || 0) / 95 * 100)}%` })));
  }

  function alertRow(a) {
    return h('div', { class: 'row' }, h('span', { class: 'badge ' + sevClass(a.severity) }, sevIcon(a.severity) + ' ' + a.rule),
      h('div', {}, a.summary || '', a.id ? h('div', { class: 'muted', style: 'font-size:.8rem' }, 'incident #' + a.id) : null),
      h('span', { class: 't' }, a.since ? fmtAge(a.since) : ''));
  }

  // ---- charts (uPlot) -------------------------------------------------------------------------------
  const charts = [];
  function mkChart(parent, spec) {
    const box = h('div', { class: 'chart' }, h('h3', {}, spec.title, h('small', {}, spec.unit || '')));
    const plot = h('div', { class: 'plot' });
    box.append(plot);
    // one legend per chart; it is also the live readout for the crosshair (no separate tooltip layer)
    const legend = h('div', { class: 'legend' }, h('span', { class: 'when muted' }, ''),
      ...spec.series.map((s, i) => h('span', { class: 'item' }, h('i', { style: 'background:' + css(s.color) }), s.label, ' ', h('b', { 'data-i': i }, '—'))));
    box.append(legend);
    const table = h('div', { class: 'table', hidden: true });
    box.append(table);
    parent.append(box);
    const state = { u: null, spec, box, plot, table, legend, data: null };
    charts.push(state);
    return state;
  }
  function readout(st, idx) {
    const d = st.data; if (!d) return;
    const i = idx == null ? d[0].length - 1 : idx;
    $('.when', st.legend).textContent = idx == null ? 'latest' : fmtTime(d[0][i]);
    st.legend.querySelectorAll('b[data-i]').forEach(b => { const v = d[+b.dataset.i + 1][i];
      b.textContent = v == null ? '—' : (st.spec.digits != null ? v.toFixed(st.spec.digits) : v.toFixed(v < 10 ? 2 : 1)); });
  }
  function drawChart(st, cols) {
    const { spec, plot } = st;
    let ys = spec.series.map(s => cols[s.key] || []);
    let unit = spec.unit, scale = 1;
    if (spec.auto === 'hashrate') { const mx = Math.max(0, ...ys.flat().filter(v => v != null)); [scale, unit] = hrUnit(mx || 1); }
    if (spec.auto === 'eff') { const mx = Math.max(0, ...ys.flat().filter(v => v != null)); [scale, unit] = hrUnit(mx || 1); unit = unit.replace('/s', '/s/W'); }
    if (scale !== 1) ys = ys.map(a => a.map(v => v == null ? null : v / scale));
    const data = [cols.ts, ...ys];
    st.data = data; st.unit = unit;
    $('small', st.box).textContent = unit || '';
    if (st.u) { st.u.destroy(); st.u = null; }
    plot.innerHTML = '';
    if (!cols.ts || cols.ts.length < 2) { plot.append(h('div', { class: 'empty' }, 'no data in range')); st.box.classList.remove('loading'); renderTable(st); return; }
    const ink2 = css('--ink-2'), grid = css('--grid'), axis = css('--axis');
    const opts = {
      width: plot.clientWidth || 320, height: 200,
      cursor: { points: { size: 8, fill: (u, i) => css(spec.series[i - 1].color), stroke: css('--surface'), width: 2 }, drag: { x: true, y: false } },
      legend: { show: false },
      hooks: { setCursor: [u => readout(st, u.cursor.idx)] },
      scales: { x: { time: true }, y: { range: (u, mn, mx) => {
        // a constant series (fixed fan %, locked clock, 100 % util) must still get a non-zero span or uPlot's tick generator fails
        if (mn == null || mx == null || !isFinite(mn) || !isFinite(mx)) return [0, 1];
        if (spec.zero) return [0, mx > 0 ? mx * 1.08 : 1];
        let pad = (mx - mn) * .1; if (!(pad > 0)) pad = Math.abs(mx) * 0.01 || 1;
        return [mn - pad, mx + pad];
      } } },
      axes: [
        { stroke: ink2, grid: { show: false }, ticks: { stroke: axis, width: 1 }, font: '11px system-ui', gap: 6 },
        { stroke: ink2, grid: { stroke: grid, width: 1 }, ticks: { show: false }, font: '11px system-ui', size: 48, gap: 6,
          values: (u, vals) => vals.map(v => v >= 10000 ? (v / 1000).toFixed(0) + 'K' : (Number.isInteger(v) ? v : v.toFixed(v < 10 ? 2 : 1))) },
      ],
      series: [{ label: 'time', value: (u, v) => v == null ? '' : new Date(v * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) },
        ...spec.series.map(s => ({ label: s.label, stroke: css(s.color), width: 2, points: { show: false }, spanGaps: false,
          fill: spec.series.length === 1 && spec.area ? css(s.color) + '1a' : undefined,
          value: (u, v) => v == null ? '—' : (spec.digits != null ? v.toFixed(spec.digits) : v.toFixed(v < 10 ? 2 : 1)) + (unit ? ' ' + unit : '') }))],
    };
    st.u = new uPlot(opts, data, plot);
    readout(st, null);
    st.box.classList.remove('loading');
    renderTable(st);
  }
  function renderTable(st) {
    const t = st.table; t.innerHTML = '';
    if (t.hidden || !st.data) return;
    const n = st.data[0].length, step = Math.max(1, Math.floor(n / 24));
    const tbl = h('table', {}, h('tr', {}, h('th', {}, 'time'), ...st.spec.series.map(s => h('th', {}, s.label + (st.unit ? ` (${st.unit})` : '')))));
    for (let i = n - 1; i >= 0; i -= step) tbl.append(h('tr', {}, h('td', {}, fmtTime(st.data[0][i])), ...st.data.slice(1).map(a => h('td', {}, a[i] == null ? '—' : a[i].toFixed(2)))));
    t.append(tbl);
  }
  new ResizeObserver(() => charts.forEach(st => st.u && st.u.setSize({ width: st.plot.clientWidth, height: 200 }))).observe(document.body);
  document.addEventListener('themechange', () => charts.forEach(st => { if (!st.data) return;
    try { drawChart(st, Object.fromEntries([['ts', st.data[0]], ...st.spec.series.map((s, i) => [s.key, st.data[i + 1].map(v => v)])])); } catch (e) { console.error(e); } }));

  const RO = {};

  // ---- Overview -------------------------------------------------------------------------------------
  RO.overview = function () {
    async function load() {
      const o = await api('/api/overview'); if (!o) return;
      $('#hero-hr').textContent = fmtHR(o.total_hashrate);
      $('#hero-sub').textContent = `${o.coin || '—'} · ${o.gpus_total} GPU${o.gpus_total === 1 ? '' : 's'} on ${o.hosts_online}/${o.hosts_total} host${o.hosts_total === 1 ? '' : 's'} · updated ${fmtAge(o.ts)}`;
      const b = o.balance || {};
      const tile = (label, value, sub, cls = '') => h('div', { class: 'tile ' + cls }, h('div', { class: 'label' }, label), h('div', { class: 'value' }, value), sub ? h('div', { class: 'sub' }, sub) : null);
      const tiles = $('#tiles'); tiles.innerHTML = '';
      tiles.append(
        tile('Balance', b.amount == null ? '—' : `${fmtNum(b.amount, b.coin === 'BTC' ? 6 : 4)} ${b.coin || ''}`, (b.usd != null ? fmtUSD(b.usd) : (b.source === 'unavailable' ? 'pool API unavailable' : b.verified === false ? 'pool API unverified' : '')) + (b.provider ? ' · ' + b.provider : '') + (b.source === 'cache' ? ' · cached' : '')),
        tile('Coin / algorithm', `${o.coin || '—'}`, o.algo || ''),
        tile('Total power', fmtW(o.total_power_w), o.total_hashrate && o.total_power_w ? fmtEff(o.total_hashrate, o.total_power_w) : ''),
        tile('Uptime', fmtDur(o.uptime_s), 'hub host'),
        tile('Active alerts', String(o.alerts.length), o.alerts.length ? o.alerts[0].rule : 'all clear', o.alerts.length ? 'alert' : ''),
        tile('Last Claude run', o.last_claude_run ? o.last_claude_run.task : '—', o.last_claude_run ? `${o.last_claude_run.status} · ${fmtAge(o.last_claude_run.started)}` : 'none yet'));
      const al = $('#alert-list'); al.innerHTML = '';
      if (!o.alerts.length) al.append(h('p', { class: 'muted' }, '✓ No active alerts')); else o.alerts.forEach(a => al.append(alertRow(a)));
      const gc = $('#gpu-cards'); gc.innerHTML = ''; o.gpus.forEach(g => gc.append(gpuCard(g)));
      const hl = $('#host-list'); hl.innerHTML = '';
      o.hosts.forEach(hst => {
        const s = hst.sample || {};
        hl.append(h('div', { class: 'row' }, h('span', { class: 'badge ' + (hst.online ? 'good' : 'crit') }, (hst.online ? '● ' : '○ ') + hst.role),
          h('div', {}, h('b', {}, hst.name), ` · ${hst.gpu_count} GPU · miner ${s.miner_state || '—'} · ${fmtHR(s.miner_hashrate)} · ${fmtW(s.gpu_w_total)}`,
            h('div', { class: 'muted', style: 'font-size:.8rem' }, `${hst.os || ''} · CPU ${fmtC(s.cpu_c)} ${fmtPct(s.cpu_pct)} · RAM ${fmtNum(s.ram_used_mb)}/${fmtNum(s.ram_total_mb)} MB · wall ${fmtW(s.wall_w)}`)),
          h('span', { class: 't' }, fmtAge(hst.last_ts))));
      });
    }
    load().catch(console.error); setInterval(() => load().catch(console.error), 60000);
  };

  // ---- GPUs list -------------------------------------------------------------------------------------
  RO.gpus = function () {
    async function load() { const gs = await api('/api/gpus'); if (!gs) return; const gc = $('#gpu-cards'); gc.innerHTML = ''; gs.forEach(g => gc.append(gpuCard(g))); if (!gs.length) gc.append(h('p', { class: 'muted' }, 'No GPUs registered yet.')); }
    load().catch(console.error); setInterval(() => load().catch(console.error), 60000);
  };

  // ---- GPU detail -------------------------------------------------------------------------------------
  RO.gpu = function (id) {
    let hours = 24;
    const specs = [
      { title: 'Hashrate', auto: 'hashrate', zero: true, area: true, series: [{ key: 'hashrate', label: 'hashrate', color: '--s1' }] },
      { title: 'Power', unit: 'W', zero: true, series: [{ key: 'power_w', label: 'draw', color: '--s1' }, { key: 'pl_w', label: 'limit', color: '--s2' }] },
      { title: 'Temperature', unit: '°C', series: [{ key: 'core_c', label: 'core', color: '--s1' }, { key: 'memj_c', label: 'memory junction', color: '--s2' }, { key: 'hotspot_c', label: 'hot spot', color: '--s3' }] },
      { title: 'Efficiency', auto: 'eff', zero: true, series: [{ key: 'eff', label: 'hashrate per watt', color: '--s1' }] },
      { title: 'Fan', unit: '%', zero: true, digits: 0, series: [{ key: 'fan_pct', label: 'fan', color: '--s1' }] },
      { title: 'Core clock', unit: 'MHz', digits: 0, series: [{ key: 'core_mhz', label: 'core', color: '--s1' }] },
      { title: 'Memory clock', unit: 'MHz', digits: 0, series: [{ key: 'mem_mhz', label: 'memory', color: '--s1' }] },
      { title: 'Utilisation', unit: '%', zero: true, digits: 0, series: [{ key: 'util_pct', label: 'util', color: '--s1' }] },
    ];
    const wrap = $('#charts'); specs.forEach(s => mkChart(wrap, s));
    $('#tables').onchange = e => charts.forEach(st => { st.table.hidden = !e.target.checked; renderTable(st); });
    $('#range').addEventListener('click', e => { const b = e.target.closest('button[data-h]'); if (!b) return; hours = +b.dataset.h; $$chips(b); series(); });
    const $$chips = b => { for (const c of $('#range').querySelectorAll('button')) c.classList.toggle('on', c === b); };

    async function series() {
      charts.forEach(st => st.box.classList.add('loading'));
      let cols = await api(`/api/gpus/${id}/series?hours=${hours}&res=${hours <= 1 ? '15s' : '5m'}`);
      if (cols && (!cols.ts || cols.ts.length < 2) && hours <= 1) cols = await api(`/api/gpus/${id}/series?hours=${hours}&res=5m`);
      if (!cols) return;
      charts.forEach(st => { try { drawChart(st, cols); } catch (e) { console.error(spec_name(st), e); st.box.classList.remove('loading'); } });
    }
    const spec_name = st => st.spec.title;
    async function detail() {
      const d = await api(`/api/gpus/${id}/detail`); if (!d) return;
      const g = d.gpu, s = g.sample || {};
      const bd = $('#gpu-badges'); bd.innerHTML = '';
      if (g.ladder) bd.append(h('span', { class: 'badge serious' }, '⚠ protected: ' + g.ladder));
      if (g.open_trial) bd.append(h('span', { class: 'badge' }, 'trial running'));
      if (g.profile) bd.append(h('span', { class: 'badge ' + (g.profile.status === 'converged' ? 'good' : '') }, 'profile: ' + g.profile.status));
      if (s.throttle && s.throttle.length) bd.append(h('span', { class: 'badge warn' }, 'throttle: ' + s.throttle.join(', ')));
      const tile = (label, value, sub) => h('div', { class: 'tile' }, h('div', { class: 'label' }, label), h('div', { class: 'value' }, value), sub ? h('div', { class: 'sub' }, sub) : null);
      const t = $('#tiles'); t.innerHTML = '';
      t.append(tile('Hashrate', fmtHR(s.hashrate), `shares ${fmtNum(s.accepted)} / ${fmtNum(s.rejected)} rej`), tile('Power', fmtW(s.power_w), `limit ${fmtW(s.pl_w)} · stock ${fmtW(g.stock_pl_w)}`),
        tile('Efficiency', fmtEff(s.hashrate, s.power_w), ''), tile('Core', fmtC(s.core_c), `fan ${fmtPct(s.fan_pct)}${s.fan_rpm ? ' · ' + fmtNum(s.fan_rpm) + ' rpm' : ''}`),
        tile('Memory junction', fmtC(s.memj_c), s.memj_c == null ? 'not exposed on this host' : ''), tile('Hot spot', fmtC((s.extra || {}).hotspot_c), (s.extra || {}).hotspot_c == null ? 'needs LibreHardwareMonitor' : 'throttles near 105 °C'),
        tile('Clocks', `${fmtNum(s.core_mhz)} / ${fmtNum(s.mem_mhz)}`, `offsets ${s.core_offset_mhz ?? '—'} / ${s.mem_offset_mhz ?? '—'} MHz`), tile('Sampled', fmtAge(s.ts), fmtTime(s.ts)));
      const p = $('#profile'); p.innerHTML = '';
      const kvs = h('div', { class: 'kvs' });
      for (const st of ['active', 'converged', 'known_good']) { const pr = d.profiles[st]; if (pr) { kvs.append(h('b', {}, st), h('span', {}, JSON.stringify(pr.settings) + ' · ' + fmtTime(pr.created) + (pr.note ? ' · ' + pr.note : ''))); } }
      p.append(kvs.children.length ? kvs : h('p', { class: 'muted' }, 'No saved profile yet (control disabled or first run).'));
      const tr = $('#trials'); tr.innerHTML = '';
      d.trials.forEach(x => tr.append(h('div', { class: 'row' }, h('span', { class: 'badge ' + ({ kept: 'good', reverted: 'warn', aborted: 'serious', open: '' }[x.verdict] || '') }, x.verdict),
        h('div', {}, `${x.kind}${x.strategy ? ' · ' + x.strategy : ''} · ${JSON.stringify(x.setting)}`, h('div', { class: 'muted', style: 'font-size:.8rem' }, x.reason || '')), h('span', { class: 't' }, fmtTime(x.started)))));
      if (!d.trials.length) tr.append(h('p', { class: 'muted' }, 'No trials yet.'));
      const ac = $('#actions'); ac.innerHTML = '';
      d.actions.forEach(a => ac.append(h('div', { class: 'row' }, h('span', { class: 'badge ' + (a.ok ? (a.dry_run ? '' : 'good') : 'serious') }, a.ok ? (a.dry_run ? 'dry-run' : 'ok') : 'failed'),
        h('div', {}, `${a.action} ${a.params || ''} by ${a.actor}`, h('div', { class: 'muted', style: 'font-size:.8rem' }, `${a.before ?? ''} → ${a.after ?? ''} · ${a.reason || ''} ${a.error || ''}`)), h('span', { class: 't' }, fmtTime(a.ts)))));
      if (!d.actions.length) ac.append(h('p', { class: 'muted' }, 'No actions yet.'));
    }
    Promise.all([detail(), series()]).catch(console.error);
    setInterval(() => Promise.all([detail(), series()]).catch(console.error), 60000);
  };

  // ---- Log -------------------------------------------------------------------------------------------
  RO.log = function () {
    let offset = 0; const limit = 200; let gpus = [];
    const q = () => { const p = new URLSearchParams(); const hst = $('#f-host').value, g = $('#f-gpu').value, r = $('#f-range').value, res = $('#f-res').value;
      if (hst) p.set('host', hst); if (g) p.set('gpu', g); if (r) p.set('since', (Date.now() / 1000 - r * 3600).toFixed(0)); p.set('res', res); return p; };
    async function init() {
      gpus = await api('/api/gpus') || [];
      const hosts = [...new Set(gpus.map(g => g.host))];
      hosts.forEach(hn => $('#f-host').append(h('option', { value: hn }, hn)));
      gpus.forEach(g => $('#f-gpu').append(h('option', { value: g.id }, `${g.host} GPU ${g.idx}`)));
      for (const sel of ['#f-host', '#f-gpu', '#f-range', '#f-res']) $(sel).onchange = () => { offset = 0; $('#logt tbody').innerHTML = ''; load(); };
      $('#more').onclick = load; load();
    }
    async function load() {
      const p = q(); p.set('limit', limit); p.set('offset', offset);
      $('#csv').href = '/api/log.csv?' + q().toString();
      const rows = await api('/api/log?' + p.toString()); if (!rows) return;
      const tb = $('#logt tbody');
      rows.forEach(r => tb.append(h('tr', {}, h('td', {}, fmtTime(r.ts)), h('td', {}, r.host), h('td', {}, 'GPU ' + r.idx), h('td', {}, fmtHR(r.hashrate)), h('td', {}, fmtNum(r.power_w)),
        h('td', {}, fmtNum(r.pl_w)), h('td', {}, fmtNum(r.core_c)), h('td', {}, r.memj_c == null ? '—' : fmtNum(r.memj_c)), h('td', {}, fmtNum(r.fan_pct)), h('td', {}, fmtNum(r.core_mhz)), h('td', {}, fmtNum(r.mem_mhz)),
        h('td', {}, `${fmtNum(r.accepted)} / ${fmtNum(r.rejected)}`), h('td', {}, (JSON.parse(r.throttle || '[]')).join(',') || ''))));
      offset += rows.length; $('#more').hidden = rows.length < limit;
      if (!tb.children.length) tb.append(h('tr', {}, h('td', { colspan: 13, class: 'muted' }, 'no rows match')));
    }
    init().catch(console.error);
  };

  // ---- Claude runs ------------------------------------------------------------------------------------
  RO.claude = function () {
    async function load() {
      const runs = await api('/api/claude-runs'); if (!runs) return;
      const box = $('#runs'); box.innerHTML = '';
      if (!runs.length) box.append(h('p', { class: 'muted' }, 'No Claude runs recorded yet. Scheduled tasks start in milestone M5.'));
      runs.forEach(r => {
        const det = h('details', { class: 'run' }, h('summary', {},
          h('span', { class: 'badge ' + ({ ok: 'good', error: 'crit', timeout: 'serious', skipped: '', running: 'warn' }[r.status] || '') }, r.status),
          h('div', {}, h('b', {}, r.task), ` · ${r.trigger || ''} ${r.schedule ? '· ' + r.schedule : ''}`, h('div', { class: 'muted', style: 'font-size:.85rem' }, r.summary || '')),
          h('span', { class: 't' }, fmtTime(r.started), r.ended ? ` · ${fmtDur(r.ended - r.started)}` : '')));
        det.addEventListener('toggle', async () => {
          if (!det.open || det.dataset.loaded) return; det.dataset.loaded = 1;
          const full = await api('/api/claude-runs/' + encodeURIComponent(r.run_id)); if (!full) return;
          const body = h('div', {});
          body.append(h('div', { class: 'kvs' }, h('b', {}, 'run id'), h('code', {}, full.run_id), h('b', {}, 'tokens'), h('span', {}, `${fmtNum(full.tokens_in)} in / ${fmtNum(full.tokens_out)} out${full.cost_usd != null ? ' · ' + fmtUSD(full.cost_usd) : ''}`)));
          if (full.actions && full.actions.length) {
            body.append(h('h2', { style: 'margin-top:10px' }, 'Actions taken'));
            full.actions.forEach(a => body.append(h('div', { class: 'row' }, h('span', { class: 'badge ' + (a.ok ? (a.dry_run ? '' : 'good') : 'serious') }, a.ok ? (a.dry_run ? 'dry-run' : 'ok') : 'failed'),
              h('div', {}, `${a.action} ${a.params || ''}`, h('div', { class: 'muted', style: 'font-size:.8rem' }, `${a.before ?? ''} → ${a.after ?? ''} · ${a.reason || ''} ${a.error || ''}`)), h('span', { class: 't' }, fmtTime(a.ts)))));
          }
          if (full.before_after) body.append(h('h2', { style: 'margin-top:10px' }, 'Before / after'), h('pre', {}, JSON.stringify(full.before_after, null, 1)));
          body.append(h('h2', { style: 'margin-top:10px' }, 'Full output'), h('pre', {}, full.output || '(empty)'));
          det.append(body);
        });
        box.append(det);
      });
    }
    load().catch(console.error);
  };

  // ---- Incidents --------------------------------------------------------------------------------------
  RO.incidents = function () {
    async function load() {
      const inc = await api('/api/incidents'); if (!inc) return;
      const box = $('#inc'); box.innerHTML = '';
      if (!inc.length) box.append(h('p', { class: 'muted' }, '✓ No incidents recorded.'));
      inc.forEach(i => box.append(h('div', { class: 'row' }, h('span', { class: 'badge ' + (i.status === 'open' ? sevClass(i.severity) : 'good') }, (i.status === 'open' ? sevIcon(i.severity) + ' ' : '✓ ') + i.status),
        h('div', {}, h('b', {}, i.rule), ` · ${i.host || ''}${i.idx != null ? ' GPU ' + i.idx : ''} · step: ${i.ladder_step || '—'}`,
          h('div', { class: 'muted', style: 'font-size:.85rem' }, i.summary || ''), i.claude_run_id ? h('div', { style: 'font-size:.85rem' }, h('a', { href: '/claude' }, 'Claude run ' + i.claude_run_id)) : null),
        h('span', { class: 't' }, fmtTime(i.opened), i.closed ? h('br') : null, i.closed ? 'closed ' + fmtAge(i.closed) : ''))));
    }
    load().catch(console.error); setInterval(() => load().catch(console.error), 60000);
  };

  window.RO = RO;
})();
