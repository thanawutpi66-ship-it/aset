import pytest
import os
from unittest.mock import patch
from aset_batt.storage.data_utils import DataHandler
import datetime

@pytest.fixture(autouse=True)
def isolate_sessions_dir(tmp_path):
    """Automatically redirects DataHandler's session directory to a temporary path for all tests."""
    with patch.object(DataHandler, "make_session_path") as mock_make:
        def fake_make(sessions_dir="sessions", label=""):
            d = tmp_path / "sessions"
            d.mkdir(exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            prefix = f"{label}_" if label else ""
            return str(d / f"test_{prefix}{ts}.csv")
        mock_make.side_effect = fake_make
        yield


@pytest.fixture(autouse=True)
def _reset_theme_registry():
    """theme.style()/on_retheme() register widget stylesheets/callbacks in
    module-level lists that live for the whole pytest process, not per-test.
    Every GUI test that builds a BatteryQtWindow/TrendContainer adds ~200
    style_registry entries and a couple of retheme_hooks that never get
    cleared (the production app only ever builds one long-lived window, so
    this never mattered there) — across hundreds of tests those accumulate
    into tens of thousands of entries, and a later test's theme.retheme()
    call ends up re-styling/repainting every leaked widget from every prior
    test, turning a ~50ms call into minutes (looked like a hang running the
    full suite). Reset both lists after each test so they only ever hold
    that one test's own widgets."""
    yield
    from aset_batt.ui import theme
    theme._style_registry.clear()
    theme._retheme_hooks.clear()
