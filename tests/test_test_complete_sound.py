"""~15s test-complete chime (ก.ค. 2026): plays once for every mode's finish —
Run Test, all 4 sequences (via the shared _slot_seq_done), all 4 CHARACTERIZE
tests (via the shared _slot_char_update "__DONE__" dispatch) — using
test_complete.wav, deliberately NOT pido.mp3 (the E-STOP siren), so a normal
successful finish never sounds identical to an emergency. Run Test and
CHARACTERIZE both reach their "done" handler unconditionally (even after an
E-STOP), so both are guarded to skip the chime in that case rather than
stack it on top of the siren; sequences route an E-STOP through
sig_seq_aborted instead of sig_seq_done, so no equivalent guard is needed
there.
"""
import os
import threading
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from aset_batt.ui import theme
theme.set_theme("light")

from PySide6.QtWidgets import QApplication
from aset_batt.core.config import ConfigManager
from aset_batt.ui.isa101_views import BatteryQtWindow

_app = QApplication.instance() or QApplication([])


def _make_window():
    w = BatteryQtWindow(ConfigManager())
    w.estimator = MagicMock(soh=100.0, soc_std=None)
    return w


def test_asset_file_exists_and_is_about_15_seconds():
    import wave
    path = os.path.join(os.path.dirname(__file__), "..", "aset_batt", "ui", "test_complete.wav")
    assert os.path.exists(path)
    with wave.open(path, "rb") as f:
        duration = f.getnframes() / f.getframerate()
    assert 14.5 <= duration <= 15.5


def _real_test_results(soh=82.0):
    """A REAL analyze_series() result (full key set) — avoids hand-rolling a
    dict that must track every key _on_test_finished happens to read (see
    test_aging_factor_wiring.py's own version of this helper)."""
    import numpy as np
    from aset_batt.acquisition.analysis import analyze_series
    from aset_batt.acquisition.models import BatteryProfile
    n = 20
    t = np.arange(n, dtype=float) * 0.2
    i = np.full(n, 1.0)
    v = np.linspace(12.6, 11.5, n)
    temp = np.full(n, 25.0)
    cap = np.cumsum(i) * 0.2 / 3600.0
    profile = BatteryProfile(
        name="t", chemistry="LeadAcid", nominal_v=12.0, series=6, capacity_ah=5.3,
        max_charge_v=14.4, cutoff_v=10.5, max_charge_a=1.0, max_discharge_a=10.0,
        ovp=15.0, uvp=9.5, otp_warn=45.0, otp_crit=60.0, internal_r=0.03,
    )
    res = analyze_series(t, i, v, temp, cap, profile, is_hppc=False)
    res["soh"] = soh
    return res


def test_play_test_complete_sound_uses_the_distinct_wav_not_the_siren():
    w = _make_window()
    try:
        with patch("PySide6.QtMultimedia.QMediaPlayer") as mock_cls:
            mock_player = mock_cls.return_value
            w._play_test_complete_sound()
            mock_player.setSource.assert_called_once()
            url_arg = mock_player.setSource.call_args[0][0]
            assert "test_complete.wav" in url_arg.toLocalFile()
            assert "pido" not in url_arg.toLocalFile()
            mock_player.play.assert_called_once()
    finally:
        w.close()


def test_run_test_finish_plays_sound_when_not_estopped():
    w = _make_window()
    try:
        w._test_worker = MagicMock(_estop=False)
        w.buf_t = []
        with patch.object(w, "_play_test_complete_sound") as mock_sound:
            w._on_test_finished(_real_test_results())
        mock_sound.assert_called_once()
    finally:
        w.close()


def test_run_test_finish_skips_sound_after_estop():
    w = _make_window()
    try:
        w._test_worker = MagicMock(_estop=True)
        w.buf_t = []
        with patch.object(w, "_play_test_complete_sound") as mock_sound:
            w._on_test_finished(_real_test_results())
        mock_sound.assert_not_called()
    finally:
        w.close()


def test_seq_done_plays_sound():
    from aset_batt.ui.sequences.base import BaseSequenceMixin

    class Host(BaseSequenceMixin):
        def __init__(self):
            self.lbl_phase_banner = MagicMock()
            self._current_test_name = "IEC 61960"
            self.sig_profile_status = MagicMock()
            self._play_test_complete_sound = MagicMock()
            self._headless = True

    host = Host()
    host._slot_seq_done("IEC 61960 Sequence Complete", "Grade: A")
    host._play_test_complete_sound.assert_called_once()


def test_char_done_plays_sound_when_not_safety_triggered():
    w = _make_window()
    try:
        w.hw = MagicMock()
        w.controller = MagicMock(safety_triggered=False)
        w._char_results = {}
        with patch.object(w, "_play_test_complete_sound") as mock_sound:
            w._slot_char_update("pk", "__DONE__")
        mock_sound.assert_called_once()
    finally:
        w.close()


def test_char_done_skips_sound_when_safety_triggered():
    w = _make_window()
    try:
        w.hw = MagicMock()
        w.controller = MagicMock(safety_triggered=True)
        w._char_results = {}
        with patch.object(w, "_play_test_complete_sound") as mock_sound:
            w._slot_char_update("pk", "__DONE__")
        mock_sound.assert_not_called()
    finally:
        w.close()
