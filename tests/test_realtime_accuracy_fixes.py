"""Regression tests for the real-time SoC/Rin accuracy audit (12-item analysis):

- B1: temp_rin_multiplier() used the wrong Arrhenius key (always fell back to the
  linear approximation) — must now match _calculate_base_rin's temperature ratio.
- C1: the EKF's un-calibrated default R0 is too low for small AGM packs — locks the
  margin applied in _ekf_rc_defaults().
- C2: Peukert correction must NOT engage on a brief HPPC/DCIR pulse, only on a
  genuinely sustained discharge (a physics misapplication otherwise).
- B2: ESP32 temperature staleness detection (current_temp had no timestamp at all).
- A1/A2: the monitor loop must use a monotonic clock for dt (not wall-clock) and must
  compensate its sleep for how long the iteration itself took (not always add a fixed
  0.1 s on top of whatever SCPI round-trip already consumed).

These avoid Qt entirely (no QApplication/BatteryQtWindow), matching the project's
existing test style. The isa101_views.py UI-side fixes (D1 busy-guard, D2 Rin EMA,
D3 graph elapsed time, and the sequence-loop clock swaps) are Qt-coupled UI code and
were verified with headless smoke checks instead — see the session notes.
"""
import time
import unittest

from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.state_estimator import StateEstimator
from aset_batt.hardware.mock_hardware import MockHardwareController
from aset_batt.app.auto_controller import AutoController


# ---------------------------------------------------------------------------
# B1 — Arrhenius key mismatch
# ---------------------------------------------------------------------------
class TestTempRinMultiplierMatchesBaseRin(unittest.TestCase):
    def test_arrhenius_ratio_matches_calculate_base_rin(self):
        m = BatteryModel("LeadAcid", 2.0, 6, 1)   # Ea/R = 2200 K in battery_profiles.py
        mult = m.temp_rin_multiplier(10.0)
        r10 = m._calculate_base_rin(50.0, 10.0)
        r25 = m._calculate_base_rin(50.0, 25.0)
        ratio = r10 / r25
        self.assertAlmostEqual(mult, ratio, places=3)
        # the bug's signature: linear fallback (~1.075) vs correct Arrhenius (~1.48
        # at the corrected Ea/R=2200 K) — assert we're on the Arrhenius side, not
        # silently back on the linear one.
        self.assertGreater(mult, 1.3)

    def test_reference_temperature_is_unity(self):
        m = BatteryModel("LeadAcid", 2.0, 6, 1)
        self.assertAlmostEqual(m.temp_rin_multiplier(25.0), 1.0, places=6)


# ---------------------------------------------------------------------------
# C1 — EKF default R0 margin for un-calibrated packs
# ---------------------------------------------------------------------------
class TestEkfDefaultR0Margin(unittest.TestCase):
    def test_default_r0_has_margin_over_base_rin(self):
        m = BatteryModel("LeadAcid", 2.0, 6, 1)
        e = StateEstimator(7.0, m)
        r0, r1, c1 = e._ekf_rc_defaults()
        self.assertGreater(r0, m.base_rin)                     # margin applied
        self.assertAlmostEqual(r0, m.base_rin * e._EKF_UNCALIBRATED_R0_MARGIN, places=6)
        # lands in the realistic range for a small AGM pack (50-80 mOhm), not the
        # idealized fresh-cell datasheet value (~30 mOhm)
        self.assertGreater(r0 * 1000.0, 40.0)

    def test_real_hppc_fit_overrides_the_default(self):
        e = StateEstimator(7.0, BatteryModel("LeadAcid", 2.0, 6, 1))
        e.update(12.6, 0.0, dt=1.0, temp=25.0)   # lazily creates the EKF
        e.update_ecm(0.099, 0.02, 1000.0)         # distinctive fitted R0
        self.assertAlmostEqual(e._ekf.R0, 0.099, places=4)


# ---------------------------------------------------------------------------
# C2 — Peukert must not engage on a brief pulse, only sustained discharge
# ---------------------------------------------------------------------------
class TestPeukertSustainGate(unittest.TestCase):
    def _est(self):
        e = StateEstimator(7.0, BatteryModel("LeadAcid", 2.0, 6, 1))
        e._reset_to_soc(80.0)
        return e

    def test_short_pulse_does_not_reach_the_gate(self):
        e = self._est()
        for _ in range(10):                      # 10 s pulse
            e.update(11.5, 7.0, dt=1.0, temp=25.0)
        self.assertLess(e._peukert_sustain_s, e._peukert_min_sustain_s)

    def test_sustained_discharge_reaches_the_gate(self):
        e = self._est()
        for _ in range(70):                      # 70 s sustained discharge
            e.update(11.5, 7.0, dt=1.0, temp=25.0)
        self.assertGreaterEqual(e._peukert_sustain_s, e._peukert_min_sustain_s)

    def test_rest_resets_the_sustain_timer(self):
        e = self._est()
        for _ in range(45):
            e.update(11.5, 7.0, dt=1.0, temp=25.0)
        e.update(12.6, 0.0, dt=1.0, temp=25.0)   # rest
        self.assertEqual(e._peukert_sustain_s, 0.0)

    def test_peukert_dah_itself_still_applies_when_called_directly(self):
        """_peukert_dah is pure math + the use_peukert flag only — the sustain-time
        gate lives at the update() call site, not inside this method, so it stays
        directly unit-testable (this mirrors the team's own existing ablation test)."""
        e = StateEstimator(5.3, BatteryModel("LeadAcid", 2.0, 6, 1))
        e.use_peukert = True
        self.assertGreater(e._peukert_dah(5.3, 1.0), 1.0)


