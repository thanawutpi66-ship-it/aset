/* ===========================================================================
   ASET Cloud Dashboard — frontend logic
   =========================================================================== */
'use strict';

const $ = (id) => document.getElementById(id);
const f = (x, d = 2) => (x == null || x === '' || isNaN(x)) ? '–' : Number(x).toFixed(d);
const num = (o, ...keys) => { for (const k of keys) { if (o && o[k] != null && !isNaN(o[k])) return Number(o[k]); } return null; };
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));
function fmtR(mOhm){ if (mOhm == null) return '–'; return mOhm >= 1000 ? (mOhm/1000).toFixed(2)+' Ω' : mOhm.toFixed(2)+' mΩ'; }
function fmtKB(bytes){ return bytes >= 1024 ? Math.round(bytes/1024)+' KB' : bytes+' B'; }
function fmtElapsed(s){
  if (s == null || isNaN(s) || s < 0) return '--:--';
  s = Math.floor(s);
  const hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
  if (hh > 0) return hh + ':' + String(mm).padStart(2,'0') + ':' + String(ss).padStart(2,'0');
  return String(mm).padStart(2,'0') + ':' + String(ss).padStart(2,'0');
}
// Escapes text pulled from an ingest payload (meta.battery, alarm ts/msg) before
// it's interpolated into an innerHTML string — those fields come from whoever
// holds the ingest token, not from this page, so they're untrusted input.
function escapeHtml(s){
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}
function fmtDate(ts){
  const d = new Date(ts * 1000);
  const day = d.getDate(), mon = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][d.getMonth()];
  const yr = d.getFullYear(), hh = String(d.getHours()).padStart(2,'0'), mm = String(d.getMinutes()).padStart(2,'0');
  return `${day} ${mon} ${yr}  ${hh}:${mm}`;
}

const TEMP_CRIT = 55;
let isMuted = false;
let graphMode = 'combined';
let graphWindow = 'recent';  // 'recent' (last 30 min, clear detail) or 'full' (whole test)
let lastSeriesRecent = {};
let lastSeriesFull = {};
let mainCharts = {};
let icaChart = null;
let alarmLog = [];
let selectedSession = null;
let _lastLabAlarmTs = null;  // null = not yet backfilled (see processLabAlarms)
let _analyzeState = null;  // {idx, queued_at, timer} while re-analysis is in flight

function sohColor(v){ return v >= 85 ? css('--ok') : v >= 70 ? css('--warn') : css('--crit'); }

/* ---- axis / dataset helpers (ECharts) ------------------------------------ */
function emkScale(name, color, pos, offset = 0) {
  return {
    type: 'value', name, position: pos, offset, scale: true,
    // Vertical axis title, anchored to the middle of the axis — the default
    // nameLocation:'end' floats the title at the very top of the axis, which
    // collides with the legend (also anchored top:0) and renders as garbled
    // overlapping text once there's more than one right-side axis.
    nameLocation: 'middle',
    nameGap: pos === 'left' ? 38 : 45,
    nameRotate: pos === 'left' ? 90 : -90,
    nameTextStyle: { color, fontSize: 10 },
    axisLabel: { color, fontSize: 10, formatter: (val) => val.toFixed(2) },
    // Was a hardcoded cyan tint, ignoring the theme entirely (unlike the ICA
    // chart, which already read css('--border') correctly) — invisible/wrong
    // hue against the light theme's near-white background.
    splitLine: { show: pos === 'left', lineStyle: { color: css('--border') } },
    axisLine: { show: true, lineStyle: { color } }
  };
}
function emkSeries(name, color, yAxisIndex, data) {
  return {
    name, type: 'line', data, yAxisIndex,
    showSymbol: false, itemStyle: { color }, lineStyle: { width: 1.8 },
    // End-of-line value label + hover-highlight-this-series, mirroring the
    // ECharts line-race example — dim the other lines instead of a busy
    // tooltip trying to rank 3 differently-scaled series at once.
    endLabel: {
      show: true,
      formatter: (params) => {
        // Category-axis line series stores plain numbers (not [x,y] pairs),
        // so params.value is the y-value itself here.
        const v = Array.isArray(params.value) ? params.value[params.value.length - 1] : params.value;
        return v != null && !isNaN(v) ? Number(v).toFixed(2) : '';
      },
      color, fontSize: 10,
    },
    labelLayout: { moveOverlap: 'shiftY' },
    emphasis: { focus: 'series' },
  };
}
function ebaseOpts(gridOpts = {}) {
  return {
    animation: false,
    tooltip: { trigger: 'axis', backgroundColor: '#0b1220', borderColor: 'rgba(255,255,255,.12)', textStyle: { color: css('--text'), fontSize: 11 } },
    // Bounded to the plot's left/right edges (not centered/full-width) so it
    // never overlaps the right-side axes' vertical titles above.
    legend: { textStyle: { color: css('--muted'), fontSize: 11 }, top: 0, left: 55, right: 55 },
    grid: { left: 55, right: 55, top: 35, bottom: 45, ...gridOpts },
    dataZoom: [
      { type: 'slider', bottom: 5, height: 16, borderColor: css('--border'), textStyle: { color: css('--muted') }, handleSize: '80%' },
      { type: 'inside' }
    ],
    xAxis: { 
      type: 'category', data: [], 
      axisLabel: { color: css('--faint'), fontSize: 10, maxInterval: 300 }, 
      splitLine: { show: true, lineStyle: { color: css('--border') } },
      axisTick: { show: false }
    },
    yAxis: []
  };
}

