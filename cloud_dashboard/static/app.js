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

/* ---- axis / dataset helpers ---------------------------------------------- */
function mkDataset(label, data, color, yAxisID) {
  return { label, data, borderColor: color, backgroundColor: color + '22',
    borderWidth: 1.8, pointRadius: 0, fill: false, tension: 0.18, yAxisID };
}
function mkScale(pos, color, label, drawGrid, minRange) {
  return {
    type: 'linear', position: pos,
    // Chart.js auto-picks tick precision from the data range — on a nearly-flat
    // Current trace that means 4+ decimals of noise (-0.5330, -0.5332, ...).
    // Force 2 decimals to match every other reading on the page.
    ticks: { color, font: { size: 10 }, callback: (v) => Number(v).toFixed(2) },
    grid: { color: drawGrid ? css('--border') : 'rgba(0,0,0,0)' },
    border: { color, width: pos === 'right' ? 2 : 1 },
    title: { display: true, text: label, color, font: { size: 10 } },
    afterDataLimits: (scale) => {
      if (minRange && scale.max - scale.min < minRange) {
        const mid = (scale.max + scale.min) / 2;
        scale.min = mid - minRange / 2;
        scale.max = mid + minRange / 2;
      }
    }
  };
}
function baseOpts(extraScales) {
  return {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { intersect: false, mode: 'index' },
    scales: {
      x: {
        ticks: { color: css('--faint'), font: { size: 10 }, maxTicksLimit: 8 },
        grid: { color: css('--border') },
        title: { display: true, text: 'Elapsed (s)', color: css('--muted'), font: { size: 10 } },
      },
      ...extraScales,
    },
    plugins: {
      legend: { display: true, labels: { color: css('--muted'), font: { size: 11 }, boxWidth: 12 } },
      tooltip: { backgroundColor: '#0b1220', borderColor: 'rgba(255,255,255,.12)', borderWidth: 1 },
    },
  };
}

/* ---- build / destroy main charts ----------------------------------------- */
function destroyMainCharts() {
  Object.values(mainCharts).forEach(c => { try { c.destroy(); } catch(e){} });
  mainCharts = {};
  $('charts').innerHTML = '';
}
function addWrap(heightPx) {
  const host = $('charts');
  const wrap = document.createElement('div');
  wrap.className = 'chart-wrap';
  wrap.style.height = heightPx + 'px';
  const canvas = document.createElement('canvas');
  wrap.appendChild(canvas);
  host.appendChild(wrap);
  return canvas;
}
function buildMainCharts() {
  destroyMainCharts();
  const vC = css('--v'), iC = css('--i'), tC = css('--t');
  if (graphMode === 'combined') {
    const canvas = addWrap(420);
    mainCharts.combined = new Chart(canvas, {
      type: 'line',
      data: { labels: [], datasets: [
        mkDataset('Voltage (V)', [], vC, 'yV'),
        mkDataset('Current (A)', [], iC, 'yI'),
        mkDataset('Temp (°C)',   [], tC, 'yT'),
      ]},
      options: baseOpts({
        yV: mkScale('left',  vC, 'Voltage (V)', true, 0.2),
        yI: mkScale('right', iC, 'Current (A)', false, 0.5),
        yT: mkScale('right', tC, 'Temp (°C)',   false, 2.0),
      }),
    });
  } else if (graphMode === 'split2') {
    const c1 = addWrap(205);
    mainCharts.vc = new Chart(c1, {
      type: 'line',
      data: { labels: [], datasets: [mkDataset('Voltage (V)', [], vC, 'yV'), mkDataset('Current (A)', [], iC, 'yI')] },
      options: baseOpts({ yV: mkScale('left', vC, 'Voltage (V)', true, 0.2), yI: mkScale('right', iC, 'Current (A)', false, 0.5) }),
    });
    const c2 = addWrap(205);
    mainCharts.temp = new Chart(c2, {
      type: 'line',
      data: { labels: [], datasets: [mkDataset('Temp (°C)', [], tC, 'yT')] },
      options: baseOpts({ yT: mkScale('left', tC, 'Temp (°C)', true, 2.0) }),
    });
  } else {
    [['Voltage (V)', vC, 'yV', 0.2], ['Current (A)', iC, 'yI', 0.5], ['Temp (°C)', tC, 'yT', 2.0]].forEach(([label, color, axis, minRange]) => {
      const c = addWrap(135);
      mainCharts[axis] = new Chart(c, {
        type: 'line',
        data: { labels: [], datasets: [mkDataset(label, [], color, axis)] },
        options: baseOpts({ [axis]: mkScale('left', color, label, true, minRange) }),
      });
    });
  }
}
function updateMainCharts(ser) {
  const elapsed = (ser.Elapsed_s || []).map(v => v.toFixed(1));
  const vD = ser.Voltage_V || [], iD = ser.Current_A || [], tD = ser.Temperature_C || [];
  if (graphMode === 'combined' && mainCharts.combined) {
    const ch = mainCharts.combined;
    ch.data.labels = elapsed; ch.data.datasets[0].data = vD; ch.data.datasets[1].data = iD; ch.data.datasets[2].data = tD;
    ch.update('none');
  } else if (graphMode === 'split2') {
    if (mainCharts.vc)   { mainCharts.vc.data.labels = elapsed;   mainCharts.vc.data.datasets[0].data = vD; mainCharts.vc.data.datasets[1].data = iD; mainCharts.vc.update('none'); }
    if (mainCharts.temp) { mainCharts.temp.data.labels = elapsed; mainCharts.temp.data.datasets[0].data = tD; mainCharts.temp.update('none'); }
  } else {
    if (mainCharts.yV) { mainCharts.yV.data.labels = elapsed; mainCharts.yV.data.datasets[0].data = vD; mainCharts.yV.update('none'); }
    if (mainCharts.yI) { mainCharts.yI.data.labels = elapsed; mainCharts.yI.data.datasets[0].data = iD; mainCharts.yI.update('none'); }
    if (mainCharts.yT) { mainCharts.yT.data.labels = elapsed; mainCharts.yT.data.datasets[0].data = tD; mainCharts.yT.update('none'); }
  }
}

