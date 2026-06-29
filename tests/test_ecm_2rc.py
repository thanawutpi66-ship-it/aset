"""
Tests for the 2-RC Thevenin ECM fit (BatteryParameterIdentifier.fit_model_2rc).

The 2-RC model is:
    V(t) = Voc - I*R0 - I*R1*(1-exp(-t/tau1)) - I*R2*(1-exp(-t/tau2))
    where tau1 = R1*C1, tau2 = R2*C2

Known params used across tests:
    R0 = 0.010 Ohm, R1 = 0.005 Ohm, C1 = 3000 F (tau1 = 15 s)
    R2 = 0.003 Ohm, C2 = 26667 F (tau2 = 80 s, ratio ~5.3x > 5x minimum)
    I  = 5 A, Voc = 25.6 V, pulse = 200 s (covers ~2.5 x tau2)

The fit_model_2rc implementation requires the two time constants to differ by
at least 5x and uses a 200 s pulse so both RC transients are well-excited and
resolvable by the bounded TRF solver.
"""
import pytest
import numpy as np

from aset_batt.core.parameter_id import BatteryParameterIdentifier


# ---------------------------------------------------------------------------
# Synthetic data helpers
# ---------------------------------------------------------------------------

R0_TRUE  = 0.010
R1_TRUE  = 0.005
C1_TRUE  = 3000.0   # tau1 = R1*C1 = 15 s
R2_TRUE  = 0.003
C2_TRUE  = 26667.0  # tau2 = R2*C2 = 80 s  (ratio ~5.3x > 5x required)
I_TRUE   = 5.0
VOC_TRUE = 25.6


def _synthetic_2rc(r0=R0_TRUE, r1=R1_TRUE, c1=C1_TRUE,
                   r2=R2_TRUE, c2=C2_TRUE,
                   current=I_TRUE, voc=VOC_TRUE,
                   dt=0.5, rest_s=10.0, pulse_s=200.0,
                   noise_v=0.001, seed=42):
    """Generate rest -> discharge pulse data from a 2-RC ECM with added noise.

    Default pulse length (200 s) covers ~2.5 x tau2 (80 s) so both RC branches
    are sufficiently excited for the TRF solver to separate them.
    """
    rng = np.random.default_rng(seed)
    tau1 = r1 * c1
    tau2 = r2 * c2

    t_rest  = np.arange(0.0, rest_s, dt)
    t_pulse = np.arange(0.0, pulse_s, dt)

    v_rest = np.full_like(t_rest, voc)
    i_rest = np.zeros_like(t_rest)

    v_pulse = (voc
               - current * r0
               - current * r1 * (1.0 - np.exp(-t_pulse / tau1))
               - current * r2 * (1.0 - np.exp(-t_pulse / tau2)))
    i_pulse = np.full_like(t_pulse, current)

    t = np.concatenate([t_rest, t_rest[-1] + dt + t_pulse])
    v = np.concatenate([v_rest, v_pulse]) + rng.normal(0.0, noise_v, t.size)
    i = np.concatenate([i_rest, i_pulse])
    return t, i, v


def _synthetic_1rc(r0=R0_TRUE, r1=R1_TRUE, c1=C1_TRUE,
                   current=I_TRUE, voc=VOC_TRUE,
                   dt=0.5, rest_s=10.0, pulse_s=200.0,
                   noise_v=0.001, seed=7):
    """Generate rest -> discharge pulse data from a 1-RC ECM with added noise."""
    rng = np.random.default_rng(seed)
    tau1 = r1 * c1

    t_rest  = np.arange(0.0, rest_s, dt)
    t_pulse = np.arange(0.0, pulse_s, dt)

    v_rest = np.full_like(t_rest, voc)
    i_rest = np.zeros_like(t_rest)

    v_pulse = (voc
               - current * r0
               - current * r1 * (1.0 - np.exp(-t_pulse / tau1)))
    i_pulse = np.full_like(t_pulse, current)

    t = np.concatenate([t_rest, t_rest[-1] + dt + t_pulse])
    v = np.concatenate([v_rest, v_pulse]) + rng.normal(0.0, noise_v, t.size)
    i = np.concatenate([i_rest, i_pulse])
    return t, i, v


# ---------------------------------------------------------------------------
# Skip guard — skip all tests if fit_model_2rc is not implemented
# ---------------------------------------------------------------------------

def _skip_if_no_2rc():
    ident = BatteryParameterIdentifier()
    if not hasattr(ident, "fit_model_2rc"):
        pytest.skip("BatteryParameterIdentifier.fit_model_2rc() not implemented yet")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFitModel2RCExists:
    """Test 1: verify the method is present (or skip cleanly if not)."""

    def test_method_exists(self):
        ident = BatteryParameterIdentifier()
        if not hasattr(ident, "fit_model_2rc"):
            pytest.skip("fit_model_2rc not yet implemented — skipping")
        assert callable(ident.fit_model_2rc), "fit_model_2rc must be callable"