/* ---- responsive resize ----------------------------------------------------
   ECharts locks in whatever pixel size its container reports at echarts.init()
   time and never re-measures on its own. If that happens before the flex
   layout (.chart-area{flex:1}) has settled — very likely on first paint, or
   whenever this container's size changes afterward (window resize, sidebar
   content changing height) — the canvas is stuck squished (this was the
   actual cause of the garbled/overlapping chart reported: it wasn't just the
   axis-label/legend collision, the plot area itself was rendering at ~100px
   wide). A ResizeObserver on the shared #charts host catches every case;
   window 'resize' is kept too as a cheap belt-and-suspenders fallback. */
function resizeAllCharts() {
  Object.values(mainCharts).forEach(c => { try { c.resize(); } catch(e){} });
  if (icaChart) { try { icaChart.resize(); } catch(e){} }
}

/* ---- build / destroy main charts ----------------------------------------- */
function destroyMainCharts() {
  Object.values(mainCharts).forEach(c => { try { c.dispose(); } catch(e){} });
  mainCharts = {};
  $('charts').innerHTML = '';
}
function addWrap(heightPx) {
  const host = $('charts');
  const wrap = document.createElement('div');
  wrap.className = 'chart-wrap';
  wrap.style.width = '100%';
  wrap.style.height = heightPx + 'px';
  host.appendChild(wrap);
  return wrap;
}
function buildMainCharts() {
  destroyMainCharts();
  const vC = css('--v'), iC = css('--i'), tC = css('--t');
  if (graphMode === 'combined') {
    const el = addWrap(420);
    mainCharts.combined = echarts.init(el);
    // Extra right margin: two stacked right-side axes (Current/Temp, offset 0/45)
    // plus each series' end-of-line value label need room beyond the old 90px.
    const opts = ebaseOpts({ right: 150 });
    opts.yAxis = [ 
      emkScale('Voltage (V)', vC, 'left'), 
      emkScale('Current (A)', iC, 'right'), 
      emkScale('Temp (°C)', tC, 'right', 45) 
    ];
    opts.series = [ 
      emkSeries('Voltage (V)', vC, 0, []), 
      emkSeries('Current (A)', iC, 1, []), 
      emkSeries('Temp (°C)', tC, 2, []) 
    ];
    mainCharts.combined.setOption(opts);
  } else if (graphMode === 'split2') {
    const c1 = addWrap(215);
    mainCharts.vc = echarts.init(c1);
    const opts1 = ebaseOpts();
    opts1.yAxis = [ emkScale('Voltage (V)', vC, 'left'), emkScale('Current (A)', iC, 'right') ];
    opts1.series = [ emkSeries('Voltage (V)', vC, 0, []), emkSeries('Current (A)', iC, 1, []) ];
    mainCharts.vc.setOption(opts1);

    const c2 = addWrap(215);
    mainCharts.temp = echarts.init(c2);
    const opts2 = ebaseOpts();
    opts2.yAxis = [ emkScale('Temp (°C)', tC, 'left') ];
    opts2.series = [ emkSeries('Temp (°C)', tC, 0, []) ];
    mainCharts.temp.setOption(opts2);
  } else {
    [['Voltage (V)', vC, 'yV'], ['Current (A)', iC, 'yI'], ['Temp (°C)', tC, 'yT']].forEach(([label, color, axis]) => {
      const el = addWrap(145);
      mainCharts[axis] = echarts.init(el);
      const opts = ebaseOpts();
      opts.yAxis = [ emkScale(label, color, 'left') ];
      opts.series = [ emkSeries(label, color, 0, []) ];
      mainCharts[axis].setOption(opts);
    });
  }
  // The DOM mutations above (innerHTML='' then appendChild) happen in the same
  // synchronous tick as echarts.init() — the browser hasn't run a layout pass
  // yet, so the container can report a stale/incorrect size (this is what
  // actually produced the ~100px-wide squished chart, separate from the
  // axis-label/legend overlap). Deferring one frame guarantees layout has
  // settled before ECharts measures its container. A parent-size-only
  // ResizeObserver (see resizeAllCharts) doesn't cover this case — swapping
  // children doesn't change the parent's own size, so it never fires here.
  requestAnimationFrame(resizeAllCharts);
}
function updateMainCharts(ser) {
  const elapsed = (ser.Elapsed_s || []).map(v => v.toFixed(1));
  const vD = ser.Voltage_V || [], iD = ser.Current_A || [], tD = ser.Temperature_C || [];
  if (graphMode === 'combined' && mainCharts.combined) {
    mainCharts.combined.setOption({ xAxis: { data: elapsed }, series: [{ data: vD }, { data: iD }, { data: tD }] });
  } else if (graphMode === 'split2') {
    if (mainCharts.vc)   mainCharts.vc.setOption({ xAxis: { data: elapsed }, series: [{ data: vD }, { data: iD }] });
    if (mainCharts.temp) mainCharts.temp.setOption({ xAxis: { data: elapsed }, series: [{ data: tD }] });
  } else {
    if (mainCharts.yV) mainCharts.yV.setOption({ xAxis: { data: elapsed }, series: [{ data: vD }] });
    if (mainCharts.yI) mainCharts.yI.setOption({ xAxis: { data: elapsed }, series: [{ data: iD }] });
    if (mainCharts.yT) mainCharts.yT.setOption({ xAxis: { data: elapsed }, series: [{ data: tD }] });
  }
}

