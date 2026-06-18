import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import csv
import io
from typing import Any, Dict, List, Optional, Tuple

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _tail_csv_rows(csv_path: str, limit: int = 200) -> List[Dict[str, str]]:
    if not os.path.exists(csv_path):
        return []

    # Read entire file is okay for moderate sizes; we only tail for UI.
    # If it becomes large, we can implement streaming tail.
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    return rows[-limit:]


def _get_latest_csv_path(config: Any) -> str:
    # config.system.csv_filepath is already used by DataHandler.start_logging
    return getattr(config.system, "csv_filepath")


def _extract_series(rows: List[Dict[str, str]], keys: List[str]) -> Dict[str, List[float]]:
    out: Dict[str, List[float]] = {k: [] for k in keys}
    for r in rows:
        for k in keys:
            try:
                out[k].append(float(r.get(k, "")))
            except Exception:
                # skip invalid
                pass
    return out


# Channels shown on the dashboard (besides Elapsed_s/Timestamp)
_CHANNELS = ["Voltage_V", "Current_A", "SoC_pct", "Resistance_mOhm", "Temperature_C"]


def _stats(vals: List[float]) -> Dict[str, Optional[float]]:
    if not vals:
        return {"min": None, "max": None, "avg": None}
    return {"min": min(vals), "max": max(vals), "avg": sum(vals) / len(vals)}


def _compute_summary(rows: List[Dict[str, str]]) -> Dict[str, Any]:
    """สรุปข้อมูลครบทุกช่องวัด: latest, min/max/avg, capacity, energy"""
    series = _extract_series(rows, keys=_CHANNELS + ["Elapsed_s"])
    elapsed = series.get("Elapsed_s", [])
    current = series.get("Current_A", [])
    voltage = series.get("Voltage_V", [])

    # coulomb counting ของช่วง discharge (Current_A > 0) -> capacity / energy
    capacity_ah = 0.0
    energy_wh = 0.0
    n = min(len(elapsed), len(current), len(voltage))
    for k in range(1, n):
        dt_h = (elapsed[k] - elapsed[k - 1]) / 3600.0
        i_mid = (current[k] + current[k - 1]) / 2.0
        v_mid = (voltage[k] + voltage[k - 1]) / 2.0
        if i_mid > 0:
            capacity_ah += i_mid * dt_h
            energy_wh += i_mid * v_mid * dt_h

    latest = rows[-1] if rows else {}
    return {
        "row_count": len(rows),
        "elapsed_s": elapsed[-1] if elapsed else 0.0,
        "latest": {k: latest.get(k) for k in _CHANNELS},
        "stats": {k: _stats(series.get(k, [])) for k in _CHANNELS},
        "capacity_ah": capacity_ah,
        "energy_wh": energy_wh,
    }


def _render_main_plot(rows: List[Dict[str, str]]) -> bytes:
    """กราฟรวม 6 ช่อง: Voltage, Current, SoC, Temperature, Resistance, Power"""
    s = _extract_series(rows, keys=_CHANNELS + ["Elapsed_s"])
    x = s.get("Elapsed_s", [])

    fig, axes = plt.subplots(3, 2, figsize=(12, 9), dpi=110)
    fig.patch.set_facecolor("white")
    specs = [
        ("Voltage_V", "Voltage (V)", "#00b3a4"),
        ("Current_A", "Current (A)", "#f59e0b"),
        ("SoC_pct", "SoC (%)", "#3b82f6"),
        ("Temperature_C", "Temperature (C)", "#ef4444"),
        ("Resistance_mOhm", "Resistance (mOhm)", "#a855f7"),
    ]
    for ax, (key, label, color) in zip(axes.flat, specs):
        y = s.get(key, [])
        m = min(len(x), len(y))
        ax.plot(x[:m], y[:m], color=color, linewidth=1.8)
        ax.set_title(label, fontsize=11)
        ax.set_xlabel("Elapsed_s", fontsize=8)
        ax.grid(True, alpha=0.25)

    # ช่องที่ 6: Power (W) = V * I
    ax_p = axes.flat[5]
    v = s.get("Voltage_V", [])
    i = s.get("Current_A", [])
    m = min(len(x), len(v), len(i))
    power = [v[k] * i[k] for k in range(m)]
    ax_p.plot(x[:m], power, color="#10b981", linewidth=1.8)
    ax_p.set_title("Power (W)", fontsize=11)
    ax_p.set_xlabel("Elapsed_s", fontsize=8)
    ax_p.grid(True, alpha=0.25)

    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def _run_analysis(config: Any, csv_path: str) -> Dict[str, Any]:
    """รัน BatteryAnalyzer บนไฟล์ CSV แล้วคืนผลเป็น dict (สำหรับ /api/analysis)"""
    from analysis_module import BatteryAnalyzer

    rated = getattr(config.battery, "rated_capacity", 2.0)
    base_r0 = 25.0
    try:
        from battery_model import BatteryModel
        bm = BatteryModel(config.battery.battery_type, config.battery.nominal_voltage)
        base_r0 = bm.rin_params["r0"] * 1000.0
    except Exception:
        pass

    analyzer = BatteryAnalyzer(rated_capacity_ah=rated, base_r0_mohm=base_r0)
    return analyzer.analyze(csv_path).to_dict()