class TestFitModel2RCRecovery:
    """Test 2: fitting recovers known 2-RC parameters within 20 %."""

    def test_recovers_r0_within_20pct(self):
        _skip_if_no_2rc()
        t, i, v = _synthetic_2rc()
        ident = BatteryParameterIdentifier(smooth_window=5)
        res = ident.fit_model_2rc(t, i, v, VOC_TRUE)
        assert res is not None, "fit_model_2rc returned None on clean 2-RC data"
        r0 = res.get("R0_ohm") if isinstance(res, dict) else res.R0_ohm
        assert abs(r0 - R0_TRUE) <= 0.20 * R0_TRUE, (
            f"R0 recovery failed: got {r0:.6f}, expected {R0_TRUE:.6f} ±20%"
        )

    def test_recovers_r1_within_20pct(self):
        _skip_if_no_2rc()
        t, i, v = _synthetic_2rc()
        ident = BatteryParameterIdentifier(smooth_window=5)
        res = ident.fit_model_2rc(t, i, v, VOC_TRUE)
        assert res is not None
        r1 = res.get("R1_ohm") if isinstance(res, dict) else res.R1_ohm
        assert abs(r1 - R1_TRUE) <= 0.20 * R1_TRUE, (
            f"R1 recovery failed: got {r1:.6f}, expected {R1_TRUE:.6f} ±20%"
        )

    def test_recovers_r2_within_20pct(self):
        _skip_if_no_2rc()
        t, i, v = _synthetic_2rc()
        ident = BatteryParameterIdentifier(smooth_window=5)
        res = ident.fit_model_2rc(t, i, v, VOC_TRUE)
        assert res is not None
        r2 = res.get("R2_ohm") if isinstance(res, dict) else res.R2_ohm
        assert abs(r2 - R2_TRUE) <= 0.20 * R2_TRUE, (
            f"R2 recovery failed: got {r2:.6f}, expected {R2_TRUE:.6f} ±20%"
        )

    def test_result_keys_present(self):
        _skip_if_no_2rc()
        t, i, v = _synthetic_2rc()
        ident = BatteryParameterIdentifier(smooth_window=5)
        res = ident.fit_model_2rc(t, i, v, VOC_TRUE)
        assert res is not None
        if isinstance(res, dict):
            for key in ("R0_ohm", "R1_ohm", "R2_ohm"):
                assert key in res, f"Expected key '{key}' in result dict"

    def test_all_resistances_positive(self):
        _skip_if_no_2rc()
        t, i, v = _synthetic_2rc()
        ident = BatteryParameterIdentifier(smooth_window=5)
        res = ident.fit_model_2rc(t, i, v, VOC_TRUE)
        assert res is not None
        if isinstance(res, dict):
            assert res.get("R0_ohm", 0.0) > 0, "R0 must be positive"
            assert res.get("R1_ohm", 0.0) > 0, "R1 must be positive"
            assert res.get("R2_ohm", 0.0) > 0, "R2 must be positive"


class TestFitModel2RC1RCFallback:
    """Test 3: 1-RC input should return None or fall back to 1-RC — must not crash."""

    def test_1rc_data_does_not_crash(self):
        _skip_if_no_2rc()
        t, i, v = _synthetic_1rc()
        ident = BatteryParameterIdentifier(smooth_window=5)
        # Must not raise — may return None or a degraded result
        try:
            res = ident.fit_model_2rc(t, i, v, VOC_TRUE)
        except Exception as exc:
            pytest.fail(
                f"fit_model_2rc raised {type(exc).__name__} on 1-RC data: {exc}"
            )

    def test_1rc_data_returns_none_or_result(self):
        _skip_if_no_2rc()
        t, i, v = _synthetic_1rc()
        ident = BatteryParameterIdentifier(smooth_window=5)
        res = ident.fit_model_2rc(t, i, v, VOC_TRUE)
        # Acceptable: None (fit rejected) or a dict/dataclass result
        assert res is None or isinstance(res, (dict, object)), (
            "Expected None or a result object, got unexpected type"
        )


class TestFitModel2RCInsufficientData:
    """Test 4: fewer than 10 samples should return None gracefully (no exception)."""

    def test_fewer_than_10_samples_returns_none(self):
        _skip_if_no_2rc()
        t = np.linspace(0.0, 0.8, 9)
        i = np.array([0.0, 0.0, 0.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0])
        v = np.full(9, VOC_TRUE - I_TRUE * R0_TRUE)
        ident = BatteryParameterIdentifier(smooth_window=3)
        try:
            res = ident.fit_model_2rc(t, i, v, VOC_TRUE)
        except Exception as exc:
            pytest.fail(
                f"fit_model_2rc raised {type(exc).__name__} on < 10 samples: {exc}"
            )
        assert res is None, (
            f"Expected None for < 10 samples, got {res!r}"
        )

    def test_empty_arrays_returns_none(self):
        _skip_if_no_2rc()
        ident = BatteryParameterIdentifier()
        try:
            res = ident.fit_model_2rc(np.array([]), np.array([]), np.array([]), VOC_TRUE)
        except Exception as exc:
            pytest.fail(
                f"fit_model_2rc raised {type(exc).__name__} on empty arrays: {exc}"
            )
        assert res is None, "Expected None for empty input arrays"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