/* ---- ICA chart (in Diagnostics tab) -------------------------------------- */
function buildIcaChart() {
  const el = $('icaChart');
  if (!el) return;
  if (icaChart) { try { icaChart.dispose(); } catch(e){} icaChart = null; }
  icaChart = echarts.init(el);
  const opts = {
    animation: false,
    tooltip: { trigger: 'axis', backgroundColor: '#0b1220', borderColor: 'rgba(255,255,255,.12)', textStyle: { color: css('--text'), fontSize: 11 } },
    grid: { left: 55, right: 20, top: 20, bottom: 40 },
    xAxis: { type: 'value', name: 'Voltage (V)', nameLocation: 'middle', nameGap: 25, nameTextStyle: { color: css('--muted'), fontSize: 10 }, axisLabel: { color: css('--faint'), fontSize: 10 }, splitLine: { show: true, lineStyle: { color: css('--border') } }, scale: true },
    yAxis: { type: 'value', name: 'dQ/dV', nameLocation: 'middle', nameGap: 35, nameTextStyle: { color: css('--muted'), fontSize: 10 }, axisLabel: { color: css('--faint'), fontSize: 10 }, splitLine: { show: true, lineStyle: { color: css('--border') } }, scale: true },
    series: [{ name: 'dQ/dV', type: 'line', data: [], showSymbol: false, itemStyle: { color: css('--soc') }, lineStyle: { width: 1.5 } }]
  };
  icaChart.setOption(opts);
  requestAnimationFrame(() => { try { icaChart.resize(); } catch(e){} });
}
function updateIcaChart(voltageArr, socArr) {
  if (!icaChart) return;
  if (!voltageArr || !socArr || voltageArr.length < 5) { icaChart.setOption({ series: [{ data: [] }] }); return; }
  const pts = [];
  for (let i = 1; i < voltageArr.length; i++) {
    const dV = voltageArr[i] - voltageArr[i-1], dQ = socArr[i] - socArr[i-1];
    if (Math.abs(dV) > 1e-4) pts.push([voltageArr[i], dQ / dV]);
  }
  const w = 5;
  const smoothed = pts.map((p, i) => { const sl = pts.slice(Math.max(0,i-w),i+w+1); return [p[0], sl.reduce((s,q)=>s+q[1],0)/sl.length]; });
  icaChart.setOption({ series: [{ data: smoothed }] });
}

/* ---- re-analysis (Method 1 — lab polling) -------------------------------- */
function _clearAnalyzeState() {
  if (_analyzeState && _analyzeState.timer) clearInterval(_analyzeState.timer);
  _analyzeState = null;
  const btn = $('analyzeBtn');
  if (btn) { btn.textContent = 'Analyze'; btn.disabled = false; }
}

async function requestAnalysis(idx) {
  _clearAnalyzeState();
  const btn = $('analyzeBtn');
  btn.textContent = 'Sending to lab…';
  btn.disabled = true;
  let queued_at;
  try {
    const res = await fetch('/api/analyze-request/' + idx, { method: 'POST' });
    if (!res.ok) { _clearAnalyzeState(); return; }
    const data = await res.json();
    queued_at = data.queued_at;
    btn.textContent = 'Lab analyzing… (0s)';
  } catch(e) { _clearAnalyzeState(); return; }

  let elapsed = 0;
  const timer = setInterval(async () => {
    elapsed += 5;
    const btn2 = $('analyzeBtn');
    if (btn2) btn2.textContent = 'Lab analyzing… (' + elapsed + 's)';
    try {
      const res = await fetch('/api/session/' + idx);
      if (!res.ok) return;
      const snap = await res.json();
      const at = (snap.payload && snap.payload.analysis && snap.payload.analysis._analyzed_at) || 0;
      if (at > queued_at) {
        renderPayload(snap.payload, snap.received_at);
        _clearAnalyzeState();
      }
    } catch(e) {}
    if (elapsed >= 120) _clearAnalyzeState();
  }, 5000);
  _analyzeState = { idx, queued_at, timer };
}

/* ---- sessions list ------------------------------------------------------- */
async function fetchSessions() {
  try {
    const res = await fetch('/api/sessions');
    if (!res.ok) return;
    const data = await res.json();
    renderSessions(data.sessions || []);
  } catch(e) {}
}