class ASETWebServer:
    def __init__(self, config: Any, host: str = "0.0.0.0", port: int = 8000):
        self.config = config
        self.host = host
        self.port = port
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        # cache สำหรับกราฟรวม (render หนัก ~2-3s) เพื่อรองรับผู้ชมหลายคนพร้อมกัน
        self._plot_cache: Dict[str, Any] = {"ts": 0.0, "data": b""}
        self._plot_lock = threading.Lock()
        self._plot_ttl = 3.0  # วินาที
        # cache สำหรับผลวิเคราะห์ AI (analyze หนัก -> เก็บนานกว่า plot)
        self._analysis_cache: Dict[str, Any] = {"ts": 0.0, "data": None}
        self._analysis_lock = threading.Lock()
        self._analysis_ttl = 15.0  # วินาที

    def start(self) -> None:
        if self._server is not None:
            return

        handler = self._make_handler()

        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None

    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_text(self, text: str, status: int = 200) -> None:
                body = text.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path
                query = parse_qs(parsed.query)

                try:
                    if path == "/" or path == "/index.html":
                        self._send_text(server._index_html())
                        return

                    if path == "/api/last":
                        limit = _safe_int(query.get("limit", ["200"])[0], 200)
                        csv_path = _get_latest_csv_path(server.config)
                        rows = _tail_csv_rows(csv_path, limit=limit)
                        self._send_json({
                            "csv_path": csv_path,
                            "row_count": len(rows),
                            "rows": rows,
                        })
                        return

                    if path == "/api/summary":
                        limit = _safe_int(query.get("limit", ["2000"])[0], 2000)
                        csv_path = _get_latest_csv_path(server.config)
                        rows = _tail_csv_rows(csv_path, limit=limit)
                        payload = _compute_summary(rows)
                        payload["csv_path"] = csv_path
                        self._send_json(payload)
                        return

                    if path == "/api/analysis":
                        now = time.time()
                        with server._analysis_lock:
                            cache = server._analysis_cache
                            if cache["data"] is not None and (now - cache["ts"]) < server._analysis_ttl:
                                payload = cache["data"]
                            else:
                                csv_path = _get_latest_csv_path(server.config)
                                payload = _run_analysis(server.config, csv_path)
                                cache["data"] = payload
                                cache["ts"] = now
                        self._send_json(payload)
                        return

                    if path == "/plot/main.png":
                        now = time.time()
                        with server._plot_lock:
                            cache = server._plot_cache
                            if cache["data"] and (now - cache["ts"]) < server._plot_ttl:
                                data = cache["data"]
                            else:
                                csv_path = _get_latest_csv_path(server.config)
                                rows = _tail_csv_rows(csv_path, limit=_safe_int(query.get("limit", ["2000"])[0], 2000))
                                data = _render_main_plot(rows)
                                cache["data"] = data
                                cache["ts"] = now
                        self.send_response(200)
                        self.send_header("Content-Type", "image/png")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                        return

                    if path == "/plot/soc.png":
                        csv_path = _get_latest_csv_path(server.config)
                        rows = _tail_csv_rows(csv_path, limit=_safe_int(query.get("limit", ["500"])[0], 500))
                        series = _extract_series(rows, keys=["Elapsed_s", "SoC_pct"])
                        elapsed = series.get("Elapsed_s", [])
                        soc = series.get("SoC_pct", [])
                        fig = plt.figure(figsize=(10, 4), dpi=120)
                        plt.plot(elapsed, soc, color="#00b3a4", linewidth=2)
                        plt.xlabel("Elapsed_s")
                        plt.ylabel("SoC_pct")
                        plt.grid(True, alpha=0.25)
                        plt.tight_layout()

                        buf = io.BytesIO()
                        fig.savefig(buf, format="png")
                        plt.close(fig)
                        data = buf.getvalue()

                        self.send_response(200)
                        self.send_header("Content-Type", "image/png")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                        return

                    if path == "/api/health":
                        self._send_json({"ok": True, "time": time.time()})
                        return

                    self._send_json({"error": "not found"}, status=404)
                except (ConnectionError, ConnectionAbortedError,
                        ConnectionResetError, BrokenPipeError):
                    # client ปิด connection กลางคัน (เช่นรีเฟรชรูปซ้ำ) — ไม่ใช่ error จริง
                    return
                except Exception as e:
                    try:
                        self._send_json({"error": str(e)}, status=500)
                    except Exception:
                        pass

            def log_message(self, format, *args):  # noqa: A002
                # keep quiet for cleaner logs
                return

        return Handler

    def _index_html(self) -> str:
        # Complete read-only dashboard. No external deps.
        return """<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>ASET Lab - Live Battery Dashboard</title>
  <style>
    body { margin: 0; font-family: Arial, sans-serif; background: #0b1220; color: #e5e7eb; }
    .wrap { padding: 16px; max-width: 1200px; margin: 0 auto; }
    .row { display: flex; gap: 16px; flex-wrap: wrap; }
    .card { background: #0f172a; border: 1px solid rgba(255,255,255,0.08); border-radius: 12px; padding: 14px; }
    h1 { font-size: 20px; margin: 0 0 4px; }
    .sub { color: #a3a3a3; margin-bottom: 12px; font-size: 13px; }
    button { background: #00b3a4; border: 0; color: #03131a; padding: 9px 14px; border-radius: 10px; font-weight: bold; cursor: pointer; }
    button:disabled { opacity: 0.6; cursor: not-allowed; }
    img { width: 100%; height: auto; border-radius: 10px; border: 1px solid rgba(255,255,255,0.08); background: white; }
    pre { white-space: pre-wrap; word-break: break-word; background: rgba(0,0,0,0.35); padding: 10px; border-radius: 10px; border: 1px solid rgba(255,255,255,0.08); font-size: 12px; }
    .small { font-size: 12px; color: #a3a3a3; }
    .metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin: 12px 0; }
    .metric { background: #0f172a; border: 1px solid rgba(255,255,255,0.08); border-radius: 12px; padding: 12px 14px; }
    .k { color: #a3a3a3; font-size: 12px; }
    .v { font-weight: bold; font-size: 22px; margin-top: 2px; }
    .u { color: #6b7280; font-size: 12px; font-weight: normal; }
    .mm { color: #6b7280; font-size: 11px; margin-top: 4px; }
    .pill { display:inline-block; padding:2px 8px; border-radius:999px; font-size:12px; background:#142033; border:1px solid rgba(255,255,255,0.08); }
    .ok { color:#34d399; } .stale { color:#f59e0b; }
    a { color: #00b3a4; }
    table { width:100%; border-collapse: collapse; font-size:12px; }
    td,th { text-align:left; padding:4px 8px; border-bottom:1px solid rgba(255,255,255,0.06); }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>🔋 ASET Lab — Live Battery Dashboard</h1>
    <div class="sub">
      Read-only live view of the latest test. Auto-refresh every 2s &middot;
      <span class="pill">CSV: <span id="csvPath">-</span></span>
      <span class="pill">Rows: <span id="rowCount">-</span></span>
      <span class="pill">Elapsed: <span id="elapsed">-</span> s</span>
      <span class="pill" id="liveDot">status: <span id="liveTxt">…</span></span>
    </div>

    <div class="metrics">
      <div class="metric"><div class="k">Voltage</div><div class="v"><span id="mV">-</span> <span class="u">V</span></div><div class="mm" id="rV"></div></div>
      <div class="metric"><div class="k">Current</div><div class="v"><span id="mI">-</span> <span class="u">A</span></div><div class="mm" id="rI"></div></div>
      <div class="metric"><div class="k">State of Charge</div><div class="v"><span id="mSoC">-</span> <span class="u">%</span></div><div class="mm" id="rSoC"></div></div>
      <div class="metric"><div class="k">Resistance</div><div class="v"><span id="mR">-</span> <span class="u">mΩ</span></div><div class="mm" id="rR"></div></div>
      <div class="metric"><div class="k">Temperature</div><div class="v"><span id="mT">-</span> <span class="u">°C</span></div><div class="mm" id="rT"></div></div>
    </div>

    <div class="row">
      <div class="card" style="flex: 2 1 600px;">
        <div class="k" style="margin-bottom:8px;">Measurements vs Elapsed time</div>
        <img id="mainPlot" src="/plot/main.png" alt="measurement plots"/>
      </div>
      <div class="card" style="flex: 1 1 280px;">
        <div class="k">Discharge summary</div>
        <table style="margin:8px 0 14px;">
          <tr><td>Capacity (discharged)</td><td><b id="capAh">-</b> Ah</td></tr>
          <tr><td>Energy (discharged)</td><td><b id="energyWh">-</b> Wh</td></tr>
          <tr><td>Avg voltage</td><td><b id="avgV">-</b> V</td></tr>
          <tr><td>Avg current</td><td><b id="avgI">-</b> A</td></tr>
          <tr><td>Max temperature</td><td><b id="maxT">-</b> °C</td></tr>
        </table>
        <button id="refreshBtn">Refresh now</button>
        <div style="margin-top:12px;">
          <div class="k">Latest row</div>
          <pre id="preview">-</pre>
        </div>
      </div>

      <div class="card" style="flex: 1 1 240px;">
        <div class="k">AI Grade (offline analysis)</div>
        <div class="v" id="aiGrade" style="font-size:42px; line-height:1.1;">–</div>
        <div class="mm" id="aiMeta"></div>
        <table style="margin:8px 0 10px;">
          <tr><td>SoH</td><td><b id="aiSoH">-</b> %</td></tr>
          <tr><td>R0 (ohmic)</td><td><b id="aiR0">-</b> mΩ</td></tr>
          <tr><td>Rp (polar.)</td><td><b id="aiRp">-</b> mΩ</td></tr>
          <tr><td>τ (RC)</td><td><b id="aiTau">-</b> s</td></tr>
          <tr><td>Pulses fitted</td><td><b id="aiPulses">-</b></td></tr>
        </table>
        <button id="analyzeBtn">Run AI analysis</button>
        <div class="mm" id="aiNotes" style="margin-top:6px;"></div>
      </div>
    </div>
  </div>

<script>
  const f1 = (x, d=3) => (x === null || x === undefined || x === '' || isNaN(x)) ? '-' : Number(x).toFixed(d);
  let lastElapsed = null, lastSeenAt = Date.now();

  async function refresh() {
    try {
      const res = await fetch('/api/summary?t=' + Date.now());
      const s = await res.json();
      document.getElementById('csvPath').textContent = (s.csv_path || '-').split(/[\\\\/]/).pop();
      document.getElementById('rowCount').textContent = s.row_count ?? '-';
      document.getElementById('elapsed').textContent = f1(s.elapsed_s, 1);

      const L = s.latest || {};
      document.getElementById('mV').textContent = f1(L.Voltage_V, 3);
      document.getElementById('mI').textContent = f1(L.Current_A, 3);
      document.getElementById('mSoC').textContent = f1(L.SoC_pct, 1);
      document.getElementById('mR').textContent = f1(L.Resistance_mOhm, 1);
      document.getElementById('mT').textContent = f1(L.Temperature_C, 2);

      const st = s.stats || {};
      const mm = (o, d=2) => o && o.min !== null ? ('min ' + f1(o.min, d) + ' / max ' + f1(o.max, d)) : '';
      document.getElementById('rV').textContent = mm(st.Voltage_V, 3);
      document.getElementById('rI').textContent = mm(st.Current_A, 3);
      document.getElementById('rSoC').textContent = mm(st.SoC_pct, 1);
      document.getElementById('rR').textContent = mm(st.Resistance_mOhm, 1);
      document.getElementById('rT').textContent = mm(st.Temperature_C, 2);

      document.getElementById('capAh').textContent = f1(s.capacity_ah, 3);
      document.getElementById('energyWh').textContent = f1(s.energy_wh, 2);
      document.getElementById('avgV').textContent = f1(st.Voltage_V && st.Voltage_V.avg, 3);
      document.getElementById('avgI').textContent = f1(st.Current_A && st.Current_A.avg, 3);
      document.getElementById('maxT').textContent = f1(st.Temperature_C && st.Temperature_C.max, 2);
      document.getElementById('preview').textContent = JSON.stringify(L, null, 2);

      // liveness: did elapsed advance recently?
      if (s.elapsed_s !== lastElapsed) { lastElapsed = s.elapsed_s; lastSeenAt = Date.now(); }
      const live = (Date.now() - lastSeenAt) < 8000 && s.row_count > 0;
      const dot = document.getElementById('liveTxt');
      dot.textContent = s.row_count > 0 ? (live ? 'LIVE' : 'idle (no new data)') : 'no data yet';
      dot.className = live ? 'ok' : 'stale';

      document.getElementById('mainPlot').src = '/plot/main.png?t=' + Date.now();
    } catch (e) {
      document.getElementById('liveTxt').textContent = 'error: ' + e;
    }
  }

  async function fetchAnalysis() {
    const btn = document.getElementById('analyzeBtn');
    btn.disabled = true; btn.textContent = 'Analyzing…';
    try {
      const r = await fetch('/api/analysis?t=' + Date.now());
      const a = await r.json();
      const grade = document.getElementById('aiGrade');
      if (!a.success) {
        grade.textContent = '!';
        document.getElementById('aiMeta').textContent = 'error: ' + (a.error || 'failed');
      } else {
        grade.textContent = a.grade;
        grade.style.color = ({A:'#34d399',B:'#a3e635',C:'#f59e0b',D:'#ef4444'})[a.grade] || '#e5e7eb';
        document.getElementById('aiMeta').textContent =
          (a.method === 'ml' ? 'ML model' : 'heuristic') +
          ' · confidence ' + Math.round((a.confidence || 0) * 100) + '%';
        const f = a.features || {};
        document.getElementById('aiSoH').textContent = f1(f.soh_pct, 1);
        document.getElementById('aiR0').textContent = f1(f.r0_mohm, 2);
        document.getElementById('aiRp').textContent = f1(f.rp_mohm, 2);
        document.getElementById('aiTau').textContent = f1(f.tau_s, 2);
        document.getElementById('aiPulses').textContent = f.num_pulses ?? '-';
        document.getElementById('aiNotes').textContent = (a.notes || []).join(' · ');
      }
    } catch (e) {
      document.getElementById('aiMeta').textContent = 'error: ' + e;
    } finally {
      btn.disabled = false; btn.textContent = 'Run AI analysis';
    }
  }

  document.getElementById('refreshBtn').addEventListener('click', refresh);
  document.getElementById('analyzeBtn').addEventListener('click', fetchAnalysis);
  refresh();
  fetchAnalysis();
  setInterval(refresh, 2000);
</script>
</body>
</html>
"""
