"""Tests for cloud_dashboard/server.py — the first test coverage this module
has ever had. Also exercises the Phase A hardening added alongside these
tests: XSS-adjacent payload validation (_validate_payload), constant-time
token comparison, POST body-size cap, and the path-traversal guard fix.

Uses a real ThreadingHTTPServer bound to a random free port (stdlib, no
mocking needed) rather than the PySide6/pyvisa mocking patterns used
elsewhere in this repo (see tests/test_hardware_connect_flow.py) — the
server itself is cheap to run for real.

Run with: python -m pytest cloud_dashboard/tests/ -q
(or: python -m unittest discover cloud_dashboard/tests)

IMPORTANT: never use the real cloud_token.txt / lab INGEST_TOKEN here — these
tests set their own disposable TEST_TOKEN and never read that file.
"""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as srv  # cloud_dashboard/server.py

TEST_TOKEN = "test-token-for-unit-tests-only-not-a-real-secret"


class _ServerTestCase(unittest.TestCase):
    """Spins up a real server on a random free port; resets server.py's
    module-level globals (_store/_sessions/_analyze_queue/INGEST_TOKEN) and
    redirects snapshot/session persistence to a throwaway temp dir so tests
    never interfere with each other or touch the real dev snapshot.json."""

    def setUp(self):
        self._orig_token = srv.INGEST_TOKEN
        self._orig_snapshot_path = srv.SNAPSHOT_PATH
        self._orig_sessions_path = srv.SESSIONS_PATH
        self._orig_max_body = srv.MAX_BODY_BYTES

        srv.INGEST_TOKEN = TEST_TOKEN
        srv._store["payload"] = None
        srv._store["received_at"] = 0.0
        srv._sessions.clear()
        srv._analyze_queue.clear()

        self._tmpdir = tempfile.mkdtemp(prefix="aset_cloud_dashboard_test_")
        srv.SNAPSHOT_PATH = os.path.join(self._tmpdir, "snapshot.json")
        srv.SESSIONS_PATH = os.path.join(self._tmpdir, "sessions.json")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), srv._make_handler())
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        srv.INGEST_TOKEN = self._orig_token
        srv.SNAPSHOT_PATH = self._orig_snapshot_path
        srv.SESSIONS_PATH = self._orig_sessions_path
        srv.MAX_BODY_BYTES = self._orig_max_body

    def _request(self, method, path, body=None, token=None, raw_body=None):
        """Returns (status, parsed_json_or_None, raw_bytes)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if token is not None:
            headers["X-Ingest-Token"] = token
        data = raw_body
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        finally:
            conn.close()
        try:
            parsed = json.loads(raw) if raw else None
        except Exception:
            parsed = None
        return resp.status, parsed, raw

    def _ingest(self, payload, token=TEST_TOKEN):
        return self._request("POST", "/api/ingest", body=payload, token=token)


class TestIngestAuth(_ServerTestCase):
    def test_missing_token_rejected(self):
        status, body, _ = self._ingest({"meta": {"battery": "x"}}, token=None)
        self.assertEqual(status, 401)

    def test_wrong_token_rejected(self):
        status, body, _ = self._ingest({"meta": {"battery": "x"}}, token="wrong-token")
        self.assertEqual(status, 401)

    def test_correct_token_accepted(self):
        status, body, _ = self._ingest({"meta": {"battery": "x"}})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_unset_ingest_token_rejects_everything(self):
        srv.INGEST_TOKEN = ""
        status, body, _ = self._ingest({"meta": {"battery": "x"}})
        self.assertEqual(status, 503)

    def test_pending_analyses_requires_token(self):
        status, _, _ = self._request("GET", "/api/pending-analyses", token=None)
        self.assertEqual(status, 401)
        status, _, _ = self._request("GET", "/api/pending-analyses", token="wrong")
        self.assertEqual(status, 401)
        status, body, _ = self._request("GET", "/api/pending-analyses", token=TEST_TOKEN)
        self.assertEqual(status, 200)


class TestHealthSnapshotSessions(_ServerTestCase):
    def test_health_before_and_after_ingest(self):
        status, body, _ = self._request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertFalse(body["has_data"])

        self._ingest({"meta": {"battery": "YTZ6V"}})

        status, body, _ = self._request("GET", "/api/health")
        self.assertTrue(body["has_data"])

    def test_snapshot_round_trip(self):
        self._ingest({"meta": {"battery": "YTZ6V"}, "summary": {"row_count": 5}})
        status, body, _ = self._request("GET", "/api/snapshot")
        self.assertEqual(status, 200)
        self.assertEqual(body["payload"]["meta"]["battery"], "YTZ6V")

    def test_sessions_list_after_ingest(self):
        self._ingest({"meta": {"battery": "A"}})
        self._ingest({"meta": {"battery": "B"}})
        status, body, _ = self._request("GET", "/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["sessions"]), 2)
        self.assertEqual(body["sessions"][-1]["battery"], "B")

    def test_session_detail_by_id(self):
        self._ingest({"meta": {"battery": "A"}})
        status, body, _ = self._request("GET", "/api/session/1")
        self.assertEqual(status, 200)
        self.assertEqual(body["payload"]["meta"]["battery"], "A")

    def test_session_detail_not_found(self):
        status, body, _ = self._request("GET", "/api/session/999")
        self.assertEqual(status, 404)


class TestPathTraversal(_ServerTestCase):
    def test_traversal_attempt_rejected(self):
        # os.path.normpath collapses "..", so this targets a file genuinely
        # outside STATIC_DIR (the server's own source) rather than 404ing for
        # the trivial reason that the literal path doesn't exist.
        status, _, _ = self._request("GET", "/../server.py")
        self.assertEqual(status, 404)

    def test_normal_static_file_still_served(self):
        status, _, raw = self._request("GET", "/index.html")
        self.assertEqual(status, 200)
        self.assertIn(b"<", raw)


class TestAnalyzeRequestAndPendingAnalyses(_ServerTestCase):
    def test_analyze_request_unknown_session_404(self):
        status, body, _ = self._request("POST", "/api/analyze-request/999")
        self.assertEqual(status, 404)

    def test_analyze_request_no_auth_needed(self):
        self._ingest({"meta": {"battery": "A"}})
        status, body, _ = self._request("POST", "/api/analyze-request/1")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])

    def test_queued_request_appears_in_pending_analyses(self):
        self._ingest({"meta": {"battery": "A"}})
        self._request("POST", "/api/analyze-request/1")
        status, body, _ = self._request("GET", "/api/pending-analyses", token=TEST_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["pending"]), 1)
        self.assertEqual(body["pending"][0]["idx"], 1)

    def test_update_analysis_requires_auth(self):
        self._ingest({"meta": {"battery": "A"}})
        status, _, _ = self._request(
            "POST", "/api/update-analysis/1", body={"analysis": {"soh": 90.0}}, token=None)
        self.assertEqual(status, 401)

    def test_update_analysis_writes_back(self):
        self._ingest({"meta": {"battery": "A"}})
        status, body, _ = self._request(
            "POST", "/api/update-analysis/1", body={"analysis": {"soh": 90.0}}, token=TEST_TOKEN)
        self.assertEqual(status, 200)
        _, snap, _ = self._request("GET", "/api/session/1")
        self.assertEqual(snap["payload"]["analysis"]["soh"], 90.0)


class TestNanSanitizer(_ServerTestCase):
    def test_nan_in_stored_payload_becomes_null_on_serve(self):
        # json.loads() (Python's, non-strict-JSON default) accepts the bare
        # NaN token, matching what a numpy-derived float("nan") would produce
        # on the lab side — see _json_sanitize's own docstring for why this
        # matters (browsers' JSON.parse() rejects literal NaN outright).
        raw = b'{"meta": {"battery": "A"}, "analysis": {"soh": NaN}}'
        status, body, _ = self._request(
            "POST", "/api/ingest", raw_body=raw, token=TEST_TOKEN)
        self.assertEqual(status, 200)

        status, snap, raw_resp = self._request("GET", "/api/snapshot")
        self.assertEqual(status, 200)
        self.assertIsNone(snap["payload"]["analysis"]["soh"])
        self.assertNotIn(b"NaN", raw_resp)


class TestBodySizeCap(_ServerTestCase):
    def test_oversized_body_rejected_with_413(self):
        srv.MAX_BODY_BYTES = 10
        status, body, _ = self._ingest({"meta": {"battery": "this payload is over 10 bytes"}})
        self.assertEqual(status, 413)

    def test_body_within_cap_still_accepted(self):
        srv.MAX_BODY_BYTES = 10_000
        status, body, _ = self._ingest({"meta": {"battery": "fits fine"}})
        self.assertEqual(status, 200)


class TestPayloadValidation(_ServerTestCase):
    def test_non_dict_payload_rejected(self):
        status, body, _ = self._request(
            "POST", "/api/ingest", raw_body=b'"just a string"', token=TEST_TOKEN)
        self.assertEqual(status, 400)

    def test_oversized_battery_name_truncated_not_rejected(self):
        status, body, _ = self._ingest({"meta": {"battery": "A" * 500}})
        self.assertEqual(status, 200)
        _, snap, _ = self._request("GET", "/api/snapshot")
        self.assertLessEqual(len(snap["payload"]["meta"]["battery"]), srv.MAX_BATTERY_NAME_LEN)

    def test_oversized_alarms_list_truncated(self):
        alarms = [{"ts": i, "severity": "ALARM", "message": "x"} for i in range(500)]
        status, body, _ = self._ingest({"meta": {"battery": "A"}, "alarms": alarms})
        self.assertEqual(status, 200)
        _, snap, _ = self._request("GET", "/api/snapshot")
        self.assertLessEqual(len(snap["payload"]["alarms"]), srv.MAX_ALARMS)


if __name__ == "__main__":
    unittest.main()