function renderSessions(sessions) {
  const list = $('sessionsList');
  if (!sessions.length) { list.innerHTML = '<div class="sess-empty">No sessions yet.</div>'; return; }
  list.innerHTML = '';
  // Show newest first
  [...sessions].reverse().forEach(s => {
    const div = document.createElement('div');
    div.className = 'sess-item' + (selectedSession === s.idx ? ' selected' : '');
    div.dataset.idx = s.idx;
    div.innerHTML =
      `<span class="sess-num">${s.idx}.</span>` +
      `<span class="sess-info"><span class="sess-name">Data Log</span>` +
      `<span class="sess-date">${fmtDate(s.received_at)}</span></span>` +
      `<span class="sess-size">${fmtKB(s.size_bytes)}</span>`;
    div.addEventListener('click', () => selectSession(s.idx));
    list.appendChild(div);
  });
}

async function selectSession(idx) {
  _clearAnalyzeState();
  selectedSession = idx;
  document.querySelectorAll('.sess-item').forEach(el => el.classList.toggle('selected', +el.dataset.idx === idx));
  $('sessPrompt').hidden = true;
  $('anResults').hidden  = false;

  // Open ECM accordion automatically
  const ecmContent = $('ecmContent'), ecmToggle = $('ecmToggle');
  if (ecmContent && ecmContent.hidden) {
    ecmContent.hidden = false;
    if (ecmToggle) ecmToggle.textContent = '▼ Show Equivalent Circuit';
  }

  // Show "viewing session" badge, show back-to-live button
  const badge = $('sessViewingBadge');
  if (badge) badge.textContent = 'Viewing session #' + idx;
  const liveBtn = $('backToLiveBtn');
  if (liveBtn) liveBtn.hidden = false;

  try {
    const res = await fetch('/api/session/' + idx);
    if (!res.ok) return;
    const snap = await res.json();
    if (snap.payload) renderPayload(snap.payload, snap.received_at);
  } catch(e) {}
}

function backToLive() {
  _clearAnalyzeState();
  selectedSession = null;
  document.querySelectorAll('.sess-item').forEach(el => el.classList.remove('selected'));
  const badge = $('sessViewingBadge');
  if (badge) badge.textContent = '';
  const liveBtn = $('backToLiveBtn');
  if (liveBtn) liveBtn.hidden = true;
  $('sessPrompt').hidden = false;
  $('anResults').hidden  = true;
  load();
}