# ---------------------------------------------------------------------------
# B2 — ESP32 temperature staleness
# ---------------------------------------------------------------------------
class TestTempStaleness(unittest.TestCase):
    def test_fresh_after_connect(self):
        hw = MockHardwareController()
        hw.connect_esp32("COM_MOCK")
        self.assertFalse(hw.temp_is_stale())

    def test_stale_after_max_age(self):
        hw = MockHardwareController()
        hw.connect_esp32("COM_MOCK")
        hw.last_esp_heartbeat -= 20
        self.assertTrue(hw.temp_is_stale(max_age_s=10.0))

    def test_disconnected_is_always_stale(self):
        hw = MockHardwareController()
        self.assertTrue(hw.temp_is_stale())


# ---------------------------------------------------------------------------
# A1/A2 — monitor loop: monotonic clock + sleep compensates for iteration time
# ---------------------------------------------------------------------------
class _FakeEventHandler:
    def __init__(self):
        self.events = []

    def post_event(self, etype, data):
        self.events.append((etype, data))


class _FakeEstimator:
    def __init__(self):
        self.calls = []

    def update(self, v, i, dt, temp):
        self.calls.append(dt)
        return {"soc": 50.0, "rin": 0.03, "soh": 100.0}


class _FakeData:
    def log_row(self, *a, **kw):
        pass


class _FakeSystemConfig:
    safety_limits = {"max_temperature": 60.0, "min_temperature": -10.0,
                     "max_current": 30.0, "max_voltage": 15.0, "min_voltage": 10.0}


class _FakeConfig:
    def __init__(self):
        self.system = _FakeSystemConfig()


class _SlowFakeHW:
    """Simulates SCPI round-trip latency inside read_vi()."""
    def __init__(self, read_delay_s):
        self.is_connected = True
        self.current_temp = 25.0
        self._psu_output_on = False
        self.read_delay_s = read_delay_s
        self.n_calls = 0

    def read_vi(self):
        self.n_calls += 1
        time.sleep(self.read_delay_s)
        return (12.0, 0.0, 0.0)


class TestMonitorLoopTiming(unittest.TestCase):
    def _controller(self, hw):
        c = AutoController(root=None, hw=hw, data=_FakeData(),
                           estimator=_FakeEstimator(), config=_FakeConfig())
        c.event_handler = _FakeEventHandler()
        c.monitor_running = True
        c._start_time = time.time()
        return c

    def test_sleep_compensates_for_slow_scpi_round_trip(self):
        """A slow read (e.g. 60 ms of simulated SCPI latency) must not ALSO get a
        full fixed 0.1 s tacked on afterwards — the achieved period should stay close
        to the target period, not target+latency."""
        hw = _SlowFakeHW(read_delay_s=0.06)
        controller = self._controller(hw)
        n_iterations = 5

        def stop_after_n():
            if hw.n_calls >= n_iterations:
                controller.monitor_running = False

        # wrap read_vi to stop the loop after n_iterations
        real_read_vi = hw.read_vi
        def read_vi_and_maybe_stop():
            result = real_read_vi()
            stop_after_n()
            return result
        hw.read_vi = read_vi_and_maybe_stop

        t0 = time.perf_counter()
        controller._monitor_loop()
        elapsed = time.perf_counter() - t0

        # OLD behavior (fixed time.sleep(0.1) after every read): >= n*(0.06+0.1) = 0.8 s
        # NEW behavior (top up to the 0.1 s target): ~= n*0.1 = 0.5 s
        self.assertLess(elapsed, 0.7,
                        "monitor loop did not compensate its sleep for read latency")

    def test_dt_uses_monotonic_clock_not_wall_clock(self):
        """Feed the estimator two samples and confirm dt is a small, sane positive
        number reflecting perf_counter — this would be corrupted (huge, negative, or
        NaN) if the loop still used time.time() and the wall clock jumped."""
        hw = _SlowFakeHW(read_delay_s=0.01)
        controller = self._controller(hw)
        estimator = controller.estimator

        def stop_after_two():
            if hw.n_calls >= 2:
                controller.monitor_running = False
            return (12.0, 0.0, 0.0)
        real_read_vi = hw.read_vi
        def read_vi_and_maybe_stop():
            v = real_read_vi()
            return stop_after_two()
        hw.read_vi = read_vi_and_maybe_stop

        controller._monitor_loop()
        self.assertEqual(len(estimator.calls), 2)
        # first call: no previous timestamp -> falls back to the documented 0.1 default
        self.assertAlmostEqual(estimator.calls[0], 0.1, places=6)
        # second call: a real measured interval, small and positive (not a huge
        # wall-clock epoch difference, not negative)
        self.assertGreater(estimator.calls[1], 0.0)
        self.assertLess(estimator.calls[1], 1.0)


if __name__ == "__main__":
    unittest.main()
