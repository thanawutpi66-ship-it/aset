/* ===========================================================================
   ASET Cloud Dashboard — frontend logic
   Data source: GET /api/snapshot  ->  { payload:{meta,summary,analysis,series}, received_at }
   Tolerant of BOTH the unified analyze_csv dict AND the legacy {success,features} shape.
   =========================================================================== */
'use strict';

/* ---- small helpers ------------------------------------------------------- */
const $ = (id) => document.getElementById(id);
const f = (x, d = 2) => (x == null || x === '' || isNaN(x)) ? '–' : Number(x).toFixed(d);
const num = (o, ...keys) => { for (const k of keys) { if (o && o[k] != null && !isNaN(o[k])) return Number(o[k]); } return null; };
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));
function fmtR(mOhm){ if (mOhm == null) return '–'; return mOhm >= 1000 ? (mOhm/1000).toFixed(3)+' Ω' : mOhm.toFixed(1)+' mΩ'; }

const TEMP_CRIT = 55;     // °C — trips the emergency overlay
let isMuted = false;

/* ---- radial gauges ------------------------------------------------------- */
const R = 82;                                   // must match the SVG circle r
const CIRC = 2 * Math.PI * R;                   // full circumference
const ARC = CIRC * 0.75;                        // 270° sweep
function initGauge(arcId){
  const el = $(arcId);
  el.style.strokeDasharray = ARC + ' ' + CIRC;
  el.style.strokeDashoffset = ARC;              // start empty
  // track shares the same dash so it draws the 270° arc too
  const track = el.previousElementSibling;
  if (track) { track.style.strokeDasharray = ARC + ' ' + CIRC; track.style.strokeDashoffset = 0; }
}
/* set gauge to fraction 0..1 with a colour */
function setGauge(arcId, valEl, value, color){
  const arc = $(arcId);
  if (value == null) {
    arc.style.strokeDashoffset = ARC;
    valEl.textContent = '–';
    return;
  }
  const frac = clamp(value / 100, 0, 1);
  arc.style.strokeDashoffset = ARC * (1 - frac);
  arc.style.stroke = color;
  arc.style.filter = 'drop-shadow(0 0 6px ' + color + '88)';
  valEl.textContent = Math.round(value);
}
/* colour ramps */
function socColor(v){ return v >= 60 ? css('--ok') : v >= 25 ? css('--t') : css('--crit'); }
function sohColor(v){ return v >= 85 ? css('--ok') : v >= 70 ? css('--t') : css('--crit'); }
function socWord(v){ return v >= 80 ? 'เต็ม' : v >= 50 ? 'ปานกลาง' : v >= 20 ? 'ต่ำ' : 'ใกล้หมด'; }
function sohWord(v){ return v >= 90 ? 'ดีเยี่ยม' : v >= 80 ? 'ดี' : v >= 70 ? 'พอใช้' : 'เสื่อม'; }

/* ---- charts -------------------------------------------------------------- */
const CH = [
  { key:'Voltage_V',      id:'cV',   label:'Voltage (V)',     v:'--v'  },
  { key:'Current_A',      id:'cI',   label:'Current (A)',     v:'--i'  },
  { key:'SoC_pct',        id:'cSoC', label:'SoC (%)',         v:'--soc'},
  { key:'Resistance_mOhm',id:'cR',   label:'Resistance (mΩ)', v:'--r'  },
  { key:'Temperature_C',  id:'cT',   label:'Temperature (°C)',v:'--t'  },
];
const charts = {};
function mkChart(id, label, color){
  return new Chart($(id), {
    type:'line',
    data:{ labels:[], datasets:[{ label, data:[], borderColor:color,
      backgroundColor:color+'22', borderWidth:1.8, pointRadius:0, fill:true, tension:.18 }] },
    options:{
      responsive:true, maintainAspectRatio:true, animation:false,
      interaction:{ intersect:false, mode:'index' },
      scales:{
        x:{ grid:{ color:'rgba(255,255,255,.04)' }, ticks:{ color:css('--faint'), maxTicksLimit:7, font:{size:10} } },
        y:{ grid:{ color:'rgba(255,255,255,.05)' }, ticks:{ color:css('--faint'), font:{size:10} } },
      },
      plugins:{ legend:{ display:false },
        tooltip:{ backgroundColor:'#0b1220', borderColor:'rgba(255,255,255,.12)', borderWidth:1 } },
    },
  });
}
function ensureCharts(series){
  const host = $('charts');
  for (const c of CH){
    const has = Array.isArray(series[c.key]) && series[c.key].length;
    if (has && !charts[c.id]){
      const color = css(c.v);
      const card = document.createElement('div');
      card.className = 'chart-card';
      card.innerHTML = '<h3><span class="swatch" style="background:'+color+'"></span>'+c.label+'</h3>'
                     + '<canvas id="'+c.id+'"></canvas>';
      host.appendChild(card);
      charts[c.id] = mkChart(c.id, c.label, color);
    }
  }
}