/* ---- render payload (used by both live poll and session select) ----------- */
function renderPayload(p, received_at) {
  const s = p.summary || {}, a = p.analysis || {},
        L = s.latest || {}, feat = a.features || {};
  lastSeriesRecent = p.series || {};
  lastSeriesFull = p.series_full || p.series || {};
  const ser = graphWindow === 'full' ? lastSeriesFull : lastSeriesRecent;

  $('battery').innerHTML = '<i class="dot"></i>battery: <b>' + escapeHtml((p.meta||{}).battery || '–') + '</b>';
  
  const sn = (p.meta||{}).sn || (p.meta||{}).serial_number || (p.meta||{}).device_id || 'N/A';
  if ($('deviceSn')) $('deviceSn').textContent = escapeHtml(sn);

  const T = num(L, 'Temperature_C');
  $('tempTitle').textContent = T != null ? f(T, 2) + ' °C' : '-- °C';

  // Telemetry
  const V = num(L,'Voltage_V'), I = num(L,'Current_A'), soc = num(L,'SoC_pct');
  const soh = num(a,'soh') ?? num(feat,'soh_pct');
  $('mV').textContent   = V   != null ? f(V,   2) : '0.00';
  $('mI').textContent   = I   != null ? f(I,   2) : '0.00';
  $('mSoC').textContent = soc != null ? f(soc, 2) : '0.00';
  // Rin_Calibrated=false: still _ekf_rc_defaults()'s uncalibrated placeholder guess
  // (no real HPPC pulse fitted yet), not a bench-comparable measurement — show it live
  // (operators want a continuous trend) but labelled, so it isn't mistaken for a
  // reading the way it was before this field existed.
  $('mR').textContent   = fmtR(num(L,'Resistance_mOhm')) + (L.Rin_Calibrated === false ? ' (est.)' : '');
  $('mT').textContent   = T   != null ? f(T,   2) : '0.00';
  $('mSoH').textContent = soh != null ? Math.round(soh) + '%' : '–';
  const mSoHEl = $('mSoH'); if (mSoHEl && soh != null) mSoHEl.style.color = sohColor(soh);

  // Analytics tab — grade + confidence
  const grade = a.grade || '–';
  const gEl = $('grade');
  if (gEl) {
    gEl.textContent = grade;
    const gc = css('--g' + grade);
    gEl.style.color = gc || css('--text');
  }
  const conf = num(a,'confidence') ?? num(a,'conf_pct');
  if ($('gradeConf')) $('gradeConf').textContent = conf != null ? Math.round(conf) : '–';

  // Grade action button
  const gaBtn = $('gradeActionBtn');
  if (gaBtn) {
    gaBtn.textContent = grade;
    const gc = css('--g' + grade);
    gaBtn.style.background = gc ? gc + '28' : 'rgba(255,255,255,.04)';
    gaBtn.style.color = gc || css('--text');
    gaBtn.style.borderColor = gc || css('--border-2');
  }

  // Summary table
  const capNorm = num(a,'capacity_norm_ah'), capRaw = num(a,'capacity_ah') ?? num(s,'capacity_ah');
  const capVal  = capNorm ?? capRaw;
  if ($('cap'))     $('cap').textContent     = f(capVal, 2);
  if ($('ocv'))     $('ocv').textContent     = f(num(a,'ocv_v'), 2);
  if ($('dcir'))    $('dcir').textContent    = f(num(a,'dcir_mohm','ri_mohm'), 2);
  if ($('dcirUnc')) $('dcirUnc').textContent = f(num(a,'dcir_unc_mohm'), 2);
  if ($('sumSoH')) {
    $('sumSoH').textContent = soh != null ? Math.round(soh) : 'N/A';
    if (soh != null) $('sumSoH').style.color = sohColor(soh);
  }

  // ECM values grid
  if ($('ecmVoc')) $('ecmVoc').textContent = num(a,'ocv_v') != null ? f(num(a,'ocv_v'),2) + ' V' : '–';
  if ($('r0'))  $('r0').textContent  = num(a,'r0_mohm') != null ? f(num(a,'r0_mohm'),2) : '–';
  if ($('r1'))  $('r1').textContent  = num(a,'r1_mohm') != null ? f(num(a,'r1_mohm'),2) : '–';
  if ($('tau')) $('tau').textContent = f(num(a,'tau_s'), 2);
  if ($('cca')) $('cca').textContent = f(num(a,'cca_est_a'), 2);

  const safe = s.safety_status || (p.meta||{}).safety_status || 'NORMAL';

  // Test status panel
  updateTestPanel(p.meta || {}, s);

  // Charts
  updateMainCharts(ser);
  updateIcaChart(lastSeriesFull.Voltage_V, lastSeriesFull.SoC_pct);

  // Real safety events forwarded from the GUI's alarm log (ALARM/WARNING),
  // not just this browser's own temperature-threshold guess below.
  processLabAlarms(p.alarms || []);

  // Alarm tracking
  const explicitAlarm = safe === 'ALARM';
  const hot = T != null && T >= TEMP_CRIT;
  if (explicitAlarm || hot) {
    const msg = 'TEMP: ' + f(T,2) + '°C  GRADE: ' + grade + (explicitAlarm ? '  SAFETY ALARM' : '  OVER-TEMP');
    const ts = new Date(received_at * 1000).toLocaleTimeString();
    pushAlarm(ts, msg, 'ALARM');
    setSafety(true, msg);
  } else {
    setSafety(false, '');
  }

  // Freshness (for live poll only — called from load())
  if (received_at) {
    const ageMs = Date.now() - received_at * 1000;
    const ageS  = Math.max(0, Math.round(ageMs / 1000));
    $('updated').textContent = ageS < 90 ? ageS + 's ago' : Math.round(ageS / 60) + 'm ago';
    const live = ageMs < 60000;
    $('live').textContent    = live ? 'LIVE' : 'idle';
    $('livePill').className  = 'pill ' + (live ? 'live' : 'stale');
    setConnected(live ? 'connected' : 'idle');
    if (s.row_count != null) $('rowInfo').textContent = s.row_count + ' samples · ' + f(s.energy_wh,2) + ' Wh logged';
  }

  // Charge/Discharge status + CC/CV mode + elapsed time
  updateChargeStatus(I, p.meta || {}, s);
}

/* ---- charge / discharge status + CC/CV mode ------------------------------ */
function updateChargeStatus(current, meta, summary) {
  const statusEl = $('currentStatus');
  const iconEl = $('currentStatusIcon');
  const textEl = $('currentStatusText');
  const chargeElEl = $('chargeElapsed');
  const ccBadge = $('ccBadge'), cvBadge = $('cvBadge');

  const phase = (meta.phase || summary.phase || '').toLowerCase();
  const subPhase = (meta.sub_phase || '').toLowerCase();

  // Determine charge/discharge/rest state
  let state = 'rest';
  let icon = '\u2015';   // ― horizontal bar
  let label = 'REST';

  if (current != null && current > 0.01) {
    state = 'charging'; icon = '\u25B2'; label = 'CHARGING';   // ▲
  } else if (current != null && current < -0.01) {
    state = 'discharging'; icon = '\u25BC'; label = 'DISCHARGING'; // ▼
  }

  // Override based on phase if current is near zero but phase says charge/discharge
  if (state === 'rest') {
    if (['charge','bulk','absorption','float','cc','cv'].includes(phase)) {
      state = 'charging'; icon = '\u25B2'; label = 'CHARGING';
    } else if (['test','discharge','dcir'].includes(phase)) {
      state = 'discharging'; icon = '\u25BC'; label = 'DISCHARGING';
    }
  }

  if (statusEl) {
    statusEl.className = 'tcard-status ' + state;
    if (iconEl) iconEl.textContent = icon;
    if (textEl) textEl.textContent = label;
  }

  // CC/CV mode detection
  const isCC = subPhase === 'cc' || phase === 'cc' || phase === 'bulk';
  const isCV = subPhase === 'cv' || phase === 'cv' || phase === 'absorption' || phase === 'float';
  const isChargePhase = ['charge','bulk','absorption','float','cc','cv'].includes(phase);
}

