"""
ASET Cloud Dashboard — บริการแสดงผลเทสต์แบตเตอรี่ 24 ชม. (stdlib ล้วน)

แยกจากแอปแล็บ: เครื่องแล็บ push ข้อมูลขึ้นมาที่ POST /api/ingest (auth ด้วย token)
service นี้แค่ "เก็บ snapshot ล่าสุด + เสิร์ฟ dashboard" — ไม่แตะฮาร์ดแวร์/ไม่ต้องมี numpy

Deploy ได้ทั้ง Heroku (Procfile) และ DigitalOcean/VM (รัน python server.py)
ENV:
  PORT          พอร์ต (Heroku ตั้งให้อัตโนมัติ; local default 8001)
  INGEST_TOKEN  token สำหรับ /api/ingest (ต้องตั้ง ไม่งั้น ingest ถูกปฏิเสธ)
  SNAPSHOT_PATH ไฟล์เก็บ snapshot ล่าสุด (default ./snapshot.json, best-effort)
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.environ.get("PORT", "8001"))
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")
SNAPSHOT_PATH = os.environ.get("SNAPSHOT_PATH", "snapshot.json")

# ---------------------------------------------------------------------------
# In-memory store (+ best-effort disk snapshot เผื่อ process restart)
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_store = {"payload": None, "received_at": 0.0}


def _load_snapshot() -> None:
    try:
        if os.path.exists(SNAPSHOT_PATH):
            with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            _store["payload"] = data.get("payload")
            _store["received_at"] = data.get("received_at", 0.0)
    except Exception:
        pass


def _save_snapshot() -> None:
    try:
        with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            json.dump(_store, f)
    except Exception:
        pass  # ระบบไฟล์ ephemeral (Heroku) ล้มได้ — ไม่เป็นไร


def _make_handler():
    class Handler(BaseHTTPRequestHandler):
        def _json(self, payload, status=200):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, text, status=200):
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # ---- ingest (จากเครื่องแล็บ) -------------------------------------
        def do_POST(self):  # noqa: N802
            if urlparse(self.path).path != "/api/ingest":
                self._json({"error": "not found"}, 404)
                return
            if not INGEST_TOKEN:
                self._json({"error": "server INGEST_TOKEN not configured"}, 503)
                return
            token = self.headers.get("X-Ingest-Token", "")
            if token != INGEST_TOKEN:
                self._json({"error": "unauthorized"}, 401)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                payload = json.loads(raw.decode("utf-8"))
            except Exception as e:
                self._json({"error": f"bad payload: {e}"}, 400)
                return
            with _lock:
                _store["payload"] = payload
                _store["received_at"] = time.time()
                _save_snapshot()
            self._json({"ok": True, "received_at": _store["received_at"]})

        # ---- serve (ให้ผู้ชม) --------------------------------------------
        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            try:
                if path in ("/", "/index.html"):
                    self._html(_INDEX_HTML)
                    return
                if path == "/api/health":
                    self._json({"ok": True, "time": time.time(),
                                "has_data": _store["payload"] is not None})
                    return

                with _lock:
                    payload = _store["payload"]
                    received_at = _store["received_at"]

                if path == "/api/snapshot":
                    self._json({"payload": payload, "received_at": received_at})
                    return
                if payload is None:
                    self._json({"error": "no data yet"}, 404)
                    return
                if path == "/api/summary":
                    out = dict(payload.get("summary", {}))
                    out["meta"] = payload.get("meta", {})
                    out["received_at"] = received_at
                    self._json(out)
                    return
                if path == "/api/analysis":
                    self._json(payload.get("analysis", {}))
                    return
                if path == "/api/series":
                    self._json(payload.get("series", {}))
                    return
                self._json({"error": "not found"}, 404)
            except (ConnectionError, BrokenPipeError):
                return
            except Exception as e:
                try:
                    self._json({"error": str(e)}, 500)
                except Exception:
                    pass

        def log_message(self, *args):  # ลด log noise
            return

    return Handler


_INDEX_HTML = """<!doctype html>
<html><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>ASET Cloud Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  body{margin:0;font-family:Arial,sans-serif;background:#0b1220;color:#e5e7eb}
  .wrap{padding:16px;max-width:1200px;margin:0 auto}
  h1{font-size:20px;margin:0 0 4px}
  .sub{color:#a3a3a3;font-size:13px;margin-bottom:12px}
  .pill{display:inline-block;padding:2px 8px;border-radius:999px;background:#142033;border:1px solid rgba(255,255,255,.08);font-size:12px}
  .ok{color:#34d399}.stale{color:#f59e0b}
  .metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:12px 0}
  .card{background:#0f172a;border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:12px 14px}
  .k{color:#a3a3a3;font-size:12px}.v{font-weight:bold;font-size:22px;margin-top:2px}
  .u{color:#6b7280;font-size:12px;font-weight:normal}
  .row{display:flex;gap:16px;flex-wrap:wrap}
  table{width:100%;border-collapse:collapse;font-size:13px}
  td{padding:5px 8px;border-bottom:1px solid rgba(255,255,255,.06)}
  td:first-child{color:#a3a3a3}
  canvas{max-height:260px}
  .grade-banner{display:flex;align-items:center;gap:12px;padding:10px 14px;border-radius:8px;margin-bottom:10px}
  .grade-letter{font-size:48px;font-weight:900;line-height:1}
  .grade-detail{font-size:13px;color:#a3a3a3;line-height:1.6}
  .grade-detail b{color:#e5e7eb}
</style></head>
<body><div class="wrap">
  <h1>&#x1F50B; ASET Cloud Dashboard</h1>
  <div class="sub">
    เสิร์ฟจากคลาวด์ (เครื่องแล็บ push ข้อมูลขึ้นมา) &middot;
    <span class="pill" id="battery">battery: &#x2013;</span>
    <span class="pill">updated: <span id="updated">&#x2013;</span></span>
    <span class="pill" id="livePill">status: <span id="live">&#x2026;</span></span>
  </div>

  <!-- Live metrics -->
  <div class="metrics">
    <div class="card"><div class="k">Voltage</div><div class="v"><span id="mV">-</span> <span class="u">V</span></div></div>
    <div class="card"><div class="k">Current</div><div class="v"><span id="mI">-</span> <span class="u">A</span></div></div>
    <div class="card"><div class="k">SoC</div><div class="v"><span id="mSoC">-</span> <span class="u">%</span></div></div>
    <div class="card"><div class="k">Resistance</div><div class="v"><span id="mR">-</span> <span class="u">m&#x3a9;</span></div></div>
    <div class="card"><div class="k">Temperature</div><div class="v"><span id="mT">-</span> <span class="u">&#x00b0;C</span></div></div>
  </div>

  <!-- Charts -->
  <div class="row">
    <div class="card" style="flex:1 1 520px"><canvas id="chartV"></canvas></div>
    <div class="card" style="flex:1 1 520px"><canvas id="chartSoC"></canvas></div>
  </div>

  <!-- Analytics + Summary -->
  <div class="row" style="margin-top:16px">

    <!-- Analytics (mirrors app Analytics tab) -->
    <div class="card" style="flex:1 1 360px">
      <div class="k" style="font-size:13px;font-weight:600;margin-bottom:8px">&#x1F4CA; Analytics</div>
      <div class="grade-banner" id="gradeBanner" style="background:rgba(239,68,68,.12)">
        <div class="grade-letter" id="grade">&#x2013;</div>
        <div class="grade-detail" id="gradeDetail">SoH &#x2013; &middot; Cap &#x2013;</div>
      </div>
      <table>
        <tr><td>R0 (ohmic)</td><td><b id="r0">-</b> m&#x3a9;</td></tr>
        <tr><td>R1 (charge-transfer)</td><td><b id="r1">-</b> m&#x3a9;</td></tr>
        <tr><td>C1</td><td><b id="c1">-</b> F</td></tr>
        <tr><td>&#x03c4; (R1&#x00b7;C1)</td><td><b id="tau">-</b> s</td></tr>
        <tr><td>Total DCIR (R0+R1)</td><td><b id="dcir">-</b> m&#x3a9;</td></tr>
      </table>
    </div>

    <!-- Discharge summary -->
    <div class="card" style="flex:1 1 260px">
      <div class="k" style="font-size:13px;font-weight:600;margin-bottom:8px">&#x1F4DD; Discharge Summary</div>
      <table>
        <tr><td>Capacity</td><td><b id="cap">-</b> Ah</td></tr>
        <tr><td>Energy</td><td><b id="energy">-</b> Wh</td></tr>
        <tr><td>SoH</td><td><b id="soh">-</b> %</td></tr>
        <tr><td>Avg Voltage</td><td><b id="avgV">-</b> V</td></tr>
        <tr><td>Rows</td><td><b id="rows">-</b></td></tr>
      </table>
    </div>

  </div>
</div>
<script>
const f=(x,d=2)=>(x==null||x===''||isNaN(+x))?'-':Number(x).toFixed(d);
const GRADE_COLOR={A:'#34d399',B:'#a3e635',C:'#f59e0b',D:'#ef4444',REJECT:'#ef4444'};
let cV,cSoC;
function mkChart(id,label,color){
  return new Chart(document.getElementById(id),{type:'line',
    data:{labels:[],datasets:[{label,data:[],borderColor:color,borderWidth:1.5,pointRadius:0,tension:.15}]},
    options:{responsive:true,animation:false,
      scales:{x:{ticks:{color:'#6b7280',maxTicksLimit:8}},y:{ticks:{color:'#6b7280'}}},
      plugins:{legend:{labels:{color:'#a3a3a3'}}}}});
}
async function load(){
  try{
    const snap=await (await fetch('/api/snapshot?t='+Date.now())).json();
    if(!snap.payload){document.getElementById('live').textContent='no data yet';return;}
    const p=snap.payload, s=p.summary||{}, a=p.analysis||{}, ser=p.series||{}, L=s.latest||{};

    // header
    document.getElementById('battery').textContent='battery: '+((p.meta||{}).battery||'&#x2013;');

    // live metrics
    document.getElementById('mV').textContent=f(L.Voltage_V,3);
    document.getElementById('mI').textContent=f(L.Current_A,3);
    document.getElementById('mSoC').textContent=f(L.SoC_pct,1);
    document.getElementById('mR').textContent=f(L.Resistance_mOhm,1);
    document.getElementById('mT').textContent=f(L.Temperature_C,2);

    // summary
    document.getElementById('cap').textContent=f(s.capacity_ah,3);
    document.getElementById('energy').textContent=f(s.energy_wh,2);
    document.getElementById('avgV').textContent=f(s.avg_voltage_v,3);
    document.getElementById('rows').textContent=s.row_count??'-';

    // analytics
    if(a.grade){
      const gc=GRADE_COLOR[a.grade]||'#e5e7eb';
      const gradeEl=document.getElementById('grade');
      gradeEl.textContent=a.grade;
      gradeEl.style.color=gc;
      document.getElementById('gradeBanner').style.background='rgba('+
        (a.grade==='A'?'52,211,153':(a.grade==='B'?'163,230,53':(a.grade==='C'?'245,158,11':'239,68,68')))+
        ',.10)';
      const sohPct=a.soh!=null?f(a.soh*100,1)+' %':'&#x2013;';
      const capAh=a.capacity_ah!=null?f(a.capacity_ah,3)+' Ah':'&#x2013;';
      document.getElementById('gradeDetail').innerHTML='SoH '+sohPct+' &middot; Cap '+capAh;
      document.getElementById('soh').textContent=a.soh!=null?f(a.soh*100,1):'-';
      document.getElementById('r0').textContent=f(a.r0_mohm,2);
      document.getElementById('r1').textContent=f(a.r1_mohm,2);
      document.getElementById('c1').textContent=f(a.c1_farad,0);
      document.getElementById('tau').textContent=f(a.tau_s,1);
      document.getElementById('dcir').textContent=f(a.ri_mohm,2);
    }

    // charts
    const x=(ser.Elapsed_s||[]).map(v=>Math.round(v));
    cV.data.labels=x; cV.data.datasets[0].data=ser.Voltage_V||[]; cV.update();
    cSoC.data.labels=x; cSoC.data.datasets[0].data=ser.SoC_pct||[]; cSoC.update();

    // age / live indicator
    const ageMs=Date.now()-snap.received_at*1000;
    document.getElementById('updated').textContent=Math.round(ageMs/1000)+'s ago';
    const liveEl=document.getElementById('live');
    const live=ageMs<60000;
    liveEl.textContent=live?'LIVE':'idle'; liveEl.className=live?'ok':'stale';
  }catch(e){document.getElementById('live').textContent='error: '+e;}
}
window.addEventListener('load',()=>{
  cV=mkChart('chartV','Voltage (V)','#00b3a4');
  cSoC=mkChart('chartSoC','SoC (%)','#3b82f6');
  load(); setInterval(load,5000);
});
</script>
</body></html>
"""


def main():
    if not INGEST_TOKEN:
        print("WARNING: INGEST_TOKEN ไม่ได้ตั้ง — /api/ingest จะถูกปฏิเสธทั้งหมด")
    _load_snapshot()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), _make_handler())
    print(f"ASET Cloud Dashboard listening on :{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
