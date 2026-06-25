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
            self.send_header("Cache-Control", "no-cache" if ext == ".html" else "public, max-age=300")
            self.end_headers()
            self.wfile.write(body)
            return True

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
    server = ThreadingHTTPServer(("0.0.0.0", PORT), _make_handler())
    print(f"ASET Cloud Dashboard listening on :{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
