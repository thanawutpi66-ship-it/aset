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
