"""
ASET Cloud Dashboard — บริการแสดงผลเทสต์แบตเตอรี่ 24 ชม. (stdlib ล้วน)

แยกจากแอปแล็บ: เครื่องแล็บ push ข้อมูลขึ้นมาที่ POST /api/ingest (auth ด้วย token)
Frontend อยู่ที่ static/ (index.html + style.css + app.js)

ENV:
  PORT          พอร์ต (Heroku ตั้งให้อัตโนมัติ; local default 8001)
  INGEST_TOKEN  token สำหรับ /api/ingest (ต้องตั้ง ไม่งั้น ingest ถูกปฏิเสธ)
  SNAPSHOT_PATH ไฟล์เก็บ snapshot ล่าสุด (default ./snapshot.json, best-effort)
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# Windows consoles default to a legacy code page (e.g. cp1252) that cannot encode the
# Thai status messages below, which would crash the server on startup. Force UTF-8 on
# the standard streams so logging works regardless of the host console encoding.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

PORT = int(os.environ.get("PORT", "8001"))
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")
SNAPSHOT_PATH = os.environ.get("SNAPSHOT_PATH", "snapshot.json")
SESSIONS_PATH = os.environ.get("SESSIONS_PATH", "sessions.json")
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "20"))

# Static frontend lives in ./static (index.html + style.css + app.js). It is served
# for any non-/api path; the whole UI is editable there without touching this file.
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".webmanifest": "application/manifest+json",
}

# ---------------------------------------------------------------------------
# In-memory store (+ best-effort disk snapshot เผื่อ process restart)
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_store = {"payload": None, "received_at": 0.0}
_sessions: list = []   # [{idx, received_at, battery, row_count, size_bytes, payload}]
_analyze_queue: dict = {}  # {session_idx: queued_at} — pending re-analysis requests


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


def _sessions_meta(sessions: list) -> list:
    """Return session list without full payload (for disk / list API)."""
    return [{"idx": s["idx"], "received_at": s["received_at"],
             "battery": s["battery"], "row_count": s["row_count"],
             "size_bytes": s["size_bytes"]} for s in sessions]


def _load_sessions() -> None:
    try:
        if os.path.exists(SESSIONS_PATH):
            with open(SESSIONS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            for m in data.get("sessions", []):
                _sessions.append({**m, "payload": None})  # payloads lost on restart
    except Exception:
        pass


def _save_sessions() -> None:
    try:
        with open(SESSIONS_PATH, "w", encoding="utf-8") as f:
            json.dump({"sessions": _sessions_meta(_sessions[-MAX_SESSIONS:])}, f)
    except Exception:
        pass


def _json_sanitize(obj):
    """Recursively replace float NaN/Infinity with None.
    json.dumps emits the literal tokens NaN/Infinity/-Infinity for these by default
    (valid Python, NOT valid JSON) — browsers' JSON.parse() rejects them outright,
    which silently breaks every fetch().json() call on the frontend the moment any
    analysis field (e.g. soh on a partial discharge) is NaN."""
    if isinstance(obj, float):
        return None if (obj != obj or obj in (float("inf"), float("-inf"))) else obj
    if isinstance(obj, dict):
        return {k: _json_sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_sanitize(v) for v in obj]
    return obj


def _make_handler():
    class Handler(BaseHTTPRequestHandler):
        def _json(self, payload, status=200):
            body = json.dumps(_json_sanitize(payload), ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_static(self, path) -> bool:
            """Serve a file from STATIC_DIR. Returns False if it does not exist
            (so the caller can fall back to a 404). Blocks path traversal."""
            rel = path.lstrip("/") or "index.html"
            full = os.path.normpath(os.path.join(STATIC_DIR, rel))
            if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
                return False
            ext = os.path.splitext(full)[1].lower()
            ctype = _CONTENT_TYPES.get(ext, "application/octet-stream")
            with open(full, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # HTML must stay fresh; static assets can be cached briefly.
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
            return True

        # ---- ingest + re-analysis endpoints (จากเครื่องแล็บ / web) ----------
        def do_POST(self):  # noqa: N802
            import re as _re
            path = urlparse(self.path).path

            # POST /api/analyze-request/:id — web browser requests re-analysis (no auth)
            _ar = _re.match(r"^/api/analyze-request/(\d+)$", path)
            if _ar:
                sidx = int(_ar.group(1))
                with _lock:
                    exists = any(s["idx"] == sidx for s in _sessions)
                if not exists:
                    self._json({"error": "session not found"}, 404)
                    return
                now = time.time()
                with _lock:
                    _analyze_queue[sidx] = now
                self._json({"ok": True, "queued": sidx, "queued_at": now})
                return

            # POST /api/update-analysis/:id — lab pushes fresh analysis back (auth required)
            _ua = _re.match(r"^/api/update-analysis/(\d+)$", path)
            if _ua:
                if not INGEST_TOKEN:
                    self._json({"error": "server INGEST_TOKEN not configured"}, 503)
                    return
                token = self.headers.get("X-Ingest-Token", "")
                if token != INGEST_TOKEN:
                    self._json({"error": "unauthorized"}, 401)
                    return
                sidx = int(_ua.group(1))
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    raw = self.rfile.read(length) if length else b"{}"
                    body = json.loads(raw.decode("utf-8"))
                except Exception as e:
                    self._json({"error": f"bad payload: {e}"}, 400)
                    return
                analysis = body.get("analysis", {})
                analysis["_analyzed_at"] = time.time()
                with _lock:
                    match = next((s for s in _sessions if s["idx"] == sidx), None)
                    if match and match.get("payload"):
                        match["payload"]["analysis"] = analysis
                        if _store["payload"] and _sessions and _sessions[-1]["idx"] == sidx:
                            _store["payload"]["analysis"] = analysis
                    _analyze_queue.pop(sidx, None)
                self._json({"ok": True, "updated": sidx})
                return

            # POST /api/ingest — full push from lab
            if path != "/api/ingest":
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
                now = time.time()
                _store["payload"] = payload
                _store["received_at"] = now
                _save_snapshot()
                entry = {
                    "idx": len(_sessions) + 1,
                    "received_at": now,
                    "battery": (payload.get("meta") or {}).get("battery", "–"),
                    "row_count": int((payload.get("summary") or {}).get("row_count") or 0),
                    "size_bytes": len(raw),
                    "payload": payload,
                }
                _sessions.append(entry)
                if len(_sessions) > MAX_SESSIONS:
                    _sessions.pop(0)
                # Re-index after trim
                for i, s in enumerate(_sessions):
                    s["idx"] = i + 1
                _save_sessions()
            self._json({"ok": True, "received_at": _store["received_at"]})

        # ---- serve (ให้ผู้ชม) --------------------------------------------
        def do_GET(self):  # noqa: N802
            path = urlparse(self.path).path
            try:
                if path == "/api/health":
                    self._json({"ok": True, "time": time.time(),
                                "has_data": _store["payload"] is not None})
                    return

                # Non-API requests are served from the static frontend folder.
                if not path.startswith("/api/"):
                    if self._serve_static(path):
                        return
                    self._json({"error": "not found"}, 404)
                    return

                with _lock:
                    payload = _store["payload"]
                    received_at = _store["received_at"]

                if path == "/api/snapshot":
                    self._json({"payload": payload, "received_at": received_at})
                    return

                if path == "/api/sessions":
                    with _lock:
                        self._json({"sessions": _sessions_meta(_sessions)})
                    return

                # GET /api/pending-analyses — lab polls for re-analysis requests (auth required)
                if path == "/api/pending-analyses":
                    token = self.headers.get("X-Ingest-Token", "")
                    if not INGEST_TOKEN or token != INGEST_TOKEN:
                        self._json({"error": "unauthorized"}, 401)
                        return
                    with _lock:
                        pending = []
                        for sidx, queued_at in list(_analyze_queue.items()):
                            match = next((s for s in _sessions if s["idx"] == sidx), None)
                            if match:
                                csv_path = ((match.get("payload") or {}).get("summary") or {}).get("csv_path", "")
                                pending.append({"idx": sidx, "csv_path": csv_path, "queued_at": queued_at})
                    self._json({"pending": pending})
                    return

                import re as _re
                _sm = _re.match(r"^/api/session/(\d+)$", path)
                if _sm:
                    sidx = int(_sm.group(1))
                    with _lock:
                        match = next((s for s in _sessions if s["idx"] == sidx), None)
                    if match and match.get("payload"):
                        self._json({"payload": match["payload"],
                                    "received_at": match["received_at"]})
                    elif match:
                        # metadata only (payload lost on restart) — fall back to latest
                        self._json({"payload": _store["payload"],
                                    "received_at": _store["received_at"],
                                    "fallback": True})
                    else:
                        self._json({"error": "session not found"}, 404)
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




def main():
    if not INGEST_TOKEN:
        print("WARNING: INGEST_TOKEN ไม่ได้ตั้ง — /api/ingest จะถูกปฏิเสธทั้งหมด")
    _load_snapshot()
    _load_sessions()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), _make_handler())
    print(f"ASET Cloud Dashboard listening on :{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