/* ---- ICA chart (in Diagnostics tab) -------------------------------------- */
function buildIcaChart() {
  const canvas = $('icaChart');
  if (!canvas) return;
  if (icaChart) { try { icaChart.destroy(); } catch(e){} icaChart = null; }
  icaChart = new Chart(canvas, {
    type: 'line',
    data: { datasets: [{ label: 'dQ/dV', data: [],
      borderColor: css('--soc'), backgroundColor: css('--soc') + '22',
      borderWidth: 1.5, pointRadius: 0, tension: 0.3, fill: false }] },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      scales: {
        x: { type: 'linear', title: { display: true, text: 'Voltage (V)', color: css('--muted'), font: { size: 9 } }, ticks: { color: css('--faint'), font: { size: 9 }, maxTicksLimit: 6 }, grid: { color: css('--border') } },
        y: { title: { display: true, text: 'dQ/dV', color: css('--muted'), font: { size: 9 } }, ticks: { color: css('--faint'), font: { size: 9 } }, grid: { color: css('--border') } },
      },
      plugins: { legend: { display: false }, tooltip: { backgroundColor: '#0b1220' } },
    },
  });
}
function updateIcaChart(voltageArr, socArr) {
  if (!icaChart) return;
  if (!voltageArr || !socArr || voltageArr.length < 5) { icaChart.data.datasets[0].data = []; icaChart.update('none'); return; }
  const pts = [];
  for (let i = 1; i < voltageArr.length; i++) {
    const dV = voltageArr[i] - voltageArr[i-1], dQ = socArr[i] - socArr[i-1];
    if (Math.abs(dV) > 1e-4) pts.push({ x: voltageArr[i], y: dQ / dV });
  }
  const w = 5;
  const smoothed = pts.map((p, i) => { const sl = pts.slice(Math.max(0,i-w),i+w+1); return { x: p.x, y: sl.reduce((s,q)=>s+q.y,0)/sl.length }; });
  icaChart.data.datasets[0].data = smoothed;
  icaChart.update('none');
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

  if (ccBadge) ccBadge.classList.toggle('active', isCC);
  if (cvBadge) cvBadge.classList.toggle('active', isCV);

  // Charge elapsed time — use elapsed_s from meta during charging phases
  if (chargeElEl) {
    if (isChargePhase || state === 'charging') {
      const el = num(meta, 'elapsed_s');
      chargeElEl.textContent = fmtElapsed(el);
    } else if (state === 'discharging') {
      // Also show elapsed for discharge phase
      const el = num(meta, 'elapsed_s');
      chargeElEl.textContent = fmtElapsed(el);
      // Update card label
      const tkEl = chargeElEl.closest('.tcard');
      if (tkEl) { const lbl = tkEl.querySelector('.tk'); if (lbl) lbl.textContent = 'ELAPSED'; }
    } else {
      chargeElEl.textContent = '--:--';
      // Reset label
      const tkEl = chargeElEl.closest('.tcard');
      if (tkEl) { const lbl = tkEl.querySelector('.tk'); if (lbl) lbl.textContent = 'CHARGE TIME'; }
    }
  }
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
  const subPhase = (meta.sub_phase || '').toLowerCase();
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
});
