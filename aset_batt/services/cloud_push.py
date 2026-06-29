"""Cloud push service — periodically sends battery data to an HTTP endpoint.

Reads configuration from ConfigManager (config.system):
  cloud_push_enabled   : bool   — master switch
  cloud_dashboard_url  : str    — POST endpoint (e.g. https://host/api/ingest)
  cloud_push_interval  : int/float — seconds between pushes (default 60)

Auth token lookup order:
  1. Environment variable  INGEST_TOKEN
  2. File  cloud_token.txt  next to main.py (first non-empty line, stripped)
  If neither is found the request is sent without an Authorization header.
"""

import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

# Path anchor: cloud_token.txt lives next to main.py, which is at the project root.
_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
_TOKEN_FILE = os.path.join(_PROJECT_ROOT, "cloud_token.txt")


def _read_token() -> str:
    """Return the auth token or empty string if none is found."""
    # 1. Environment variable
    token = os.environ.get("INGEST_TOKEN", "").strip()
    if token:
        return token

    # 2. File next to main.py
    try:
        with open(_TOKEN_FILE, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    return line
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.debug(f"cloud_push: cannot read token file: {exc}")

    return ""


class CloudPushService:
    """Background worker that POSTs JSON payloads to a remote dashboard endpoint.

    Usage::

        svc = CloudPushService(config)
        svc.start()
        ...
        svc.push_now({"grade": "A", "soh": 94.2, ...})
        ...
        svc.stop()
    """

    def __init__(self, config):
        self._enabled: bool = False
        self._url: str = ""
        self._interval: float = 60.0
        self._payload: dict = {}
        self._payload_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        try:
            sys_cfg = config.system
            self._enabled = bool(getattr(sys_cfg, "cloud_push_enabled", False))
            self._url = str(getattr(sys_cfg, "cloud_dashboard_url", "") or "").strip()
            interval_raw = getattr(sys_cfg, "cloud_push_interval", 60)
            self._interval = float(interval_raw) if interval_raw else 60.0
        except AttributeError:
            logger.warning("cloud_push: config.system not available — service disabled")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background push thread (idempotent)."""
        if not self._enabled:
            logger.debug("cloud_push: disabled via config — not starting")
            return
        if not self._url:
            logger.debug("cloud_push: no endpoint URL configured — not starting")
            return
        if self._thread is not None and self._thread.is_alive():
            return  # already running

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._worker,
            name="CloudPushWorker",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"cloud_push: started — posting to {self._url} every {self._interval}s")

    def stop(self) -> None:
        """Signal the worker thread to stop."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(self._interval + 5, 10))
            self._thread = None
        logger.debug("cloud_push: stopped")

    def push_now(self, payload: dict) -> None:
        """Update the latest payload to be sent on the next push cycle.

        Thread-safe. The service does NOT interpret the payload; it sends
        whatever dict is provided verbatim as JSON.
        """
        with self._payload_lock:
            self._payload = dict(payload)

    # ------------------------------------------------------------------
    # Internal worker
    # ------------------------------------------------------------------

    def _worker(self) -> None:
        """Background loop: sleep → POST → repeat until stop_event is set."""
        import json
        import urllib.request
        import urllib.error

        while not self._stop_event.is_set():
            # Sleep in short increments so stop() responds promptly.
            interval_remaining = self._interval
            while interval_remaining > 0 and not self._stop_event.is_set():
                chunk = min(1.0, interval_remaining)
                time.sleep(chunk)
                interval_remaining -= chunk

            if self._stop_event.is_set():
                break

            with self._payload_lock:
                payload_snapshot = dict(self._payload)

            if not payload_snapshot:
                logger.debug("cloud_push: payload empty — skipping this cycle")
                continue

            try:
                token = _read_token()
                body = json.dumps(payload_snapshot).encode("utf-8")
                req = urllib.request.Request(
                    self._url,
                    data=body,
                    method="POST",
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "ASET-BatteryTester/1.0",
                    },
                )
                if token:
                    req.add_header("Authorization", f"Bearer {token}")

                with urllib.request.urlopen(req, timeout=15) as resp:
                    status = resp.status
                logger.debug(f"cloud_push: POST ok — HTTP {status}")

            except urllib.error.HTTPError as exc:
                logger.warning(f"cloud_push: HTTP {exc.code} from endpoint — {exc.reason}")
            except urllib.error.URLError as exc:
                logger.warning(f"cloud_push: network error — {exc.reason}")
            except OSError as exc:
                logger.warning(f"cloud_push: OS error during push — {exc}")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"cloud_push: unexpected error — {exc}")