/* ---- safety / overlay ---------------------------------------------------- */
function setSafety(alarm, msg){
  const box = $('safety'), ov = $('overlay');
  if (alarm){
    box.textContent = 'ALARM'; box.style.background = css('--crit');
    if (!isMuted){ ov.style.display = 'flex'; $('overlayMsg').textContent = msg; }
  } else {
    box.textContent = 'NORMAL'; box.style.background = css('--ok');
    ov.style.display = 'none'; isMuted = false;
  }
}
function muteAlarm(){ isMuted = true; $('overlay').style.display = 'none'; }
window.muteAlarm = muteAlarm;

/* ---- main load loop ------------------------------------------------------ */
async function load(){
  try{
    const snap = await (await fetch('/api/snapshot?t=' + Date.now())).json();
    const wait = $('waiting'), content = $('content'), pill = $('livePill');

    if (!snap.payload){
      wait.style.display = 'block'; content.hidden = true;
      $('live').textContent = 'waiting'; pill.className = 'pill stale';
      return;
    }
    wait.style.display = 'none'; content.hidden = false;

    const p = snap.payload, s = p.summary || {}, a = p.analysis || {},
          ser = p.series || {}, L = s.latest || {}, feat = a.features || {};

    $('battery').innerHTML = '<i class="dot"></i>battery: <b>' + ((p.meta||{}).battery || '–') + '</b>';

    /* --- gauges --- */
    const soc = num(L,'SoC_pct');
    const soh = num(a,'soh') ?? num(feat,'soh_pct');
    if (soc != null){ setGauge('socArc', $('socVal'), soc, socColor(soc));
      $('socFoot').innerHTML = 'สถานะ: <b>'+socWord(soc)+'</b>'; }
    if (soh != null){ setGauge('sohArc', $('sohVal'), soh, sohColor(soh));
      $('sohFoot').innerHTML = 'สภาพ: <b>'+sohWord(soh)+'</b>'; }

    /* --- sorting grade --- */
    const grade = a.grade || '–';
    const gEl = $('grade');
    gEl.textContent = grade;
    gEl.style.fontSize = grade.length > 2 ? '16px' : '';
    gEl.style.color = css('--g'+grade) || css('--text');
    gEl.style.borderColor = (css('--g'+grade) || css('--border-2'));
    $('gradeMeta').textContent = sohWord(soh ?? 0) + ' battery';
    const conf = num(a,'confidence');
    $('confFill').style.width = conf != null ? Math.round(conf*100)+'%' : '0%';
    $('confTxt').textContent = conf != null ? 'confidence ' + Math.round(conf*100) + '%' : '';

    $('cap').textContent    = f(num(a,'capacity_ah') ?? num(s,'capacity_ah'), 2);
    $('dcir').textContent   = f(num(a,'dcir_mohm','ri_mohm'), 1);
    $('sag').textContent    = f(num(a,'voltage_sag_v'), 3);
    $('cca').textContent    = f(num(a,'cca_est_a'), 0);
    $('ocv').textContent    = f(num(a,'ocv_v'), 3);
    $('method').textContent = a.method || (a.ecm_identified ? 'ECM' : '–');

    /* --- live telemetry --- */
    const V = num(L,'Voltage_V'), I = num(L,'Current_A'), T = num(L,'Temperature_C');
    $('mV').textContent = f(V, 3);
    $('mI').textContent = f(I, 3);
    $('mR').textContent = fmtR(num(L,'Resistance_mOhm'));
    $('mT').textContent = f(T, 1);

    /* --- charts --- */
    ensureCharts(ser);
    const x = (ser.Elapsed_s || []).map(v => Math.round(v/60) + 'm');
    for (const c of CH){
      const ch = charts[c.id]; if (!ch) continue;
      ch.data.labels = x; ch.data.datasets[0].data = ser[c.key] || []; ch.update('none');
    }

    /* --- freshness + footer --- */
    const ageMs = Date.now() - snap.received_at * 1000;
    const ageS = Math.max(0, Math.round(ageMs/1000));
    $('updated').textContent = ageS < 90 ? ageS+'s ago' : Math.round(ageS/60)+'m ago';
    const live = ageMs < 60000;
    $('live').textContent = live ? 'LIVE' : 'idle';
    $('livePill').className = 'pill ' + (live ? 'live' : 'stale');
    if (s.row_count != null) $('rowInfo').textContent = s.row_count + ' samples · ' + f(s.energy_wh,1) + ' Wh logged';

    /* --- safety --- */
    const explicitAlarm = (s.safety_status || (p.meta||{}).safety_status) === 'ALARM';
    const hot = T != null && T >= TEMP_CRIT;
    setSafety(explicitAlarm || hot,
      'TEMP: ' + f(T,1) + '°C · GRADE: ' + grade + (explicitAlarm ? ' · SAFETY ALARM' : ''));

  } catch(e){
    $('live').textContent = 'error';
    $('livePill').className = 'pill off';
    const sb = $('safety'); if (sb){ sb.textContent = 'OFFLINE'; sb.style.background = css('--faint'); }
  }
}

/* boot once Chart.js (defer) and DOM are ready */
window.addEventListener('DOMContentLoaded', () => {
  initGauge('socArc');
  initGauge('sohArc');
  load();
  setInterval(load, 5000);
});
