"""
Advanced State Estimation: SoC estimation ด้วย Coulomb counting + OCV correction
+ 2-state EKF (1-RC) fusion, live SoH, SoH-adjusted capacity, current-offset tare.
"""
import time
import threading
from functools import wraps
from aset_batt.core.battery_model import (
    BatteryModel, is_plausible_r0, MAX_STEP_EDGE_LATENCY_S, STEADY_STATE_MAX_SPREAD_V,
)
from aset_batt.core.soc_ekf import SoCEKF
import logging

logger = logging.getLogger(__name__)

class StateEstimator:
    """Adaptive & robust SoC estimator"""

    def __init__(self, rated_capacity: float, battery_model: BatteryModel = None):
        self._lock = threading.RLock()
        self.rated_capacity = rated_capacity  # Ah
        self.battery_model = battery_model or BatteryModel()

        # State variables
        self.soc = 50.0          # % (initial assumption)
        self.soc_std = 10.0      # % — 1σ SoC uncertainty from the EKF covariance (live)
        self.soc_initial = 50.0  # % ใช้เป็น reference ของ Coulomb counting
        self.soh = 100.0         # %
        # Initial displayed rin on the SAME basis the EKF branch will report from
        # the first update() onward ((R0+R1) of _ekf_rc_defaults()) — not the bare
        # chemistry base_rin. A real session CSV (test_IEC_20260708_203952) showed
        # the display sitting at the generic 30 mΩ through the whole pre-test rest
        # and then jumping to ~64 mΩ at the first sample: same battery, same
        # session, two different uncalibrated-placeholder regimes purely from
        # object-init order. (Both are still guesses until a fit lands — the
        # _ecm_calibrated flag below is what the UI keys "measured vs estimated"
        # off of — but at least the guess is now one continuous number.)
        _r0_d, _r1_d, _ = self._ekf_rc_defaults()
        self.rin = _r0_d + _r1_d  # Ohm

        # Coulomb counting
        self.ah_accumulated = 0.0   # Ah นับจาก initial SoC
        self.coulomb_efficiency = 0.99  # fallback for non-lead-acid chemistries

        # --- Current-offset tare (drift source #4): sensor bias removed during rest ---
        self.current_offset = 0.0       # A — subtracted from every reading
        self._tare_sum = 0.0            # auto-tare accumulator (during rest)
        self._tare_n = 0
        self._auto_tare = True          # estimate offset from rest segments

        # --- Self-discharge (off by default; bench tests too short to matter) ---
        # %/day leak; only applied when explicitly enabled via set_self_discharge().
        self.self_discharge_pct_per_day = 0.0

        # --- Live SoH from full→empty capacity sweep (#1/#2) ---
        # capacity counter runs independently of the re-anchoring CC resets
        self._cap_counter_ah = 0.0      # raw |discharge Ah| since last 100% anchor
        self._cap_counting = False      # True after a 100% (full) anchor is seen
        self.measured_capacity_ah = 0.0 # last full-sweep measured capacity

        # --- EKF (2-state, 1-RC) fusion ---
        self.use_ekf = True
        self.use_adaptive_r = True      # AEKF: de-weight voltage on model mismatch (real data)
        self._ekf = None                # lazily created on first update()

        # --- SoC-dependent ECM (R0/R1/C1 vs SoC) ---
        # R0/R1/C1 change strongly with SoC; a single fixed fit degrades the EKF's
        # voltage prediction at the extremes. When a table is provided (from an HPPC
        # sweep via characterization.build_ecm_table) the RC dynamics + R0 used by the
        # EKF are interpolated at the current SoC each step. None → single fixed fit.
        self.ecm_table = None
        self._ecm_grid = None           # cached (soc, r0, r1, c1) numpy arrays
        # Until a real fit lands, self.rin is _ekf_rc_defaults()'s uncalibrated guess
        # (base_rin with a deliberate safety margin, R0+R1 summed) — plausible enough to
        # keep the EKF sane, but it isn't a measurement. Operators comparing it against a
        # bench meter (ACIR) before any HPPC pulse has run kept mistaking a guess for a
        # reading, so the UI needs to know which one it's currently displaying.
        self._ecm_calibrated = False
        # SoC at which the EKF's current R0/R1 were established (a fit or a step
        # detection) — lets the live rin follow the chemistry's SoC U-shape
        # relative to where the values were actually measured, instead of showing
        # a flat resistance all the way into the low-SoC region where lead-acid
        # genuinely rises (a real IEC discharge logged 66→65 mΩ, mildly FALLING,
        # across a full 100%→cutoff sweep). Defaults to 50% (where the chemistry
        # r0 baseline itself is specified) until something real lands.
        self._ecm_fit_soc = 50.0

        # --- Universal single-step R0 detector ---
        # update() is the one function EVERY mode routes through (manual charge/
        # discharge via _monitor_loop, IEC/Quick Scan/Cycle Life's own discharge
        # loops, HPPC's CHARGE phase, and now HPPC's pulse/relax legs too) — so a
        # real current-step edge detected HERE improves R0 accuracy everywhere,
        # without wiring per-mode. HPPC additionally runs a full per-cycle ECM fit
        # on top of this (see sequences.py). Deliberately R0-ONLY: a single step
        # can't resolve R1/C1
        # (needs the relaxation decay shape), so _r0_calibrated is a separate,
        # easier-to-reach flag from the stricter _ecm_calibrated above (which
        # stays reserved for a real full R0+R1+C1 fit, and is what the UI's
        # "measured vs estimated" Rin label keys off — an R0-only step shouldn't
        # claim the full Rin is a real measurement when R1/C1 are still guesses).
        self._step_buf = []          # [(voltage, current), ...] short rolling history
        self._r0_calibrated = False  # gates the EKF's uncalibrated-R0 runaway guard

        # --- Ablation flags (for replay.py study; all ON = full model) ---
        self.use_ocv = True             # OCV-based correction / EKF measurement update
        self.use_peukert = True         # Peukert discharge correction
        self.use_eta = True             # coulombic efficiency on charge
        self.use_temp = True            # temperature compensation (OCV Nernst + Rin)

        # --- Trapezoidal current integration (research: lower drift than rectangular) ---
        self._last_current = None       # previous tared current for trapezoid rule

        # --- Peukert minimum-sustain gate ---
        # Peukert's law models capacity depletion vs. a SUSTAINED constant-current
        # discharge rate (it's derived from full discharge curves at different fixed
        # rates) — applying it to a brief current pulse (an HPPC/DCIR pulse is
        # typically 10-30 s) is a physics misapplication that over-penalises SoC
        # during every pulse. Track how long the CURRENT discharge has been
        # continuous; only engage Peukert once it's been sustained past this
        # threshold (well beyond a typical pulse, short enough to engage quickly for
        # a genuine CC discharge test). Resets on any rest or polarity change, so an
        # HPPC test's repeated pulse/rest cycles never accumulate toward it.
        self._peukert_sustain_s = 0.0
        self._peukert_min_sustain_s = 60.0

        # --- Endpoint-anchor sustain gate ---
        # A real HPPC test caught this failing on real hardware: the 0% anchor's
        # ocv_est = voltage + cur*self.rin crossed its threshold by 0.0075 V for a
        # SINGLE sample (one noisy Rin/voltage reading, while self.rin was still an
        # uncalibrated pre-fit guess — see rin_calibrated), hard-resetting SoC from
        # 65% to 0% mid-pulse and wrecking the whole test's grade. The condition
        # itself is fine — the bug was trusting one instantaneous sample. Require it
        # to hold continuously for _anchor_min_sustain_s before firing, same pattern
        # as the Peukert sustain gate above; a genuinely full/empty pack stays past
        # the threshold far longer than one glitchy sample, a marginal fluke doesn't.
        #
        # A real Quick Scan run (test_QuickScan_20260712_150458.csv) found the
        # dt-only version of this gate still fires on one sample: Quick Scan/IEC/
        # Cycle Life discharge loops poll every ~5s, so a single sample's dt alone
        # (~5s) already clears _anchor_min_sustain_s (3s) — the "hold continuously"
        # requirement was silently a no-op for any loop slower than the threshold.
        # SoC hard-reset 24.25%->0.00% on a single ~2mV-over-threshold sample while
        # the pack kept discharging another 4.6 min to the real voltage cutoff.
        # Also require a minimum number of consecutive qualifying samples so one
        # glitchy reading can never satisfy "sustained" by itself, regardless of
        # how slow the calling loop's cadence is.
        self._full_anchor_sustain_s = 0.0
        self._zero_anchor_sustain_s = 0.0
        self._full_anchor_count = 0
        self._zero_anchor_count = 0
        self._anchor_min_sustain_s = 3.0
        self._anchor_min_samples = 2

        # OCV correction
        # monotonic (not time.time()): this is a pure elapsed-duration check ("has N
        # seconds passed"), never a real timestamp — monotonic is immune to NTP/wall-
        # clock jumps that could otherwise fire the correction early/late or compute a
        # negative interval.
        self.last_ocv_correction_time = time.monotonic()
        self.ocv_correction_interval = 300  # วินาที (5 นาที)
        self.last_static_voltage = None
        # standby_current = known idle/rest current offset (discharge-positive convention),
        # e.g. residual sensor leakage. Default 0 — the SSR relay (ESP32 GPIO16) now
        # physically disconnects the PSU from the battery whenever not charging, so
        # there is no PSU bleed current to compensate for. Override via
        # set_standby_current() only if a specific instrument has a known offset.
        self.standby_current = 0.0            # A — default; override via set_standby_current()
        self.static_current_threshold = 0.15  # A — window around standby
        self._rested_s = 0.0                  # accumulated rest time (s) for endpoint anchor
        # Minimum rest time before OCV correction fires (chemistry-dependent).
        # LFP plateau relaxation needs 10-30 min; Li-ion ~2-5 min.
        self._min_rest_s = self._default_min_rest_s()

        # Post-anchor settle window: right after a 100%/0% endpoint anchor, the
        # terminal voltage is still surface-charge-inflated (charge) or freshly
        # polarised (discharge) — trusting it immediately can pull the EKF's SoC away
        # from a genuinely-correct anchor before the transient dissipates. Reuses
        # _min_rest_s (same "how long until voltage means something again" question
        # this constant already answers elsewhere) rather than adding a new knob.
        self._anchor_settle_until = 0.0        # time.monotonic() deadline; 0 = inactive
        self._anchor_settle_r_mult = 200.0     # measurement-variance inflation factor

        # --- Surface-charge gate latch (F3) ---
        # The surface-charge gate (see _fuse_ekf) skips voltage correction while the
        # implied OCV sits above the curve's own 100% point (a still-surface-charged
        # lead-acid pack carries no discriminating SoC information there). Two real
        # defects made the bare per-sample form of that gate release too early:
        #   (a) it compared the implied OCV built with self.rin — the blended
        #       estimate_rin value (~26-30 mΩ on a real run), NOT the R basis the
        #       EKF itself predicts terminal voltage with (its own R0+R1 ~48 mΩ).
        #       The smaller R made v_ocv_est read ~46 mV too LOW, so it crossed into
        #       range a full sample early while the pack was genuinely still charged.
        #   (b) even with the right R basis, the gate was edge-triggered with no
        #       hysteresis: because the terminal voltage sags monotonically under a
        #       discharge load, the implied OCV grazes the 100% line for exactly one
        #       sample, and that single frame was enough for one big-innovation EKF
        #       update to pin SoC at 100% (the exact HPPC 100%-pin the replay caught).
        # The latch below adds persistence: once tripped, the gate only releases after
        # the implied OCV has stayed in range for a sustained hold OR a genuine
        # in-range rest is seen — not on the first microvolt of crossing.
        self._surface_charge_latched = False
        self._surface_clear_s = 0.0
        self._SURFACE_CHARGE_CLEAR_HOLD_S = 15.0   # in-range hold before releasing

        # Exponential smoothing
        self.alpha = 0.05
        self.soc_filtered = 50.0

        # ข้าม OCV correction เมื่ออยู่บน plateau ที่ flat (slope ต่ำ → SoC ill-conditioned)
        self.min_ocv_slope = 0.003  # V ต่อ %SoC (ต่อเซลล์)

    # ------------------------------------------------------------------
    # Initialization helpers
    # ------------------------------------------------------------------

    def _default_min_rest_s(self) -> float:
        """Minimum seconds the current must be near standby before OCV correction fires.
        LFP plateau relaxation is slow; firing too early anchors on polarized voltage."""
        # battery_model.chemistry is already canonically resolved (via
        # battery_profiles.get_chemistry() in BatteryModel.__init__), so this
        # reads the resolved name directly instead of re-deriving it with an
        # ad-hoc substring match on the raw battery_type string — the same
        # inconsistency analysis.py's _cca_cutoff_v() and sequences/*.py's
        # en50342_capacity_conditions() had already moved away from.
        chem = getattr(self.battery_model.chemistry, "name", "")
        if chem == "LiFePO4":
            return 120.0    # 2 minutes: partial relaxation OK for periodic correction
        if chem == "LeadAcid":
            return 60.0
        return 30.0         # Li-ion / LiPO: faster kinetics

    # ------------------------------------------------------------------
    # Physics helpers
    # ------------------------------------------------------------------

    def _coulomb_eta(self, soc: float, current: float) -> float:
        """Faraday coulombic efficiency for a charging step.

        Lead-acid gassing loss is SoC-dependent (Faraday's 2nd law applied to
        competitive H₂O electrolysis reaction near full charge):
          SoC < 75%:  bulk, minor gassing → η = 0.97
          75–90%:     absorption, rising gassing → η = 0.92
          SoC > 90%:  heavy gassing (H₂ + O₂) → η = 0.75

        Li-ion / LFP: no significant side reaction → fixed η = 0.99.
        Discharge (current ≥ 0): no coulombic loss on discharge side → η = 1.0.
        """
        if current >= 0 or not self.use_eta:
            return 1.0
        chem = getattr(self.battery_model.chemistry, "name", "")
        if chem == "LeadAcid":
            if soc < 75.0:
                return 0.97
            if soc < 90.0:
                return 0.92
            return 0.75
        return self.coulomb_efficiency

    def _peukert_dah(self, current: float, dah: float) -> float:
        """Apply Peukert correction to a discharge Ah increment.

        Peukert's law: C_eff = C_rated × (I_rated / I)^(k-1)
        Equivalently, the incremental Ah cost at current I is scaled by
        (I / I_rated)^(k-1).  k=1.0 → no correction (Li-ion).
        Lead-acid k≈1.30, rated at 10-hour rate → I_rated = C_10 / 10.

        High current (I > I_rated): scale > 1 → SoC depletes faster (less
        usable capacity per actual Ah discharged).
        Low current (I < I_rated): scale < 1 → SoC depletes slower (more
        usable capacity at slow discharge).

        Pure Peukert math + the use_peukert flag only — the "is this discharge
        sustained long enough to apply Peukert at all" decision (see
        ``_peukert_sustain_s`` in __init__/update()) is made by the caller, so this
        method stays directly testable in isolation.
        """
        if not self.use_peukert:
            return dah
        k   = getattr(self.battery_model.chemistry, "peukert_k",  1.0)
        hr  = getattr(self.battery_model.chemistry, "peukert_hr", 20.0)
        if k <= 1.0 or hr <= 0 or self.rated_capacity <= 0:
            return dah
        i_rated = self.rated_capacity / hr
        if i_rated <= 0:
            return dah
        scale = (current / i_rated) ** (k - 1.0)
        return dah * min(scale, 5.0)   # cap at 5× — protects against huge pulse spikes

    def effective_capacity(self) -> float:
        """SoH-adjusted usable capacity (Ah).

        Coulomb counting MUST divide accumulated Ah by the *actual* capacity, not the
        nameplate rating: an aged cell at 80 % SoH that still uses rated Ah would have
        its SoC over-estimated (Analog Devices / Sunlit Energy BMS notes). As SoH is
        measured from a full sweep this shrinks accordingly.
        """
        cap = self.rated_capacity * (self.soh / 100.0)
        return max(0.05 * self.rated_capacity, cap)   # floor: never divide by ~0

    def set_soh(self, soh: float) -> None:
        """Externally set SoH (e.g. from analysis.py full-discharge capacity)."""
        with self._lock:
            self.soh = max(0.0, min(120.0, float(soh)))
            # D3: keep the Rin baseline's aging term in sync with the live measured SoH —
            # see BatteryModel.set_aging_from_soh.
            self.battery_model.set_aging_from_soh(self.soh)

    def reset_battery_state(self) -> None:
        """Clear everything this instance has learned about the PREVIOUS physical
        battery — call when the operator selects a different product/battery, not
        between cycles of the same multi-cycle test (Cycle Life legitimately wants SoH
        to evolve across its own cycles; don't call this there).

        Without this, effective_capacity() = rated_capacity * (soh/100) keeps using
        whatever SoH the last-tested battery happened to end at. A brand-new battery
        swapped in afterward (soh should read 100%) instead inherits e.g. a prior
        aged unit's 60% SoH, making the capacity denominator too small — coulomb
        counting then races to 100% SoC during CC/bulk charge, well before voltage
        even reaches the absorption/CV ceiling, because every real Ah put in reads as
        a bigger SoC jump than it should.

        Also clears the previous battery's live R0/R1/C1 fit and its SoC-shape
        anchor (_ecm_fit_soc) — without this, the new battery inherits a stale
        calibration: _r0_calibrated/_ecm_calibrated staying True bypasses the
        uncalibrated-R0 EKF safety guard for the new pack, and _ecm_fit_soc staying
        at the old battery's anchor skews the live rin SoC U-shape correction from
        the very first sample."""
        with self._lock:
            self.soh = 100.0
            self.battery_model.set_aging_from_soh(None)  # D3: also un-age the Rin baseline
            self.measured_capacity_ah = 0.0
            self._cap_counting = False
            self._cap_counter_ah = 0.0
            self._ekf = None                # recreated from fresh defaults on next update()
            self.ecm_table = None
            self._r0_calibrated = False
            self._ecm_calibrated = False
            self._ecm_fit_soc = 50.0
            self._surface_charge_latched = False   # F3: don't carry a latch across packs
            self._surface_clear_s = 0.0



    # Un-calibrated packs (before a real HPPC fit) run noticeably higher than the
    # idealized per-cell datasheet r0: connector/contact resistance, cabling, and
    # corrosion aren't in the chemistry model. E.g. a small 5-7 Ah motorcycle AGM's
    # base_rin computes to ~30 mΩ pack, but such packs commonly measure ~50-80 mΩ in
    # practice. Applied only to the EKF's transient DEFAULT — never to the chemistry
    # base_rin used elsewhere (grading baselines, aging references, etc.) — and always
    # overwritten once update_ecm()/set_ecm_table() supplies a real fit.
    _EKF_UNCALIBRATED_R0_MARGIN = 1.7

    def _ekf_rc_defaults(self):
        """Initial 1-RC parameters for the EKF from the pack model.
        R0 from base_rin (with a margin — see _EKF_UNCALIBRATED_R0_MARGIN); R1≈0.6·R0,
        C1 chosen so τ≈30 s (lead-acid diffusion order).
        Overwritten by a real HPPC ECM fit via update_ecm()."""
        r0 = max(1e-3, float(self.battery_model.base_rin) * self._EKF_UNCALIBRATED_R0_MARGIN)
        r1 = 0.6 * r0
        tau = 30.0
        c1 = tau / max(1e-4, r1)
        return r0, r1, c1

    def _ensure_ekf(self):
        if self._ekf is None:
            r0, r1, c1 = self._ekf_rc_defaults()
            # adaptive_r ON: the real bench uses a generic OCV table (until per-cell GITT),
            # a 5 Hz readback and real sensor noise, so the voltage model is imperfect.
            # AEKF inflates R from the measured innovation variance and de-weights the
            # voltage when the model disagrees → more accurate/robust live SoC on real
            # data (floored at the sensor noise so good OCV info is still used).
            self._ekf = SoCEKF(self.soc, r0, r1, c1, adaptive_r=self.use_adaptive_r)
        return self._ekf

    # Minimum |Δcurrent| between the rolling-buffer reference and the new sample
    # to count as a real step, not sensor jitter/noise.
    _STEP_MIN_DI_A = 0.15
    # Post-edge sample must arrive within this long of the edge, or the voltage
    # has already relaxed into the RC region. Shared with
    # acquisition.analysis._DCIR_MAX_STEP_DT (the batch/offline method reading
    # the same physical rig) via battery_model.MAX_STEP_EDGE_LATENCY_S.
    _STEP_MAX_DT_S = MAX_STEP_EDGE_LATENCY_S
    # The "before" reference itself must look like a genuine settled level, not
    # already mid-transition. Shared with
    # acquisition.analysis._VI_LEVEL_MAX_SPREAD_V via
    # battery_model.STEADY_STATE_MAX_SPREAD_V.
    _STEP_REF_MAX_SPREAD_V = STEADY_STATE_MAX_SPREAD_V
    _STEP_BUF_LEN = 3

    def _detect_step_r0(self, voltage: float, current: float, dt: float, temp: float) -> None:
        """Online counterpart to acquisition.analysis.identify_dcir's single-step
        method (R = |ΔV/ΔI| across a clean current edge) — streamed sample-by-
        sample instead of batch over a whole CSV, so it improves R0 in every mode
        this estimator is used in without any per-mode wiring. See the _r0_calibrated
        attribute's comment in __init__ for why this only ever touches R0."""
        buf = self._step_buf
        if len(buf) >= self._STEP_BUF_LEN:
            vs = [v for v, i in buf]
            ref_spread = max(vs) - min(vs)
            if ref_spread <= self._STEP_REF_MAX_SPREAD_V:
                v_ref = sorted(vs)[len(vs) // 2]
                i_ref = sorted(i for v, i in buf)[len(buf) // 2]
                di = current - i_ref
                if abs(di) >= self._STEP_MIN_DI_A and dt <= self._STEP_MAX_DT_S:
                    temp_mult = self.battery_model.temp_rin_multiplier(temp) if self.use_temp else 1.0
                    r0 = abs((voltage - v_ref) / di) / max(1e-6, temp_mult)
                    # Sanity band RELATIVE to the chemistry's own baseline, not a
                    # bare absolute one — see battery_model.is_plausible_r0()'s
                    # docstring for the two real failure modes this guards
                    # against (a fixed ceiling alone accepted a CV-taper
                    # polarisation edge; a bare relative band alone accepted a
                    # stale-voltage-readback ΔV=0 edge).
                    if is_plausible_r0(r0, self.battery_model.base_rin):
                        if self.use_ekf:
                            ekf = self._ensure_ekf()
                            ekf.set_rc(r0, ekf.R1, ekf.C1)
                        self._r0_calibrated = True
                        self._ecm_fit_soc = self.soc   # SoC-shape anchor for live rin
        buf.append((voltage, current))
        if len(buf) > self._STEP_BUF_LEN:
            buf.pop(0)

    def update_ecm(self, r0: float, r1: float, c1: float, fit_soc: float = None) -> None:
        """Feed a fresh single HPPC ECM fit into the EKF (R0/R1/C1 in Ohm/Ohm/Farad).
        Ignored while a SoC-dependent ECM table is active (the table takes precedence).

        fit_soc: SoC at the moment the pulse used for this fit actually happened.
        Pass this explicitly when the caller feeds the fit well after the fact (e.g.
        a post-hoc analysis that only runs once the whole record's sample loop has
        finished, by which point self.soc has already moved on) — otherwise this
        defaults to self.soc, correct only when called right after the pulse (the
        live per-cycle HPPC sequence feed)."""
        with self._lock:
            if self.ecm_table is None and self._ekf is not None and r0 > 0 and r1 > 0 and c1 > 0:
                self._ekf.set_rc(r0, r1, c1)
                self._ecm_calibrated = True
                # SoC-shape anchor for live rin
                self._ecm_fit_soc = self.soc if fit_soc is None else float(fit_soc)



    def _ocv_init_var(self, soc: float, temp: float) -> float:
        """SoC covariance to seed an OCV-derived anchor with. On a flat plateau (low
        dOCV/dSoC) the inversion is unreliable → large variance (±~15%) so the EKF stays
        correctable; near a knee (steep slope) it is trustworthy → small (±~3%)."""
        slope = self.battery_model.ocv_slope(soc, temp)
        return 225.0 if slope < self.min_ocv_slope else 9.0

    def sync_with_ocv(self, voltage: float, temp: float = 25.0) -> float:
        """Force synchronize SoC กับ OCV (ใช้หลัง rest period)"""
        with self._lock:
            soc = self.battery_model.get_soc_from_ocv(voltage, temp)
            self._reset_to_soc(soc, soc_var=self._ocv_init_var(soc, temp))
            logger.info(f"SoC synced with OCV: {voltage:.3f}V -> {self.soc:.1f}%")
            return self.soc

    def _reset_to_soc(self, soc: float, soc_var: float = 1.0,
                      start_settle_window: bool = False) -> None:
        """Reset state ทั้งหมดให้ตรงกับ soc. ``soc_var`` = ความไม่แน่นอนของ SoC ที่ตั้งให้
        EKF: ~1 สำหรับ endpoint anchor (เชื่อได้), ใหญ่สำหรับ OCV init บน plateau.

        start_settle_window: True only for the 100%/0% endpoint anchors (fire mid
        charge/discharge, right where surface charge/polarisation is worst) — NOT for
        sync_with_ocv()/init_from_voltage(), which already require the caller to have
        waited out a real rest (calibrate_from_ocv_stable's ΔV/Δt settle) before firing,
        so there's no fresh transient there to guard against."""
        self.soc = soc
        self.soc_initial = soc
        self.soc_filtered = soc
        self.ah_accumulated = 0.0
        self.last_ocv_correction_time = time.monotonic()
        self._last_current = None      # avoid a stale trapezoid average after a reset
        # An explicit re-anchor to a known SoC supersedes any pending surface-charge
        # latch — the anchor is the trustworthy value now, so don't keep the gate
        # closed on the pre-anchor voltage history (F3).
        self._surface_charge_latched = False
        self._surface_clear_s = 0.0
        if start_settle_window:
            self._anchor_settle_until = time.monotonic() + self._min_rest_s
        if self._ekf is not None:
            self._ekf.set_soc(soc, soc_var)

    # ------------------------------------------------------------------
    # Main update
    # ------------------------------------------------------------------

    def _tare_current(self, current: float) -> float:
        if self._auto_tare and abs(current - self.standby_current) < self.static_current_threshold:
            self._tare_sum += (current - self.standby_current)
            self._tare_n += 1
            if self._tare_n >= 20:
                self.current_offset = max(-0.5, min(0.5, self._tare_sum / self._tare_n))
                self._tare_sum = 0.0
                self._tare_n = 0
        return current - self.current_offset

    def _update_coulomb_counting_and_rest(self, cur: float, dt: float) -> tuple[float, float, float]:
        i_eff = cur if self._last_current is None else 0.5 * (cur + self._last_current)
        self._last_current = cur
        eta = self._coulomb_eta(self.soc, i_eff)
        dah = i_eff * (dt / 3600.0)

        if cur > 0.05:
            self._peukert_sustain_s += dt
        else:
            self._peukert_sustain_s = 0.0

        if i_eff < 0:
            dah *= eta
        else:
            if self._peukert_sustain_s >= self._peukert_min_sustain_s:
                dah = self._peukert_dah(i_eff, dah)

        if self.self_discharge_pct_per_day > 0.0:
            dah += (self.self_discharge_pct_per_day / 100.0) * self.effective_capacity() * (dt / 86400.0)
        self.ah_accumulated += dah

        if abs(cur - self.standby_current) < self.static_current_threshold:
            self._rested_s += dt
        else:
            self._rested_s = 0.0

        if self._cap_counting and i_eff > 0:
            self._cap_counter_ah += i_eff * (dt / 3600.0)

        cap = self.effective_capacity()
        soc_cc = self.soc_initial - (self.ah_accumulated / cap) * 100.0
        return i_eff, dah, max(0.0, min(100.0, soc_cc))

    def _apply_endpoint_anchors(self, voltage: float, cur: float, dt: float) -> float:
        cp = self.battery_model.charge_profile
        s  = self.battery_model.series_cells
        full_v_cell = cp.cv_voltage_per_cell or cp.absorption_voltage_per_cell
        
        # 100% anchor
        if full_v_cell > 0:
            anchor_v_full = full_v_cell * s * 0.986
            anchor_i_tail = max(0.25, self.rated_capacity * cp.tail_current_c_rate * 1.5)
            full_anchor_cond = (cur < 0 and voltage >= anchor_v_full and abs(cur) <= anchor_i_tail and self.soc < 98.0)
            self._full_anchor_sustain_s = self._full_anchor_sustain_s + dt if full_anchor_cond else 0.0
            self._full_anchor_count = self._full_anchor_count + 1 if full_anchor_cond else 0
            if (full_anchor_cond and self._full_anchor_sustain_s >= self._anchor_min_sustain_s
                    and self._full_anchor_count >= self._anchor_min_samples):
                logger.info("Endpoint anchor -> 100%%: %.3fV (>=%.3f) I=%.3fA tail=%.3fA", voltage, anchor_v_full, cur, anchor_i_tail)
                self._reset_to_soc(100.0, start_settle_window=True)
                self._cap_counting = True
                self._cap_counter_ah = 0.0
                return 100.0

        # 0% anchor
        anchor_v_empty = self.battery_model.get_ocv_from_soc(0.0)
        anchor_i_max = self.rated_capacity * 2.0
        # The IR-compensated estimate below (ocv_est = voltage + cur*self.rin) is
        # only as trustworthy as self.rin. Before any real R0 fit lands, self.rin
        # is _ekf_rc_defaults()'s generic pre-fit guess for the chemistry, not a
        # value fitted to THIS pack. A real Quick Scan run
        # (test_QuickScan_20260712_150458.csv, rin_calibrated=False the entire
        # run) under-compensated the IR drop enough to cross the 1% margin and
        # hard-reset SoC 24.25%->0.00% while the pack kept discharging another
        # 4.6 min to its real voltage cutoff — and the bias was systematic (not a
        # single noisy glitch), so it held past the existing _anchor_min_samples
        # consecutive-sample requirement too. This mirrors the exact "don't trust
        # a voltage-based correction while actively loaded and uncalibrated" rule
        # _fuse_ekf already applies to its own OCV update (see
        # uncalibrated_and_active there) — the loaded zero-anchor has the
        # identical failure mode and needs the identical guard. The anchor
        # becomes available again as soon as real R0/ECM fitting lands (now
        # reachable earlier too — see the pre-edge sample fix in
        # quick_scan.py/iec_capacity.py/cycle_life.py).
        r0_confirmed = self._r0_calibrated or self._ecm_calibrated
        ocv_est = voltage + cur * self.rin if cur > 0 else voltage
        zero_anchor_cond = (r0_confirmed and cur > 0 and cur <= anchor_i_max
                             and ocv_est <= anchor_v_empty * 1.01 and self.soc > 2.0)
        self._zero_anchor_sustain_s = self._zero_anchor_sustain_s + dt if zero_anchor_cond else 0.0
        self._zero_anchor_count = self._zero_anchor_count + 1 if zero_anchor_cond else 0
        if (zero_anchor_cond and self._zero_anchor_sustain_s >= self._anchor_min_sustain_s
                and self._zero_anchor_count >= self._anchor_min_samples):
            logger.info("Endpoint anchor -> 0%%: est.OCV %.3fV (<=%.3f) meas=%.3fV I=%.3fA", ocv_est, anchor_v_empty, voltage, cur)
            if self._cap_counting and self._cap_counter_ah > 0.30 * self.rated_capacity:
                self.measured_capacity_ah = self._cap_counter_ah
                self.soh = max(0.0, min(120.0, self._cap_counter_ah / self.rated_capacity * 100.0))
                self.battery_model.set_aging_from_soh(self.soh)
                logger.info("Live SoH <- full->empty sweep: %.3f Ah / %.3f rated = %.1f%%", self._cap_counter_ah, self.rated_capacity, self.soh)
            self._cap_counting = False
            self._reset_to_soc(0.0, start_settle_window=True)
            return 0.0
            
        return None

    def _fuse_ekf(self, voltage: float, cur: float, i_eff: float, dah: float, dt: float, t_use: float, soc_cc: float) -> dict:
        ekf = self._ensure_ekf()
        s = self.battery_model.series_cells
        r0_use = ekf.R0
        if self.ecm_table is not None:
            r0s, r1s, c1s = self._ecm_at_soc(ekf.soc)
            ekf.set_rc(r0s, r1s, c1s)
            r0_use = r0s
            
        cap = self.effective_capacity()
        soc_delta = dah / cap * 100.0 if cap > 0 else 0.0
        ekf.predict(i_eff, dt, soc_delta)
        self.soc = max(0.0, min(100.0, ekf.soc))
        
        if self.use_ocv:
            direction = 0 if abs(cur) < 0.05 else (-1 if cur > 0 else 1)
            ocv_pack = self.battery_model.get_ocv_from_soc(ekf.soc, t_use, direction)
            docv = self.battery_model.ocv_slope(ekf.soc, t_use) * s
            
            import time
            still_settling = time.monotonic() < self._anchor_settle_until
            r_override = ekf.R * self._anchor_settle_r_mult if still_settling else None
            
            r0_confirmed = self._r0_calibrated or self._ecm_calibrated
            uncalibrated_and_active = (not r0_confirmed and abs(cur - self.standby_current) >= self.static_current_threshold)
            near_rest = abs(cur - self.standby_current) < self.static_current_threshold
            # Surface-charge gate, evaluated on the IMPLIED OCV (terminal voltage
            # with the ohmic sag added back for discharge-positive current), not
            # just the raw rest voltage: right after a lead-acid charge the first
            # minutes of DISCHARGE still read V + I*R above the curve's own 100%
            # point — such a sample maps to "≥100%" no matter the true SoC, so it
            # carries zero discriminating information under load exactly as it
            # does at rest. Without the loaded form of this gate, a real replay
            # (test_20260709_154818) showed the EKF pinning SoC at 100% for the
            # first ~8 min of discharge (~0.37 Ah erased) before the voltage
            # dropped back inside the curve.
            # (a) Build the implied OCV with the SAME R basis the EKF predicts
            # terminal voltage from (its own R0+R1), not self.rin (the blended
            # estimate_rin value, ~26-30 mΩ on a real run vs the EKF's ~48 mΩ) —
            # the smaller R under-stated v_ocv_est by ~46 mV and let the gate open a
            # sample early. See the F3 note in __init__.
            v_ocv_est = voltage + max(0.0, cur) * (r0_use + ekf.R1)
            raw_surface = self.battery_model.ocv_out_of_range_mv(v_ocv_est, t_use) > 0.0
            # (b) Hysteresis: once surface charge is detected, hold the gate closed
            # until the implied OCV has stayed in range for a sustained window (or a
            # genuine in-range rest is seen) — not the first frame it grazes the line.
            if raw_surface:
                self._surface_charge_latched = True
                self._surface_clear_s = 0.0
            elif self._surface_charge_latched:
                self._surface_clear_s += dt
                cleared_by_hold = self._surface_clear_s >= self._SURFACE_CHARGE_CLEAR_HOLD_S
                cleared_by_rest = near_rest and self._rested_s >= self._min_rest_s
                if cleared_by_hold or cleared_by_rest:
                    self._surface_charge_latched = False
                    self._surface_clear_s = 0.0
            surface_charged = raw_surface or self._surface_charge_latched
            still_polarised = near_rest and self._rested_s < self._min_rest_s
            # While CHARGING (convention: charge = negative current), the terminal
            # voltage carries no usable SoC information: it sits at the charger's
            # bulk/absorption setpoint plus gassing/CV overpotential the 1-RC model
            # doesn't represent, so the innovation is systematically positive and
            # drags SoC to 100% almost immediately. Three real sessions show it:
            # SoC hit 100% after 142 s of a 242-min charge (test_HPPC_20260708) and
            # after 28 s of a 102-min charge (test_20260709_154818) — 99% of the
            # real Ah went in AFTER the display already read full. The near-rest
            # gates above can't catch this (they only apply at ~zero current), so
            # charging samples are skipped outright: SoC advances on coulomb
            # counting + coulombic-efficiency only, and voltage-based correction
            # resumes at the next rest (polarization-gated) or discharge.
            charging = (cur - self.standby_current) < -self.static_current_threshold

            if not uncalibrated_and_active and not surface_charged \
                    and not still_polarised and not charging:
                ekf.update(voltage, cur, ocv_pack, docv, r0_use, r_override=r_override)
                self.soc = max(0.0, min(100.0, ekf.soc))
                
        self.soc_filtered = self.soc
        self.soc_std = float(max(0.0, float(ekf.P[0, 0])) ** 0.5)
        
        temp_mult = self.battery_model.temp_rin_multiplier(t_use) if self.use_temp else 1.0
        if self.ecm_table is None:
            soc_coeff = float(self.battery_model.rin_params.get("soc_coeff", 0.0))
            shape_now = 1.0 + soc_coeff * abs(self.soc - 50.0)
            shape_fit = 1.0 + soc_coeff * abs(self._ecm_fit_soc - 50.0)
            temp_mult *= shape_now / max(1e-6, shape_fit)
        self.rin = (ekf.R0 + ekf.R1) * temp_mult
        
        return {
            "soc": self.soc,
            "soc_std": self.soc_std,
            "soh": self.soh,
            "rin": self.rin,
            "rin_calibrated": self._ecm_calibrated,
            "ah_accumulated": self.ah_accumulated,
        }

    def _fallback_ocv_correction(self, voltage: float, cur: float, t_use: float, soc_cc: float) -> dict:
        import time
        now = time.monotonic()
        if self.use_ocv and abs(cur - self.standby_current) < self.static_current_threshold:
            self.last_static_voltage = voltage
            ocv_voltage = voltage + self.standby_current * self.rin
            ocv_soc = self.battery_model.get_soc_from_ocv(ocv_voltage, t_use)
            slope = self.battery_model.ocv_slope(ocv_soc, t_use)
            drift = abs(self.soc_filtered - ocv_soc)
            steep = slope >= 2.0 * self.min_ocv_slope
            periodic = (now - self.last_ocv_correction_time) >= self.ocv_correction_interval
            
            if (self._rested_s >= self._min_rest_s and slope >= self.min_ocv_slope
                    and self.battery_model.ocv_out_of_range_mv(ocv_voltage, t_use) <= 0.0
                    and (steep or (periodic and drift > 3.0))):
                w = 0.9 if steep else 0.8
                corrected = w * ocv_soc + (1.0 - w) * soc_cc
                logger.info("OCV %s: CC=%.1f%% OCV=%.1f%% slope=%.4f -> %.1f%%",
                            "endpoint-reset" if steep else "correction",
                            soc_cc, ocv_soc, slope, corrected)
                self.soc_filtered = corrected
                self.soc_initial = corrected
                self.ah_accumulated = 0.0
                soc_cc = corrected
                self.last_ocv_correction_time = now
                self._rested_s = 0.0
        else:
            self.last_static_voltage = None

        self.soc_filtered = (1 - self.alpha) * self.soc_filtered + self.alpha * soc_cc
        self.soc = max(0.0, min(100.0, self.soc_filtered))

        return {
            "soc": self.soc,
            "soh": self.soh,
            "rin": self.rin,
            "rin_calibrated": True,
            "ah_accumulated": self.ah_accumulated
        }

    def update(self, voltage: float, current: float, dt: float,
               temp: float = 25.0, measured_dcir: float = 0.0) -> dict:
        """
        Update state estimation
        
        Args:
            voltage: Terminal voltage (V)
            current: Net current (A, positive = discharge)
            dt: Time step (seconds)
            temp: Temperature (°C)
            measured_dcir: Measured DCIR จาก transient test (Ohm)
            
        Returns:
            dict: {soc, soh, rin, ah_accumulated}
        """
        with self._lock:
            cur = self._tare_current(current)
            t_use = temp if self.use_temp else 25.0

            self._detect_step_r0(voltage, cur, dt, t_use)

            i_eff, dah, soc_cc = self._update_coulomb_counting_and_rest(cur, dt)

            anchor_soc = self._apply_endpoint_anchors(voltage, cur, dt)
            if anchor_soc is not None:
                soc_cc = anchor_soc

            self.rin = self.battery_model.estimate_rin(
                voltage, cur, self.soc, temp=t_use, measured_dcir=measured_dcir
            )

            if self.use_ekf:
                return self._fuse_ekf(voltage, cur, i_eff, dah, dt, t_use, soc_cc)
            else:
                return self._fallback_ocv_correction(voltage, cur, t_use, soc_cc)

    # ------------------------------------------------------------------
    # Getters
    # ------------------------------------------------------------------

    def get_state(self) -> dict:
        return {
            "soc": self.soc,
            "soh": self.soh,
            "rin": self.rin,
            "rin_calibrated": self._ecm_calibrated if self.use_ekf else True,
            "ah_accumulated": self.ah_accumulated,
            "coulomb_efficiency": self.coulomb_efficiency
        }