/* ---- test panel ---------------------------------------------------------- */
const PHASE_MAP = {
  prepare:0, ocv:0,
  charge:1, bulk:1, absorption:1, float:1, cc:1, cv:1,
  rest:2,
  test:3, discharge:3, dcir:3,
  analyze:4, complete:4, done:4
};
function updateTestPanel(meta, summary) {
  const mode = (meta.test_mode || '').toUpperCase();
  const chip = $('tpModeChip');
  if (chip) chip.textContent = mode || '–';

  if ($('tpWorkflow')) $('tpWorkflow').textContent = meta.workflow || 'IEC 61960 Standard';

  // ETA / elapsed progress — mirrors the GUI's workflow progress bar
  const etaRow = $('tpEtaRow');
  const totalS = num(meta, 'total_s');
  if (etaRow) {
    if (totalS == null || totalS <= 0) {
      etaRow.hidden = true;
    } else {
      const elapsedS = clamp(num(meta, 'elapsed_s') ?? 0, 0, totalS);
      const remS = Math.max(0, totalS - elapsedS);
      const mmss = (s) => Math.floor(s / 60) + 'm ' + String(Math.floor(s % 60)).padStart(2, '0') + 's';
      $('tpEtaFill').style.width = Math.round((elapsedS / totalS) * 100) + '%';
      $('tpEtaTxt').innerHTML =
        '<span>' + mmss(elapsedS) + ' / ' + mmss(totalS) + '</span>' +
        '<span>ETA: ' + mmss(remS) + ' remaining</span>';
      etaRow.hidden = false;
    }
  }

  const phase = (meta.phase || summary.phase || '').toLowerCase();
  // Used both by the CC/CV step badge just below and the HPPC pulse/relax
  // description further down — must be declared before either use (this was
  // previously declared after its first use here, a temporal-dead-zone
  // ReferenceError that silently aborted every renderPayload() call, so the
  // charts/analytics/alarm log never updated at all).
  const subPhase = (meta.sub_phase || '').toLowerCase();
  const activeIdx = PHASE_MAP[phase] ?? -1;

  document.querySelectorAll('.tp-step').forEach((el, i) => {
    el.classList.remove('active','complete');
    if (activeIdx >= 0) {
      if (i < activeIdx) el.classList.add('complete');
      else if (i === activeIdx) el.classList.add('active');
    }
  });

  const stepBadge = $('stepCcvBadge');
  if (stepBadge) {
    if (activeIdx === 1) { // CHARGE phase
      if (subPhase === 'cc' || phase === 'cc' || phase === 'bulk') {
        stepBadge.textContent = 'CC';
        stepBadge.className = 'step-ccv-badge mode-cc';
      } else if (subPhase === 'cv' || phase === 'cv' || phase === 'absorption' || phase === 'float') {
        stepBadge.textContent = 'CV';
        stepBadge.className = 'step-ccv-badge mode-cv';
      } else {
        stepBadge.textContent = '';
        stepBadge.className = 'step-ccv-badge';
      }
    } else {
      stepBadge.textContent = '';
      stepBadge.className = 'step-ccv-badge';
    }
  }

  const nom = num(meta,'nominal_ah') ?? num(summary,'nominal_ah');
  const cc  = num(meta,'charge_crate');
  const dc  = num(meta,'discharge_crate');
  const restMin = num(meta,'rest_duration_min') ?? num(summary,'rest_duration_min');

  if ($('tpChargeMode'))    $('tpChargeMode').textContent    = meta.charge_mode || 'Auto (by chemistry)';
  if ($('tpChargeRate') && cc != null) {
    const a = nom != null ? ' = ' + f(cc * nom, 2) + ' A' : '';
    $('tpChargeRate').textContent = cc + 'C' + a;
  }
  if ($('tpRestMin'))       $('tpRestMin').textContent       = restMin != null ? restMin + ' min' : '–';
  if ($('tpDischargeRate') && dc != null) {
    const a = nom != null ? ' = ' + f(dc * nom, 2) + ' A' : '';
    $('tpDischargeRate').textContent = dc + 'C' + a;
    if ($('tpTestDesc')) $('tpTestDesc').textContent = 'Discharge ' + dc + 'C' + a;
  }
  if ($('tpRestDesc') && restMin != null) $('tpRestDesc').textContent = restMin + ' min rest';
  if ($('tpChargeDesc') && cc != null)
    $('tpChargeDesc').textContent = cc <= 0.15 ? 'Full 3-stage (Bulk→Absorption→Float)' : 'CC-CV Charge';

  // HPPC pulse/relax sub-phase — the 5-step tracker lumps this whole cycling
  // loop into one "Test" step; override its description with the same
  // pulse-vs-relax detail (cycle N/M, pulse current) the GUI's own status
  // line shows, so the two stay in lockstep instead of just saying "Test".
  if (phase === 'test' && $('tpTestDesc')) {
    const cycIdx = num(meta, 'cycle_index'), cycTot = num(meta, 'cycle_total');
    const cycLabel = (cycIdx != null && cycTot != null) ? ` ${cycIdx}/${cycTot}` : '';
    if (subPhase === 'pulse') {
      const ip = num(meta, 'pulse_current_a');
      $('tpTestDesc').textContent = 'Pulse' + cycLabel + (ip != null ? ` · ${f(ip, 2)} A` : '');
    } else if (subPhase === 'relax') {
      $('tpTestDesc').textContent = 'Relax' + cycLabel;
    } else if (dc != null) {
      // Not HPPC (or between cycles) — fall back to the plain discharge-rate
      // description so a leftover "Pulse/Relax N/M" doesn't stick around
      // after switching away from an HPPC run in the same browser session.
      const a = nom != null ? ' = ' + f(dc * nom, 2) + ' A' : '';
      $('tpTestDesc').textContent = 'Discharge ' + dc + 'C' + a;
    }
  }

  // "What's happening right now" banner — was static placeholder markup
  // ("Idle" / "Waiting for test start") that nothing ever updated.
  const actPhaseEl = $('tpActPhase'), actDetailEl = $('tpActDetail');
  if (actPhaseEl && actDetailEl) {
    const PHASE_LABELS = ['Prepare', 'Charge', 'Rest', 'Test', 'Analyze'];
    const DEFAULT_DETAILS = ['OCV calibrate', 'Bulk→Absorption→Float', 'OCV settle', 'Discharge', 'SoH + Grade'];
    const descEls = [null, $('tpChargeDesc'), $('tpRestDesc'), $('tpTestDesc'), null];
    if (activeIdx < 0) {
      actPhaseEl.textContent = 'Idle';
      actDetailEl.textContent = 'Waiting for test start';
    } else {
      actPhaseEl.textContent = PHASE_LABELS[activeIdx];
      const descEl = descEls[activeIdx];
      actDetailEl.textContent = (descEl && descEl.textContent) || DEFAULT_DETAILS[activeIdx];
    }
  }

  const dot = $('tpStatusDot'), txt = $('tpStatusTxt');
  if (dot && txt) {
    const running = activeIdx >= 0 && activeIdx < 4;
    const done    = activeIdx === 4;
    dot.className = 'tp-status-dot' + (running ? ' running' : done ? ' done' : '');
    txt.textContent = running ? 'RUNNING — ' + ['PREPARE','CHARGE','REST','TEST','ANALYZE'][activeIdx]
                    : done    ? 'COMPLETE'
                    :           'IDLE';
  }
}

