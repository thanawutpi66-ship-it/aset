"""
Advanced State Estimation: SoC estimation ด้วย Coulomb counting + OCV correction
+ 2-state EKF (1-RC) fusion, live SoH, SoH-adjusted capacity, current-offset tare.
"""
import time
from aset_batt.core.battery_model import BatteryModel
from aset_batt.core.soc_ekf import SoCEKF
import logging

logger = logging.getLogger(__name__)

class StateEstimator:
    """Adaptive & robust SoC estimator"""

    def __init__(self, rated_capacity: float, battery_model: BatteryModel = None):
        self.rated_capacity = rated_capacity  # Ah
        self.battery_model = battery_model or BatteryModel()

        # State variables
        self.soc = 50.0          # % (initial assumption)
        self.soc_std = 10.0      # % — 1σ SoC uncertainty from the EKF covariance (live)
        self.soc_initial = 50.0  # % ใช้เป็น reference ของ Coulomb counting
        self.soh = 100.0         # %
        self.rin = self.battery_model.base_rin  # Ohm

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
        self._full_anchor_sustain_s = 0.0
        self._zero_anchor_sustain_s = 0.0
        self._anchor_min_sustain_s = 3.0

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
        chem = getattr(self.battery_model, "battery_type", "").lower()
        if "lifepo" in chem or "lfp" in chem:
            return 120.0    # 2 minutes: partial relaxation OK for periodic correction
        if "lead" in chem:
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
        chem = getattr(self.battery_model, "battery_type", "").lower()
        if "lead" in chem:
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
        a bigger SoC jump than it should."""
        self.soh = 100.0
        self.battery_model.set_aging_from_soh(None)  # D3: also un-age the Rin baseline
        self.measured_capacity_ah = 0.0
        self._cap_counting = False
        self._cap_counter_ah = 0.0

    def set_self_discharge(self, pct_per_day: float) -> None:
        self.self_discharge_pct_per_day = max(0.0, float(pct_per_day))

    def set_current_offset(self, offset_a: float) -> None:
        """Manually set the current-sensor zero offset (A, discharge-positive)."""
        self.current_offset = float(offset_a)
        self._auto_tare = False

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

    def update_ecm(self, r0: float, r1: float, c1: float) -> None:
        """Feed a fresh single HPPC ECM fit into the EKF (R0/R1/C1 in Ohm/Ohm/Farad).
        Ignored while a SoC-dependent ECM table is active (the table takes precedence)."""
        if self.ecm_table is None and self._ekf is not None and r0 > 0 and r1 > 0 and c1 > 0:
            self._ekf.set_rc(r0, r1, c1)
            self._ecm_calibrated = True

    def set_ecm_table(self, table) -> None:
        """Provide R0/R1/C1 vs SoC (from characterization.build_ecm_table) so the EKF
        uses SoC-appropriate RC dynamics instead of one fixed fit. Pass None/empty to
        revert to the single-fit behaviour."""
        if not table:
            self.ecm_table = None
            self._ecm_grid = None
            return
        import numpy as np
        socs = sorted(table.keys())
        self.ecm_table = table
        self._ecm_grid = (
            np.asarray(socs, float),
            np.asarray([table[s]["r0"] for s in socs], float),
            np.asarray([table[s]["r1"] for s in socs], float),
            np.asarray([table[s]["c1"] for s in socs], float),
        )
        self._ecm_calibrated = True
        logger.info("ECM table active: %d SoC points (R0/R1/C1 now SoC-dependent)", len(socs))

    def _ecm_at_soc(self, soc: float):
        """Interpolate (R0, R1, C1) at the given SoC from the active table."""
        import numpy as np
        g = self._ecm_grid
        return (float(np.interp(soc, g[0], g[1])),
                float(np.interp(soc, g[0], g[2])),
                float(np.interp(soc, g[0], g[3])))

    def set_standby_current(self, standby_a: float) -> None:
        """Set a known idle/rest current offset (A). Default is 0 since the SSR
        physically disconnects the PSU when not charging — only needed if a
        specific instrument has a known residual leakage current at rest."""
        self.standby_current = max(0.0, float(standby_a))

    def _ocv_init_var(self, soc: float, temp: float) -> float:
        """SoC covariance to seed an OCV-derived anchor with. On a flat plateau (low
        dOCV/dSoC) the inversion is unreliable → large variance (±~15%) so the EKF stays
        correctable; near a knee (steep slope) it is trustworthy → small (±~3%)."""
        slope = self.battery_model.ocv_slope(soc, temp)
        return 225.0 if slope < self.min_ocv_slope else 9.0

    def init_from_voltage(self, voltage: float, temp: float = 25.0) -> None:
        """Initialize SoC จาก measured OCV (voltage) หลัง rest"""
        soc = self.battery_model.get_soc_from_ocv(voltage, temp)
        self._reset_to_soc(soc, soc_var=self._ocv_init_var(soc, temp))
        logger.info(f"SoC initialized from voltage: {voltage:.3f}V -> {self.soc:.1f}%")

    def set_initial_soc(self, soc: float) -> None:
        """Set initial SoC ด้วยตนเอง"""
        self._reset_to_soc(max(0.0, min(100.0, soc)))
        logger.info(f"Initial SoC set to {self.soc:.1f}%")

    def sync_with_ocv(self, voltage: float, temp: float = 25.0) -> float:
        """Force synchronize SoC กับ OCV (ใช้หลัง rest period)"""
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
        if start_settle_window:
            self._anchor_settle_until = time.monotonic() + self._min_rest_s
        if self._ekf is not None:
            self._ekf.set_soc(soc, soc_var)

    # ------------------------------------------------------------------
    # Main update
    # ------------------------------------------------------------------

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

        # === 0. Current-offset tare (#4) ===
        # Remove sensor bias estimated during rest so coulomb counting doesn't drift.
        # During genuine rest the true current equals the known standby current
        # (default 0); any deviation is sensor offset. Auto-tare only — never
        # perturbs an active step.
        if self._auto_tare and abs(current - self.standby_current) < self.static_current_threshold:
            self._tare_sum += (current - self.standby_current)
            self._tare_n += 1
            if self._tare_n >= 20:
                # bounded offset update (±0.5 A guard against a wild reading)
                self.current_offset = max(-0.5, min(0.5, self._tare_sum / self._tare_n))
                self._tare_sum = 0.0
                self._tare_n = 0
        cur = current - self.current_offset
        # temperature compensation flag: feed 25°C when disabled (ablation studies)
        t_use = temp if self.use_temp else 25.0

        # === 1. Coulomb Counting (SoH-adjusted capacity, #2) ===
        # current > 0 = discharge → SoC ลดลง ; current < 0 = charge → SoC เพิ่มขึ้น
        # Trapezoidal integration: ΔQ = (I_k + I_{k-1})/2 · Δt — lower drift than the
        # rectangular rule at the rig's slow ~5 Hz readback (Nature s41598-026-38281-5).
        i_eff = cur if self._last_current is None else 0.5 * (cur + self._last_current)
        self._last_current = cur
        eta = self._coulomb_eta(self.soc, i_eff)        # 1.0 on discharge
        dah = i_eff * (dt / 3600.0)
        # Track continuous-discharge duration for the Peukert sustain gate: resets on
        # rest or a switch to charging, so an HPPC test's pulse/rest cycles never
        # accumulate toward it — only a genuinely sustained discharge does. Uses the
        # RAW tared current (``cur``), not the trapezoidal-smoothed ``i_eff`` — i_eff
        # takes one extra sample to reflect a current drop to zero, which would delay
        # detecting "rest" by a sample; the OCV rest-check elsewhere in this method
        # uses the same raw ``cur`` for the same reason.
        if cur > 0.05:
            self._peukert_sustain_s += dt
        else:
            self._peukert_sustain_s = 0.0
        if i_eff < 0:
            dah *= eta
        else:
            # Discharging: Peukert correction (lead-acid), gated on sustained-discharge
            # duration — an HPPC/DCIR pulse (far shorter than _peukert_min_sustain_s)
            # never engages it, only a genuinely sustained discharge does. Capacity
            # counter (raw) below.
            if self._peukert_sustain_s >= self._peukert_min_sustain_s:
                dah = self._peukert_dah(i_eff, dah)
        # Self-discharge leak (off by default) — acts like a tiny discharge.
        if self.self_discharge_pct_per_day > 0.0:
            dah += (self.self_discharge_pct_per_day / 100.0) * \
                   self.effective_capacity() * (dt / 86400.0)
        self.ah_accumulated += dah

        # live-SoH capacity counter: raw |discharge Ah| since the last 100% anchor
        if self._cap_counting and i_eff > 0:
            self._cap_counter_ah += i_eff * (dt / 3600.0)

        # SoC from coulomb counting — divide by *effective* (SoH-adjusted) capacity
        cap = self.effective_capacity()
        soc_cc = self.soc_initial - (self.ah_accumulated / cap) * 100.0
        soc_cc = max(0.0, min(100.0, soc_cc))

        # === 1b. Endpoint Anchors (SoC Restoring Points) ===
        # Hard reset ที่ขอบบน/ล่าง — แก้ Coulomb counting drift บน flat LFP plateau
        # ทำงานระหว่าง active charge/discharge (ต่างจาก OCV correction ที่ต้องการกระแส ≈ 0)
        cp = self.battery_model.charge_profile
        s  = self.battery_model.series_cells
        # 100% anchor: ชาร์จ + V ≥ 98.6% ของ CV (~3.60V/cell × 8 = 28.8V สำหรับ LFP 8S)
        #              AND กระแสชาร์จ taper ถึง tail threshold (แบตใกล้เต็มแล้ว)
        full_v_cell = cp.cv_voltage_per_cell or cp.absorption_voltage_per_cell
        if full_v_cell > 0:
            anchor_v_full = full_v_cell * s * 0.986      # 3.65 × 0.986 × 8 = 28.8V (LFP 8S)
            # 1.5× headroom + 0.25 A floor so small C-rate rounding never blocks the anchor.
            anchor_i_tail = max(0.25, self.rated_capacity * cp.tail_current_c_rate * 1.5)
            full_anchor_cond = (cur < 0 and voltage >= anchor_v_full
                               and abs(cur) <= anchor_i_tail and self.soc < 98.0)
            self._full_anchor_sustain_s = self._full_anchor_sustain_s + dt if full_anchor_cond else 0.0
            if full_anchor_cond and self._full_anchor_sustain_s >= self._anchor_min_sustain_s:
                logger.info("Endpoint anchor → 100%%: %.3fV (≥%.3f) I=%.3fA tail=%.3fA",
                            voltage, anchor_v_full, cur, anchor_i_tail)
                self._reset_to_soc(100.0, start_settle_window=True)
                soc_cc = 100.0
                # start a fresh full→empty capacity sweep for live SoH
                self._cap_counting = True
                self._cap_counter_ah = 0.0
        # 0% anchor: discharge + estimated OCV ≤ OCV ที่ 0% ของแพ็ค (+ 1% hysteresis)
        #            2.50V/cell × 8 = 20.0V สำหรับ LFP 8S
        # ชดเชย I·R ก่อนเทียบ: ระหว่างมีโหลด V = OCV − I·R ต่ำกว่า OCV จริงตามธรรมชาติ
        # (ดู _uvp_floor ใน sequences.py) — เทียบ voltage ดิบตรงๆ เคยทำให้ HPPC pulse
        # 5A บนแบตที่เพิ่งชาร์จเต็มโดน anchor เป็น 0% ทันที (แรงดันยุบจาก I·R ไม่ใช่
        # เพราะแบตหมด) ใช้ self.rin (ค่าจากรอบก่อนหน้า — ยังไม่ถูกอัปเดตในรอบนี้ ดู
        # "=== 2." ด้านล่าง) ประมาณ OCV กลับ และจำกัดไว้แค่กระแส ≤ 2C กัน edge case ที่
        # Rin ใต้โหลดจริงเพี้ยนไปจากค่าตอนพัก
        anchor_v_empty = self.battery_model.get_ocv_from_soc(0.0)
        anchor_i_max = self.rated_capacity * 2.0
        ocv_est = voltage + cur * self.rin if cur > 0 else voltage
        zero_anchor_cond = (cur > 0 and cur <= anchor_i_max
                            and ocv_est <= anchor_v_empty * 1.01 and self.soc > 2.0)
        self._zero_anchor_sustain_s = self._zero_anchor_sustain_s + dt if zero_anchor_cond else 0.0
        if zero_anchor_cond and self._zero_anchor_sustain_s >= self._anchor_min_sustain_s:
            logger.info("Endpoint anchor → 0%%: est.OCV %.3fV (≤%.3f) meas=%.3fV I=%.3fA",
                        ocv_est, anchor_v_empty, voltage, cur)
            # live SoH from a full→empty sweep (#1): measured capacity ÷ rated.
            # Require a near-full sweep (> 30% rated) so partial discharges don't corrupt SoH.
            if self._cap_counting and self._cap_counter_ah > 0.30 * self.rated_capacity:
                self.measured_capacity_ah = self._cap_counter_ah
                self.soh = max(0.0, min(120.0,
                                        self._cap_counter_ah / self.rated_capacity * 100.0))
                self.battery_model.set_aging_from_soh(self.soh)  # D3: sync Rin aging term
                logger.info("Live SoH ← full→empty sweep: %.3f Ah / %.3f rated = %.1f%%",
                            self._cap_counter_ah, self.rated_capacity, self.soh)
            self._cap_counting = False
            self._reset_to_soc(0.0, start_settle_window=True)
            soc_cc = 0.0

        # === 2. Update Internal Resistance (forward temp + measured_dcir ให้ถูก) ===
        self.rin = self.battery_model.estimate_rin(
            voltage, cur, self.soc, temp=t_use, measured_dcir=measured_dcir
        )

        # === 3-EKF. 2-state EKF fusion (primary path) ===
        # The EKF fuses the coulomb prediction with the terminal-voltage measurement
        # using a covariance-weighted gain; dOCV/dSoC (its Jacobian) is naturally tiny
        # on a flat plateau, so it trusts CC there and OCV near the knees automatically.
        if self.use_ekf:
            ekf = self._ensure_ekf()
            s = self.battery_model.series_cells
            # SoC-dependent ECM: feed the RC dynamics (and R0 for the update) that match
            # the current SoC, so voltage prediction stays accurate toward empty.
            # R0 for the measurement update MUST be the *ohmic* resistance (ekf.R0, from
            # the extrapolated ECM fit) — NOT self.rin, which is a full DCIR that already
            # contains the RC polarisation. Using self.rin would double-count polarisation
            # against the EKF's own V_RC state and bias the voltage residual (worst at
            # high current), pulling SoC off. Fix: default to the ohmic ekf.R0.
            r0_use = ekf.R0
            if self.ecm_table is not None:
                r0s, r1s, c1s = self._ecm_at_soc(ekf.soc)
                ekf.set_rc(r0s, r1s, c1s)
                r0_use = r0s
            # ΔSoC from the SAME coulomb increment (η + Peukert + trapezoid + SoH-cap)
            # so Peukert/η actually affect the EKF output, not just the unused soc_cc.
            soc_delta = dah / cap * 100.0 if cap > 0 else 0.0
            ekf.predict(i_eff, dt, soc_delta)
            self.soc = max(0.0, min(100.0, ekf.soc))
            if self.use_ocv:
                # direction for OCV hysteresis: discharge (cur>0)→−1, charge→+1, rest→0
                direction = 0 if abs(cur) < 0.05 else (-1 if cur > 0 else 1)
                ocv_pack = self.battery_model.get_ocv_from_soc(ekf.soc, t_use, direction)
                docv = self.battery_model.ocv_slope(ekf.soc, t_use) * s   # V/%SoC pack
                # Still settling from the last 100%/0% anchor (surface charge / fresh
                # polarisation) — inflate the measurement variance so this update barely
                # moves SoC, instead of the filter reading the settling transient as a
                # real SoC error. See _reset_to_soc's start_settle_window.
                still_settling = time.monotonic() < self._anchor_settle_until
                r_override = ekf.R * self._anchor_settle_r_mult if still_settling else None
                ekf.update(voltage, cur, ocv_pack, docv, r0_use, r_override=r_override)
                self.soc = max(0.0, min(100.0, ekf.soc))
            self.soc_filtered = self.soc
            # live 1σ SoC uncertainty from the filter covariance (large mid-plateau /
            # early, shrinks after an OCV/endpoint anchor) — lets the UI show ±%.
            self.soc_std = float(max(0.0, float(ekf.P[0, 0])) ** 0.5)
            # Live internal resistance = the IDENTIFIED DC resistance R0+R1 (ohmic +
            # polarisation, SoC-dependent via the ECM table, and stable) rescaled by the
            # Arrhenius temperature multiplier — so it is SoC-aware, temperature-aware,
            # AND stable, instead of the noisy per-sample (OCV−V)/I from estimate_rin.
            temp_mult = self.battery_model.temp_rin_multiplier(t_use) if self.use_temp else 1.0
            self.rin = (ekf.R0 + ekf.R1) * temp_mult
            return {
                "soc": self.soc,
                "soc_std": self.soc_std,
                "soh": self.soh,
                "rin": self.rin,
                "rin_calibrated": self._ecm_calibrated,
                "ah_accumulated": self.ah_accumulated,
            }

        # === 3. OCV-Based Correction + ENDPOINT RESET (เมื่อกระแสน้อย) — fallback ===
        # หลักการ (จาก literature ของ LFP): coulomb counting drift ได้ → ต้อง re-anchor
        # ด้วย OCV "เฉพาะตรงที่ OCV เชื่อถือได้" คือบริเวณ knee/ปลาย (slope ชัน) หลัง full
        # charge / full discharge. ตรง plateau ที่ flat (slope ต่ำ) ห้ามแก้ (V คลาดนิด SoC
        # เพี้ยนมาก). ปลายที่ steep → anchor ทันที (ไม่รอ 300s) และ re-anchor coulomb counter.
        now = time.monotonic()   # duration check only — see the comment in __init__
        if self.use_ocv and abs(cur - self.standby_current) < self.static_current_threshold:
            self._rested_s += dt
            self.last_static_voltage = voltage
            ocv_voltage = voltage + self.standby_current * self.rin
            ocv_soc = self.battery_model.get_soc_from_ocv(ocv_voltage, t_use)
            slope = self.battery_model.ocv_slope(ocv_soc, t_use)
            drift = abs(self.soc_filtered - ocv_soc)
            steep = slope >= 2.0 * self.min_ocv_slope          # ปลาย/knee ที่ OCV เชื่อได้มาก
            periodic = (now - self.last_ocv_correction_time) >= self.ocv_correction_interval
            # เงื่อนไข: พักนานพอ (กัน transient) + ไม่ใช่ plateau แบน + (อยู่ปลาย หรือ ถึงรอบ+drift)
            if (self._rested_s >= self._min_rest_s and slope >= self.min_ocv_slope
                    and (steep or (periodic and drift > 3.0))):
                w = 0.9 if steep else 0.8                       # ปลาย anchor หนักกว่า
                corrected = w * ocv_soc + (1.0 - w) * soc_cc
                logger.info("OCV %s: CC=%.1f%% OCV=%.1f%% slope=%.4f -> %.1f%%",
                            "endpoint-reset" if steep else "correction",
                            soc_cc, ocv_soc, slope, corrected)
                self.soc_filtered = corrected
                # re-anchor coulomb counting to the corrected SoC (ไม่งั้น smoothing ดึงกลับ)
                self.soc_initial = corrected
                self.ah_accumulated = 0.0
                soc_cc = corrected
                self.last_ocv_correction_time = now
                self._rested_s = 0.0
        else:
            self.last_static_voltage = None
            self._rested_s = 0.0

        # === 4. Exponential Smoothing ===
        self.soc_filtered = (1 - self.alpha) * self.soc_filtered + self.alpha * soc_cc
        self.soc = max(0.0, min(100.0, self.soc_filtered))

        return {
            "soc": self.soc,
            "soh": self.soh,
            "rin": self.rin,
            "rin_calibrated": True,   # non-EKF fallback path doesn't use the uncalibrated
                                      # EKF-default mechanism this flag is about
            "ah_accumulated": self.ah_accumulated
        }

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