/* ---- lab-forwarded events -------------------------------------------------- */
// Severities the GUI's _log_alarm() classifies into (isa101_views.py) — every
// line is forwarded now, not just ALARM/WARNING, so the log is colored per
// severity instead of rendering everything as a red alarm.
const _NEEDS_TAB_SWITCH = new Set(['ALARM', 'WARNING']);

function processLabAlarms(alarms) {
  if (!alarms.length) return;
  const sorted = [...alarms].sort((a, b) => a.ts - b.ts);
  if (_lastLabAlarmTs === null) {
    // First payload seen this page load: backfill the log silently (no tab
    // switch) so a stale event from before we opened the tab doesn't yank
    // the user away from whatever they're looking at.
    for (const a of sorted) pushAlarm(new Date(a.ts * 1000).toLocaleTimeString(), a.message, a.severity);
    _lastLabAlarmTs = sorted[sorted.length - 1].ts;
    return;
  }
  let sawNewAlarm = false;
  for (const a of sorted) {
    if (a.ts <= _lastLabAlarmTs) continue;
    pushAlarm(new Date(a.ts * 1000).toLocaleTimeString(), a.message, a.severity);
    if (_NEEDS_TAB_SWITCH.has(a.severity)) sawNewAlarm = true;
    _lastLabAlarmTs = a.ts;
  }
  // Only yank the user to the Alarm Log tab for genuine ALARM/WARNING events —
  // routine activity (Connected, Charge started, ...) logs silently.
  if (sawNewAlarm) switchLpTab('alarms');
}

/* ---- alarm log ----------------------------------------------------------- */
function pushAlarm(ts, msg, severity) {
  alarmLog.unshift({ ts, msg, severity: severity || 'ALARM' });
  if (alarmLog.length > 50) alarmLog.pop();
  renderAlarmLog();
}
function renderAlarmLog() {
  const list = $('alarmList');
  if (!alarmLog.length) { list.innerHTML = '<div class="alarm-empty">No alarms recorded.</div>'; return; }
  // Built via DOM nodes + textContent (not innerHTML string-templating) — a.ts/
  // a.msg come from the ingest payload's alarms[], which is untrusted input.
  list.innerHTML = '';
  for (const a of alarmLog) {
    const item = document.createElement('div');
    item.className = 'alarm-item';
    item.dataset.severity = a.severity || 'INFO';
    const tsSpan = document.createElement('span');
    tsSpan.className = 'alarm-ts';
    tsSpan.textContent = a.ts;
    const msgSpan = document.createElement('span');
    msgSpan.className = 'alarm-msg';
    msgSpan.textContent = a.msg;
    item.appendChild(tsSpan);
    item.appendChild(msgSpan);
    list.appendChild(item);
  }
}

/* ---- graph mode ---------------------------------------------------------- */
function setMode(mode) {
  graphMode = mode;
  document.querySelectorAll('.mode-btn[data-mode]').forEach(b => b.classList.toggle('active', b.dataset.mode === mode));
  buildMainCharts();
}

/* ---- graph window (recent 30 min vs whole test) --------------------------- */
function setGraphWindow(win) {
  graphWindow = win;
  document.querySelectorAll('.mode-btn[data-window]').forEach(b => b.classList.toggle('active', b.dataset.window === win));
  updateMainCharts(win === 'full' ? lastSeriesFull : lastSeriesRecent);
}

/* ---- left panel tab switching -------------------------------------------- */
function switchLpTab(name) {
  document.querySelectorAll('.lptab').forEach(b => b.classList.toggle('active', b.dataset.lptab === name));
  document.querySelectorAll('.lptab-content').forEach(el => { el.hidden = el.id !== 'lptab-' + name; });
  // The ICA chart was initialized while its tab was hidden (display:none), so
  // ECharts may have locked in a zero/stale size — force a re-measure now
  // that the container is actually visible.
  if (name === 'diagnostics' && icaChart) { try { icaChart.resize(); } catch(e){} }
}

/* ---- ECM accordion ------------------------------------------------------- */
function toggleEcm() {
  const btn = $('ecmToggle'), content = $('ecmContent');
  const open = !content.hidden;
  content.hidden = open;
  btn.textContent = (open ? '▶' : '▼') + ' Show Equivalent Circuit';
}

/* ---- safety / overlay ---------------------------------------------------- */
function setSafety(alarm, msg){
  const ov = $('overlay');
  if (alarm){ if (!isMuted){ ov.style.display = 'flex'; $('overlayMsg').textContent = msg; } }
  else { ov.style.display = 'none'; isMuted = false; }
}
function muteAlarm(){ isMuted = true; $('overlay').style.display = 'none'; }
window.muteAlarm = muteAlarm;
window.backToLive = backToLive;

/* ---- connection status --------------------------------------------------- */
function setConnected(state) {
  const dot = $('connDot'), txt = $('connStatus');
  if (!dot || !txt) return;
  dot.className = 'conn-dot ' + state;
  txt.textContent = state === 'connected' ? 'Connected' : state === 'idle' ? 'Idle' : 'Disconnected';
}

/* ---- live poll ------------------------------------------------------------ */
async function load(){
  try{
    const snap = await (await fetch('/api/snapshot?t=' + Date.now())).json();
    const wait = $('waiting'), content = $('content'), pill = $('livePill');
    if (!snap.payload){
      wait.style.display = 'block'; content.hidden = true;
      $('live').textContent = 'waiting'; pill.className = 'pill stale';
      setConnected('idle');
      return;
    }
    wait.style.display = 'none'; content.hidden = false;
    // Always render latest data if no manual session is selected
    if (selectedSession === null) {
      renderPayload(snap.payload, snap.received_at);
      $('anResults').hidden  = false;
      $('sessPrompt').hidden = true;
    }
    // Refresh sessions list silently
    fetchSessions();
  } catch(e){
    $('live').textContent   = 'error';
    $('livePill').className = 'pill off';
    setConnected('disconnected');
  }
}

/* ---- theme toggle ---------------------------------------------------------- */
function applyThemeIcon() {
  const isLight = document.documentElement.getAttribute('data-theme') === 'light';
  const btn = $('themeToggle');
  if (btn) btn.textContent = isLight ? '☀️' : '🌙';
}
function toggleTheme() {
  const isLight = document.documentElement.getAttribute('data-theme') === 'light';
  if (isLight) {
    document.documentElement.removeAttribute('data-theme');
    localStorage.setItem('aset-theme', 'dark');
  } else {
    document.documentElement.setAttribute('data-theme', 'light');
    localStorage.setItem('aset-theme', 'light');
  }
  applyThemeIcon();
  // Chart.js bakes axis/legend colors in at build time — rebuild so they pick up the new theme
  destroyMainCharts();
  buildMainCharts();
  buildIcaChart();
  if (selectedSession !== null) selectSession(selectedSession);
  else load();
}

/* ---- boot ---------------------------------------------------------------- */
window.addEventListener('DOMContentLoaded', () => {
  applyThemeIcon();
  $('themeToggle').addEventListener('click', toggleTheme);
  document.querySelectorAll('.mode-btn[data-mode]').forEach(btn =>
    btn.addEventListener('click', () => setMode(btn.dataset.mode)));
  document.querySelectorAll('.mode-btn[data-window]').forEach(btn =>
    btn.addEventListener('click', () => setGraphWindow(btn.dataset.window)));
  document.querySelectorAll('.lptab').forEach(btn =>
    btn.addEventListener('click', () => switchLpTab(btn.dataset.lptab)));
  $('ecmToggle').addEventListener('click', toggleEcm);
  $('refreshSessions').addEventListener('click', fetchSessions);
  $('analyzeBtn').addEventListener('click', () => {
    if (selectedSession !== null) requestAnalysis(selectedSession);
    else fetchSessions();
  });

  buildMainCharts();
  buildIcaChart();
  fetchSessions();
  load();
  setInterval(load, 5000);

  window.addEventListener('resize', resizeAllCharts);
  const chartsHost = $('charts');
  if (chartsHost && window.ResizeObserver) {
    new ResizeObserver(resizeAllCharts).observe(chartsHost);
  }
});
