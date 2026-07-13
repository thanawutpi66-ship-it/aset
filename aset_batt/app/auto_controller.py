import threading
import time
import logging
from typing import Optional, Dict, Any

from aset_batt.services.service_locator import ServiceLocator
from aset_batt.services.event_system import EventType, UIEventHandler
from aset_batt.services.exceptions import SafetyError, HardwareError

logger = logging.getLogger(__name__)

class AutoController:
    """Advanced controller for battery testing operations"""

    # G8 (industrial-grade audit): sustained-staleness escalation threshold — see
    # _monitor_loop's temp_is_stale() handling below. Deliberately much larger than
    # HardwareController.temp_is_stale()'s own 10s default so a momentary serial
    # glitch only warns, not trips.
    _TEMP_STALE_TRIP_S = 60.0
    _LOAD_NOISE_FLOOR_A = 0.02
    _DEFAULT_MAX_TEMP_C = 60.0
    _DEFAULT_MAX_CURRENT_A = 30.0
    _DEFAULT_MONITOR_DT = 0.1

    def __init__(self, root: Any, hw: Any, data: Any, estimator: Any, config: Any):
        self.root = root
        self.hw = hw
        self.data = data
        self.estimator = estimator
        self.config = config
        self.ui = None  # จะถูกเซ็ตจาก main.py

        # System States
        self.monitor_running = False
        self.live_readback_running = False   # lightweight pre-test Connect readback
        self.is_charging = False
        self.safety_triggered = False
        self._charge_ctrl = None
        self._shutdown_done = False   # กัน shutdown ทำงานซ้ำ (idempotent)
        self._skip_ocv_reset = False  # set by stop_charge() เพื่อข้าม OCV reset

        # เวลาเริ่มต้นสำหรับคำนวณ elapsed time ใน CSV — เก็บคู่ wall-clock
        # (_start_time, ไว้อ้างอิง/แสดงผล) กับ monotonic (_start_mono, ไว้คำนวณ
        # Elapsed_s จริง): เดิม Elapsed_s = time.time()-_start_time ล้วนๆ ซึ่ง
        # NTP/DST jump กลางเทสหลายชั่วโมงทำแกนเวลาใน CSV เพี้ยน/ถอยหลังได้ —
        # ขัดกับ convention ของโปรเจกต์เอง (dt ทุกจุดใช้ perf_counter แล้ว)
        self._start_time = None
        self._start_mono = None
        self._last_update_time = None  # ใช้คำนวณ dt จริงต่อรอบ (coulomb counting)
        self._temp_stale_warned = False  # one-shot guard for the stale-ESP32-temp alarm

        # Get event handler from service locator (registered after UI bootstrap)
        self.event_handler = None

        self.event_handler = None
        
        # Crash recovery persistence
        self._recovery_file = "recovery.json"

        logger.info("AutoController initialized")

    def save_recovery_state(self, state: Dict[str, Any]):
        """Persist current execution state to disk for crash recovery"""
        try:
            import json
            import os
            
            # Merge with existing state if any
            current_state = {}
            if os.path.exists(self._recovery_file):
                try:
                    with open(self._recovery_file, "r") as f:
                        current_state = json.load(f)
                except Exception:
                    pass
                    
            current_state.update(state)
            current_state["last_updated"] = time.time()
            
            with open(self._recovery_file, "w") as f:
                json.dump(current_state, f, indent=2)
            logger.debug("Recovery state saved: %s", state)
        except Exception as e:
            logger.error("Failed to save recovery state: %s", e)
            
    def clear_recovery_state(self):
        """Remove recovery state file after clean shutdown/completion"""
        import os
        try:
            if os.path.exists(self._recovery_file):
                os.remove(self._recovery_file)
                logger.debug("Recovery state cleared")
        except Exception as e:
            logger.error("Failed to clear recovery state: %s", e)

    def _raise_if_otp_tripped(self, where: str) -> None:
        """OTP guard for long blocking waits that sit OUTSIDE _monitor_loop and
        outside the sequence-level _seq_check_otp() (calibrate_from_ocv_stable's
        settle wait — up to 15 min, used by every sequence's PREPARE phase and
        HPPC Full Sequence's post-charge rest — see G9). Fires the SAME big-banner
        + hardware-cut path a live E-STOP does (_trigger_safety), then raises so
        the caller's own thread unwinds through its existing safety cleanup."""
        temp_now = self.hw.current_temp
        otp_limit = self.config.system.safety_limits.get(
            "max_temperature", self._DEFAULT_MAX_TEMP_C)
        if temp_now is not None and temp_now > otp_limit:
            reason = f"OTP triggered {where}: {temp_now:.1f}°C > {otp_limit:.0f}°C"
            self._trigger_safety(reason)
            raise SafetyError(reason)

    def check_safety_limits(self, voltage: float, current: float, temperature: float) -> bool:
        """Check if parameters are within safety limits"""
        limits = self.config.system.safety_limits

        # During charging the ChargeController owns voltage cutoff — skip all
        # voltage checks so that a deeply-discharged battery (any starting V)
        # or a high PSU setpoint does not kill the charge loop prematurely.
        if not self.is_charging:
            if voltage > limits["max_voltage"]:
                self._trigger_safety(f"Voltage {voltage:.2f}V exceeds limit {limits['max_voltage']}V")
                return False
            if voltage < limits["min_voltage"]:
                self._trigger_safety(f"Voltage {voltage:.2f}V below limit {limits['min_voltage']}V")
                return False

        if abs(current) > limits["max_current"]:
            self._trigger_safety(f"Current {current:.2f}A exceeds limit {limits['max_current']}A")
            return False

        if temperature > limits["max_temperature"]:
            self._trigger_safety(f"Temperature {temperature:.1f}°C exceeds limit {limits['max_temperature']}°C")
            return False

        if temperature < limits["min_temperature"]:
            self._trigger_safety(f"Temperature {temperature:.1f}°C below limit {limits['min_temperature']}°C")
            return False

        return True

    def _trigger_safety(self, reason: str):
        """Trigger safety shutdown"""
        logger.error(f"SAFETY TRIGGERED: {reason}")
        self.safety_triggered = True

        # Post safety event
        if self.event_handler:
            self.event_handler.post_event(EventType.SAFETY_TRIGGERED, reason)

        # Emergency shutdown
        self._emergency_shutdown()

    def _emergency_shutdown(self):
        """Emergency shutdown of all systems"""
        try:
            self.hw.load_off()
            self.hw.psu_off()   # also cuts the SSR relay (GPIO16) — see HardwareController.psu_off
            logger.info("Emergency shutdown completed")
        except Exception as e:
            logger.error(f"Error during emergency shutdown: {e}")

    def set_ui(self, ui_instance):
        self.ui = ui_instance
        try:
            self.event_handler = ServiceLocator.get(UIEventHandler)
        except ValueError:
            self.event_handler = None

    # ------------------------------------------------------------------
    # Monitor Loop
    # ------------------------------------------------------------------

    def start_monitor(self, reuse_session: bool = False):
        """เริ่มลูปอ่านค่าจาก Hardware

        reuse_session=True: เรียกจากกลางเซสชันที่ sequence เปิด CSV ไว้แล้วตั้งแต่
        PREPARE (_ensure_logging(label="HPPC") ฯลฯ) — ต้อง "สานต่อ" ไฟล์เดิม ไม่ใช่
        เปิดใหม่ (เดิมสร้างไฟล์ใหม่ไม่มีเงื่อนไขทุกครั้ง ทำให้ label+OCV-settle
        หลายนาทีจาก PREPARE โดนทอดทิ้ง — ตรงกับหลักฐานใน test_20260708_152502.csv).

        reuse_session=False (ค่าเริ่มต้น — ใช้เมื่อผู้ใช้กดปุ่มมือ เช่น Start
        Charge/Start Test นอก sequence): ปิด session ค้าง (ถ้ามี) แล้วเปิดไฟล์ใหม่
        เสมอ ไม่งั้น is_recording ที่ไม่เคยถูกเคลียร์หลังเทสแรกจบ (Stop Charge ไม่ได้
        เรียก stop_logging()) จะทำให้เทสมือครั้งที่สองในแอปเดียวกันเงียบๆ ไปเขียน
        ทับ/ต่อท้ายไฟล์เทสแรกพร้อมนาฬิกา elapsed ที่ค้างจากรันแรก.
        """
        if not self.monitor_running:
            self.stop_live_readback()   # real monitor takes over V/I/temp display
            self._last_update_time = None
            self.monitor_running = True
            if reuse_session and self.data.is_recording:
                if self._start_time is None:
                    self._start_time = time.time()
                    self._start_mono = time.perf_counter()
            else:
                if self.data.is_recording:
                    self.data.stop_logging()
                self._start_time = time.time()
                self._start_mono = time.perf_counter()
                from aset_batt.storage.data_utils import DataHandler, write_session_metadata
                csv_path = DataHandler.make_session_path()
                ok, msg = self.data.start_logging(csv_path)
                if not ok:
                    import logging
                    logging.getLogger(__name__).error(f"Cannot start CSV logging: {msg}")
                else:
                    write_session_metadata(csv_path, self.config)   # R3: audit trail
            threading.Thread(target=self._monitor_loop, daemon=True).start()

    def stop_monitor(self):
        """Stop the hardware monitoring loop"""
        if self.monitor_running:
            self.monitor_running = False
            logger.info("Monitor loop stopped by user")

    def end_session(self):
        """ปิด CSV session ปัจจุบันอย่างชัดเจน (ให้ workflow ถัดไปเริ่ม session ใหม่
        แน่ๆ) — เรียกตอนจบ/ยกเลิก auto-sequence เท่านั้น. ไม่เรียกจาก stop_charge()/
        stop_monitor() ธรรมดา เพราะสองอันนั้นถูกเรียกกลาง sequence ระหว่างเปลี่ยน
        phase ด้วย (อยากให้ session เดิมยังอยู่); ไม่มีจุดนี้ is_recording จะค้าง True
        ตลอดไปหลัง sequence จบ ทำให้ sequence รอบถัดไปเผลอต่อท้ายไฟล์เดิม."""
        if self.data.is_recording:
            self.data.stop_logging()
        self._start_time = None
        self._start_mono = None
        self.clear_recovery_state()

    # ------------------------------------------------------------------
    # Live readback — lightweight V/I/Temp display right after Connect, before
    # any test is running. No CSV logging, no state estimator (SoC/Rin need it).
    # Stops itself automatically once a real test starts start_monitor().
    # ------------------------------------------------------------------

    def start_live_readback(self):
        if self.live_readback_running or self.monitor_running:
            return
        self.live_readback_running = True
        threading.Thread(target=self._live_readback_loop, daemon=True).start()

    def stop_live_readback(self):
        self.live_readback_running = False

    def _live_readback_loop(self):
        while self.live_readback_running and not self.monitor_running:
            if self.hw.is_connected:
                try:
                    v, psu_i, load_i = self.hw.read_vi()
                    if load_i > self._LOAD_NOISE_FLOOR_A:
                        i_net = load_i
                    elif getattr(self.hw, "_psu_output_on", False):
                        i_net = -psu_i
                    else:
                        i_net = psu_i
                    if self.ui and self.root:
                        self.root.after(0, self.ui.update_live_readback,
                                         v, i_net, self.hw.current_temp)
                except Exception as e:
                    logger.debug("Live readback read failed: %s", e)
            time.sleep(1.0)
        self.live_readback_running = False

    def calibrate_from_ocv(self):
        """Calibrate SoC from OCV reading when battery is rested"""
        if not self.hw.is_connected:
            raise HardwareError("Hardware must be connected to calibrate from OCV")

        v, psu_i, load_i = self.hw.read_vi()
        ocv_temp = self.hw.current_temp
        soc = self.estimator.sync_with_ocv(v, ocv_temp)
        logger.info(f"Calibrated SoC from OCV: {v:.3f}V @ {ocv_temp:.1f}°C -> {soc:.1f}%")
        return soc

    # Settling parameters per chemistry  (min_rest s, window s, ΔV threshold V)
    # LiFePO4: plateau ราบมาก (1.4 mV/cell/%SoC) — หลัง discharge หนัก electrochemical
    # relaxation ใช้เวลา 5-30 นาที; 120s+5mV ตรวจ "หยุดขึ้นเร็ว" แต่ยังไม่ settle จริง
    # ปรับเป็น 300s minimum + 2mV/60s เพื่อให้แม่นขึ้น (~5 นาที practical minimum)
    _OCV_SETTLE = {
        "LeadAcid": (300,  60, 0.010),
        "LiFePO4":  (300,  60, 0.002),   # เดิม (120, 30, 0.005) — เพิ่ม rest + เข้มขึ้น
        "Li-ion":   ( 60,  30, 0.005),
        "LiPO":     ( 60,  30, 0.005),
    }

    # Lead-acid surface-charge bleed-off: standard practice (Battery University
    # BU-903, IEEE 450 guidance on stationary lead-acid maintenance testing) is to
    # apply a brief, moderate discharge rather than wait hours for the surface
    # charge layer to passively diffuse into the bulk electrolyte. Commonly cited
    # range is C/20-C/10 for 5-10 minutes; C/20 for 5 minutes is the conservative
    # end (~0.4% of rated capacity removed) — verify on the bench against a
    # reference SG/OCV reading before trusting this on a specific product, same
    # as scripts/self_calibration_test.py's own note for harness calibration.
    _SURFACE_CHARGE_BLEED_C_RATE = 0.05        # C/20
    _SURFACE_CHARGE_BLEED_DURATION_S = 300.0   # 5 min
    _SURFACE_CHARGE_BLEED_POLL_S = 2.0
    # Headroom above the pack's hard discharge cutoff — this is a courtesy
    # accuracy step, not the actual test, so it must back off well before UVP
    # even for a pack that turns out weaker than the "reads as ≥100%" OCV implied.
    _SURFACE_CHARGE_BLEED_SAFETY_MARGIN = 1.05

    def calibrate_from_ocv_stable(self, on_progress=None, cancel_check=None,
                                  _allow_bleed_off=True):
        """OCV calibration แบบ wait-for-settle ตามมาตรฐาน ΔV/Δt criterion.

        อ่านแรงดันทุก 5 วิ จนกว่าจะผ่านทั้งสองเงื่อนไข:
          1. ผ่านเวลาพักขั้นต่ำของแต่ละเคมี
          2. max(V) - min(V) < dv_thresh ในช่วงเวลา window

        on_progress(elapsed_s, voltage, dv_mv, status)
          status: "waiting" | "checking" | "settled" | "timeout"

        cancel_check: callable() → bool; คืน True เมื่อ sequence ยังทำงานอยู่
          (เช่น self._seq_running.is_set). ถ้าคืน False → หยุดรอทันที

        คืน (soc, voltage, status)
        """
        import time as _t
        if not self.hw.is_connected:
            raise HardwareError("Hardware must be connected to calibrate from OCV")

        chemistry = getattr(self.config.battery, "battery_type", "LiPO")
        min_rest, window, dv_thresh = self._OCV_SETTLE.get(
            chemistry, self._OCV_SETTLE["LiPO"]
        )
        timeout   = max(min_rest * 4, 900)   # สูงสุด 15 นาที
        interval  = 5.0                       # อ่านทุก 5 วิ

        readings  = []   # [(timestamp, voltage), ...]
        t_start   = _t.time()
        settled   = False

        while True:
            if cancel_check is not None and not cancel_check():
                break

            elapsed = _t.time() - t_start
            if elapsed > timeout:
                break

            try:
                v, _, _ = self.hw.read_vi()
            except Exception as exc:
                raise HardwareError(f"OCV read failed: {exc}")

            self._raise_if_otp_tripped("during OCV settle")

            now = _t.time()
            readings.append((now, v))

            # เก็บเฉพาะ readings ใน window
            cutoff  = now - window
            readings = [(t, val) for t, val in readings if t >= cutoff]
            in_win  = [val for _, val in readings]
            dv      = (max(in_win) - min(in_win)) if len(in_win) >= 2 else float("nan")

            if elapsed < min_rest:
                status = "waiting"
            elif len(in_win) < 3 or dv != dv or dv >= dv_thresh:
                status = "checking"
            else:
                settled = True
                status  = "settled"

            if on_progress:
                on_progress(elapsed, v, dv * 1000 if dv == dv else float("nan"), status)

            if settled:
                break

            # sleep แบบ interruptible (ทุก 0.5s ตรวจ is_connected + cancel_check + OTP)
            t_end = _t.time() + interval
            while _t.time() < t_end:
                if not self.hw.is_connected:
                    raise HardwareError("Hardware disconnected during OCV settle")
                if cancel_check is not None and not cancel_check():
                    break
                self._raise_if_otp_tripped("during OCV settle")
                _t.sleep(0.5)

        # อ่านค่าสุดท้าย + sync estimator
        v_final, _, _ = self.hw.read_vi()
        temp_final    = self.hw.current_temp
        soc = self.estimator.sync_with_ocv(v_final, temp_final)
        final_status  = "settled" if settled else "timeout"
        logger.info(
            "OCV stable: %.3fV @ %.1f°C → SoC %.1f%% (%s, elapsed %.0fs)",
            v_final, temp_final, soc, final_status, _t.time() - t_start,
        )
        # Surface-charge / not-actually-at-equilibrium check — see
        # BatteryModel.ocv_out_of_range_mv's docstring. This settle window is
        # tuned for coulomb-counting drift (seconds-to-minutes), not for lead-acid
        # surface charge (hours) — a reading outside the curve's own calibrated
        # range is flat/stable within the window without being at true rest.
        oor_mv = self.estimator.battery_model.ocv_out_of_range_mv(v_final, temp_final)
        if oor_mv != 0.0:
            msg = (f"OCV {v_final:.3f}V is {abs(oor_mv):.0f} mV "
                   f"{'above the 100%' if oor_mv > 0 else 'below the 0%'} point of the "
                   f"calibrated curve — likely still settling (surface charge / fresh "
                   f"polarisation), not a reliable rested reading despite passing the "
                   f"{'settled' if settled else 'timeout'} ΔV/Δt check")
            logger.warning(msg)
            if self.event_handler:
                self.event_handler.post_event(
                    EventType.SHOW_MESSAGE, ("OCV Out of Range", msg, "warning"))
            # Only bleed off for ABOVE-range (surface charge from a recent charge) —
            # BELOW-range means the coulomb/OCV model already reads this pack as
            # near-empty, and pulling MORE current out of a possibly genuinely
            # depleted pack to "fix" that reading would be actively unsafe, not
            # helpful. One attempt only (_allow_bleed_off guards the recursive
            # re-check) — a pack still out of range after a real bleed-off is a
            # genuine anomaly to surface, not something to keep retrying.
            from aset_batt.core import battery_profiles
            chem_name = battery_profiles.get_chemistry(chemistry).name
            if oor_mv > 0.0 and _allow_bleed_off and chem_name == "LeadAcid":
                if self._bleed_off_surface_charge(on_progress=on_progress,
                                                  cancel_check=cancel_check):
                    return self.calibrate_from_ocv_stable(
                        on_progress=on_progress, cancel_check=cancel_check,
                        _allow_bleed_off=False)
        if on_progress:
            on_progress(_t.time() - t_start, v_final, 0.0, final_status)
        return soc, v_final, final_status

    def _bleed_off_surface_charge(self, on_progress=None, cancel_check=None) -> bool:
        """Apply a brief C/20 discharge to strip lead-acid surface charge (see the
        constants above calibrate_from_ocv_stable) instead of waiting hours for it
        to passively dissipate. Returns True if the bleed ran (to completion or a
        safe early stop on low voltage) so the caller should re-settle and
        re-check; False if hardware failed outright or was cancelled, in which
        case the caller should just return the still-flagged original reading.
        """
        import time as _t
        rated = self.config.battery.rated_capacity
        i_bleed = min(max(0.05, rated * self._SURFACE_CHARGE_BLEED_C_RATE),
                     self.config.battery.max_current)
        safety_floor = self.config.battery.pack_min_voltage * self._SURFACE_CHARGE_BLEED_SAFETY_MARGIN
        logger.info("Surface-charge bleed-off: %.3fA for %.0fs (safety floor %.3fV)",
                    i_bleed, self._SURFACE_CHARGE_BLEED_DURATION_S, safety_floor)
        if on_progress:
            on_progress(0.0, 0.0, float("nan"), "bleeding")
        ok = True
        try:
            if not self.hw.set_load(True, i_bleed):
                return False
            t0 = _t.time()
            while _t.time() - t0 < self._SURFACE_CHARGE_BLEED_DURATION_S:
                if cancel_check is not None and not cancel_check():
                    ok = False
                    break
                try:
                    v, i = self.hw.read_measurements(prefer_load_v=True)
                except Exception as exc:
                    logger.error("Surface-charge bleed-off read failed: %s", exc)
                    ok = False
                    break
                self._log_sample(v, i)
                if v <= safety_floor:
                    logger.warning(
                        "Surface-charge bleed-off stopped early: %.3fV ≤ safety floor %.3fV",
                        v, safety_floor)
                    break
                if on_progress:
                    on_progress(_t.time() - t0, v, float("nan"), "bleeding")
                t_end = _t.time() + self._SURFACE_CHARGE_BLEED_POLL_S
                while _t.time() < t_end:
                    if cancel_check is not None and not cancel_check():
                        ok = False
                        break
                    _t.sleep(0.2)
                if not ok:
                    break
        finally:
            self.hw.load_off()
        return ok

    # Consecutive read failures tolerated before the monitor loop gives up for real.
    # A single VISA/USB hiccup (timeout, transient bus reset) used to kill the whole
    # loop on the very first exception — and monitor_running was never reset to False,
    # so start_monitor()'s "if not running" guard meant the operator could never
    # restart it either (looked exactly like the program had frozen).
    _MONITOR_MAX_CONSEC_ERRORS = 5
    _MONITOR_TARGET_PERIOD_S = 0.1   # nominal 10 Hz

    def _monitor_loop(self):
        """ลูปอ่าน Voltage, Current และอัปเดต SoC/UI"""
        consec_errors = 0
        while self.monitor_running:
            loop_t0 = time.perf_counter()
            if self.hw.is_connected:
                try:
                    # อ่านค่าจาก Hardware
                    # Convention: discharge = บวก (ให้ตรงกับ StateEstimator,
                    # CSV/dashboard และ generate_sample_data)
                    #
                    # The SSR relay (ESP32 GPIO16) physically disconnects the PSU from
                    # the battery whenever not charging, so REST/idle reads ~0 A directly
                    # — no bleed compensation needed:
                    #   CHARGE    → OUTPUT ON, SSR ON;  i_net = -psu_i          (negative)
                    #   DISCHARGE → PSU disconnected;   i_net = load_i          (positive)
                    #   REST      → OUTPUT OFF, SSR OFF; i_net ≈ 0             (positive)
                    #
                    v, psu_i, load_i = self.hw.read_vi()
                    # Stamp the timestamp AT the measurement (not after the safety
                    # checks below) using perf_counter (monotonic, sub-ms) rather than
                    # time.time() (wall-clock, vulnerable to NTP/clock-jump corrupting
                    # coulomb counting with a negative or oversized dt).
                    now = time.perf_counter()
                    if load_i > self._LOAD_NOISE_FLOOR_A:
                        # Discharge via e-load: battery → load (positive by convention).
                        i_net = load_i
                    elif getattr(self.hw, "_psu_output_on", False):
                        # PSU OUTPUT ON → PSU is the source (charging).
                        # Convention: charge = negative.
                        i_net = -psu_i
                    else:
                        # PSU OUTPUT OFF, SSR OFF → PSU physically disconnected (REST).
                        # Convention: discharge = positive. psu_i ≈ 0.
                        i_net = psu_i

                    # Monitor loop only checks temperature & overcurrent —
                    # voltage OVP/UVP is handled by the discharge test loop and
                    # ChargeController so we don't kill live monitoring on a
                    # low-voltage battery that is being charged.
                    temp = self.hw.current_temp
                    # current_temp has no timestamp of its own — a serial glitch or a
                    # hung ESP32 would leave it silently frozen at an old value with
                    # nothing to distinguish it from a live reading, so the OTP check
                    # below (and Rin/OCV temperature compensation) would keep trusting
                    # a stale number. Warn once per stale episode; don't hard-stop the
                    # test on this alone (a false trip here would be its own hazard).
                    if getattr(self.hw, "temp_is_stale", None) and \
                            self.hw.temp_is_stale(self._TEMP_STALE_TRIP_S):
                        # G8 (industrial-grade audit): a brief staleness blip only
                        # warns (see below) — a hard stop on that alone would be its
                        # own false-trip hazard, per the original reasoning here.
                        # But if the sensor has been dead for a SUSTAINED period, OTP
                        # protection has been genuinely blind for real time, not a
                        # momentary glitch — that's now treated as an actual safety
                        # trip like any other breach, not left running forever.
                        self._trigger_safety(
                            f"ESP32 temperature reading stale for {self._TEMP_STALE_TRIP_S:.0f}s+ "
                            f"— over-temperature protection is blind, stopping test")
                        break
                    if getattr(self.hw, "temp_is_stale", None) and self.hw.temp_is_stale():
                        if not self._temp_stale_warned:
                            self._temp_stale_warned = True
                            logger.warning("ESP32 temperature reading is stale — "
                                          "OTP safety check may be blind to real temperature")
                            if self.event_handler:
                                self.event_handler.post_event(
                                    EventType.SHOW_MESSAGE,
                                    ("Temperature Sensor",
                                     "ESP32 temperature reading is stale (no update recently) — "
                                     "over-temperature protection may not reflect the real battery "
                                     "temperature until it reconnects.", "warning")
                                )
                    else:
                        self._temp_stale_warned = False
                    limits = self.config.system.safety_limits
                    if temp > limits.get("max_temperature", self._DEFAULT_MAX_TEMP_C):
                        self._trigger_safety(
                            f"Temperature {temp:.1f}°C exceeds limit {limits['max_temperature']}°C")
                        break
                    if abs(i_net) > limits.get("max_current", self._DEFAULT_MAX_CURRENT_A):
                        self._trigger_safety(
                            f"Current {i_net:.2f}A exceeds limit {limits['max_current']}A")
                        break

                    # อัปเดต State Estimator ด้วย dt จริงต่อรอบ
                    # `now` was already stamped right after read_vi() above.
                    dt = (now - self._last_update_time) if self._last_update_time else self._DEFAULT_MONITOR_DT
                    self._last_update_time = now
                    state = self.estimator.update(
                        v, i_net, dt=dt, temp=self.hw.current_temp
                    )

                    # ส่งค่าไปอัปเดต UI (thread-safe ผ่าน root.after)
                    if self.ui and self.root:
                        # Capture the generation NOW, on this thread, before
                        # root.after defers the actual update_display() call
                        # to run later on the GUI thread — by the time it
                        # fires, self.ui._run_generation may have already
                        # moved on to a new test/sequence, and update_display
                        # reading it fresh at that point would defeat the
                        # staleness check entirely. See _slot_display.
                        _gen = getattr(self.ui, "_run_generation", 0)
                        self.root.after(
                            0,
                            self.ui.update_display,
                            v,
                            i_net,
                            state["soc"],
                            state["rin"],
                            self.hw.current_temp,
                            state["soh"],
                            _gen,
                        )

                    # คำนวณ elapsed seconds จากเวลาเริ่มต้น (monotonic — ดู _start_mono)
                    elapsed = (time.perf_counter() - self._start_mono
                               if self._start_mono is not None
                               else time.time() - self._start_time)
                    self.data.log_row(
                        elapsed, v, i_net,
                        state['soc'], state['rin'] * 1000,  # แปลงเป็น mOhm
                        self.hw.current_temp,
                        rin_calibrated=state.get('rin_calibrated', True),
                    )
                    consec_errors = 0   # a clean read resets the retry budget
                except SafetyError as e:
                    # A genuine safety trip — never retry this one.
                    logger.error(f"Safety error in monitor loop: {e}")
                    self._stop_monitor_fatally(f"Safety error: {e}")
                    break
                except Exception as e:
                    # Catches HardwareError too (ASETError -> Exception); SafetyError
                    # was already matched and handled by the clause above.
                    consec_errors += 1
                    if consec_errors < self._MONITOR_MAX_CONSEC_ERRORS:
                        # Likely transient (USB glitch, VISA timeout) — retry with a
                        # short backoff instead of killing live monitoring outright.
                        logger.warning(
                            "Monitor loop read error (%d/%d, retrying): %s",
                            consec_errors, self._MONITOR_MAX_CONSEC_ERRORS, e)
                        time.sleep(min(2.0, 0.3 * consec_errors))
                        continue
                    logger.error(
                        f"Monitor loop: {consec_errors} consecutive errors, giving up: {e}",
                        exc_info=True)
                    self._stop_monitor_fatally(
                        f"Lost connection to hardware after {consec_errors} "
                        f"consecutive read errors: {e}")
                    break
            # Sleep only the time REMAINING to hit the target period. SCPI round-trip
            # latency (~40-200 ms) already eats into the 100 ms budget; always sleeping
            # a fixed 0.1 s on top of that (instead of topping up to it) needlessly
            # halves the achievable sample rate. dt itself is measured correctly
            # either way (perf_counter deltas around the actual read), so this only
            # affects throughput/sample density — useful for not under-sampling fast
            # transients (e.g. an HPPC pulse edge) — not dt correctness.
            elapsed_this_iter = time.perf_counter() - loop_t0
            time.sleep(max(0.0, self._MONITOR_TARGET_PERIOD_S - elapsed_this_iter))

    def _stop_monitor_fatally(self, reason: str):
        """Cleanly end the monitor loop and make it restartable. Without resetting
        monitor_running here, start_monitor()'s "if not running" guard would silently
        no-op forever after any unrecoverable error — the operator has no way to
        recover monitoring short of restarting the whole application."""
        self.monitor_running = False
        if self.event_handler:
            self.event_handler.post_event(
                EventType.SHOW_MESSAGE,
                ("Monitor Stopped", reason, "error")
            )
    # Chemistry-aware Charging (3-stage lead-acid / CC-CV lithium)
    # ------------------------------------------------------------------

    def start_charge(self, float_hold_s: float = 0.0, strategy: str = None,
                     bulk_c_rate_override: float = None, reuse_session: bool = False):
        """เริ่มชาร์จ; strategy=None → เลือกตามเคมีของแบตอัตโนมัติ
        (LeadAcid → 3-stage, Lithium → CC-CV). ส่ง strategy เพื่อ override จาก dropdown:
        "three_stage" 또는 "cc_cv". રันใน thread แยก; monitor loop ยัง log+safety ระหว่างชาร์จ

        reuse_session: ส่งเป็น True เมื่อเรียกจากกลาง auto-sequence (session เปิดไว้
        แล้วตั้งแต่ PREPARE) — ดู start_monitor()'s docstring. ปุ่มมือ Start Charge
        ใช้ค่าเริ่มต้น False เสมอ (อยากได้ session ใหม่ทุกครั้งที่ผู้ใช้กดเอง)."""
        if self.is_charging:
            logger.info("Charge already running")
            return False
        if not self.hw.is_connected:
            logger.error("Cannot charge: hardware not connected")
            return False
        if self.safety_triggered:
            # Always allow charge to proceed regardless of starting voltage —
            # deeply-discharged batteries need charging even at near-zero volts.
            v_now = self.hw.read_vi()[0] if self.hw.is_connected else 0.0
            logger.info("Auto-clearing safety for charge recovery (%.2fV)", v_now)
            self.safety_triggered = False
        if self.estimator is None or self.estimator.battery_model is None:
            logger.error("Cannot charge: battery model unavailable")
            return False

        self.is_charging = True
        if not self.monitor_running:
            self.start_monitor(reuse_session=reuse_session)
        threading.Thread(target=self._run_charge_loop,
                         args=(float_hold_s, strategy, bulk_c_rate_override), daemon=True).start()
        return True

    def _run_charge_loop(self, float_hold_s: float, strategy: str = None,
                         bulk_c_rate_override: float = None):
        from aset_batt.core.charge_controller import ChargeController
        logger.info("Charge loop started (strategy=%s)", strategy or "auto")
        try:
            # ปิด load ก่อนชาร์จ (กันชาร์จ-ดิสชาร์จพร้อมกัน)
            self.hw.load_off()

            # NOTE: no pre-charge OCV sync here.
            # When called from AUTO SEQUENCE the PREPARE phase already waited the
            # chemistry-aware minimum rest and called calibrate_from_ocv(), so
            # soc_initial is already correct.  A 2 s sync here would OVERWRITE that
            # anchor with a still-polarised voltage and re-introduce the same bug.

            self._charge_ctrl = ChargeController(
                self.hw, self.config, self.estimator.battery_model,
                on_update=self._on_charge_update, strategy=strategy,
                bulk_c_rate_override=bulk_c_rate_override,
            )
            final_stage = self._charge_ctrl.run(
                should_stop=lambda: (self.safety_triggered or not self.is_charging),
                float_hold_s=float_hold_s,
            )
            logger.info(f"Charge loop finished at stage: {final_stage}")
        except Exception as e:
            logger.error(f"Charge loop error: {e}")
        finally:
            self.is_charging = False
            self._charge_ctrl = None
            if not self._skip_ocv_reset:
                self._ocv_reset_after_rest("charge")
            self._skip_ocv_reset = False

    def _on_charge_update(self, stage: str, voltage: float, i_charge: float, note: str):
        """callback จาก ChargeController — อัปเดต UI ผ่าน root.after (thread-safe)"""
        if self.ui and self.root:
            txt = f"Charging [{stage}]: {voltage:.2f}V  {i_charge:.2f}A"
            setter = getattr(self.ui, "set_charge_status", None) or \
                getattr(self.ui, "set_profile_status", None)
            if setter is not None:
                self.root.after(0, setter, txt)
        self.save_recovery_state({"phase": "charge", "stage": stage, "voltage": voltage, "current": i_charge})

    def stop_charge(self):
        """หยุดชาร์จ + ปิด PSU"""
        if not self.is_charging:
            return
        logger.info("Stopping charge")
        self._skip_ocv_reset = True   # ข้าม OCV rest เมื่อหยุดกลางคัน
        self.is_charging = False
        if self._charge_ctrl is not None:
            self._charge_ctrl.stop()
        try:
            self.hw.psu_off()
        except Exception as e:
            logger.error(f"psu_off during stop_charge failed: {e}")

    # ------------------------------------------------------------------
    # Logging / auto-analysis helpers
    # ------------------------------------------------------------------
    def _ensure_logging(self, label: str = ""):
        """เปิด CSV logging + ตั้งเวลาเริ่ม ถ้ายังไม่ได้เปิด (ให้ IEC test โผล่บน dashboard)

        label (เช่น "HPPC", "QuickScan") ถูกฝังในชื่อไฟล์ session เพื่อบอกชนิดเทสต์."""
        if not self.data.is_recording:
            from aset_batt.storage.data_utils import DataHandler, write_session_metadata
            csv_path = DataHandler.make_session_path(label=label)
            ok, _ = self.data.start_logging(csv_path)
            if ok:
                write_session_metadata(csv_path, self.config)   # R3: audit trail
        if self._start_time is None:
            self._start_time = time.time()
            self._start_mono = time.perf_counter()

    def _log_sample(self, voltage: float, current: float):
        """log หนึ่งแถว ใช้ค่า SoC/Rin ล่าสุดจาก estimator (สำหรับ IEC test ที่ไม่ผ่าน monitor loop)

        Rin still logs live every sample even before any real HPPC pulse has been fitted
        (update_ecm()/set_ecm_table()) — the operator wants a continuous real-time trend,
        not a gap. But until then it's still just _ekf_rc_defaults()'s uncalibrated
        placeholder guess, not a measurement (an operator comparing it against a bench
        ACIR/DCIR meter kept mistaking the guess for a reading), so rin_calibrated rides
        along for the UI to label it "estimated" rather than presenting it as a reading."""
        try:
            calibrated = getattr(self.estimator, "_ecm_calibrated", True) or not getattr(
                self.estimator, "use_ekf", True)
            self.data.log_row(
                (time.perf_counter() - self._start_mono
                 if self._start_mono is not None else time.time() - self._start_time),
                voltage, current,
                self.estimator.soc, self.estimator.rin * 1000.0,
                self.hw.current_temp, rin_calibrated=calibrated,
            )
        except Exception as e:
            logger.debug("log_sample error: %s", e)

    def _auto_analyze(self, force_hppc: bool = False) -> dict | None:
        """รัน unified analysis (ECM/grade) บน CSV ล่าสุด แล้ว post ANALYSIS_COMPLETED -> UI.
        ใช้วิธีวิเคราะห์เดียวกับ characterization test และปุ่ม Analyze CSV (วิธีเดียวทั้งระบบ)
        Returns the result dict, or None on failure."""
        try:
            from aset_batt.acquisition.analysis import analyze_csv_mp, profile_from_config
            csv_path = self.data.current_path or self.config.system.csv_filepath
            res = analyze_csv_mp(csv_path, profile_from_config(self.config),
                                 force_hppc=force_hppc)
        except Exception as e:
            logger.warning("auto-analyze ล้มเหลว: %s", e)
            return None
        if self.event_handler:
            self.event_handler.post_event(EventType.ANALYSIS_COMPLETED, res)
        return res

    def _ocv_reset_after_rest(self, phase: str, rest_s: float = 30.0):
        """Wait for surface charge to relax then sync SoC from OCV.

        Called automatically after charge and discharge end so the SoC estimator
        is re-anchored to the true resting voltage rather than drifting on coulomb
        counting alone.  The 30-second rest is a compromise: enough for the RC
        relaxation to settle (τ₁ ≈ 5-15s for lead-acid) without blocking the UI
        thread (this runs in the charge/test background thread).
        """
        if self.estimator is None or not self.hw.is_connected:
            return
        try:
            logger.info("OCV reset: resting %.0fs after %s …", rest_s, phase)
            time.sleep(rest_s)
            v, _, _ = self.hw.read_vi()
            temp = self.hw.current_temp
            soc = self.estimator.sync_with_ocv(v, temp)
            logger.info("OCV reset after %s: %.3fV → SoC %.1f%%", phase, v, soc)
        except Exception as e:
            logger.warning("OCV reset after %s failed: %s", phase, e)

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self):
        """Graceful shutdown of all systems (idempotent — เรียกซ้ำได้ปลอดภัย:
        closeEvent ของ Qt เรียกตัดไฟทันที + bootstrapper.cleanup() เรียกอีกครั้ง)

        ลำดับสำคัญ: ตัด output ทันทีเป็นอย่างแรก (_emergency_shutdown → psu_off/
        load_off/SSR OFF) ก่อนไปยุ่งกับ flag/loop ใดๆ — เดิมตัดไฟเป็นขั้นตอนท้ายๆ
        ผ่าน hw.shutdown_all() เท่านั้น ถ้าอะไรก่อนหน้า raise หรือ write ล้มเหลว
        เครื่องจ่ายไฟจะค้าง ON ทั้งที่โปรแกรมปิดไปแล้ว"""
        if self._shutdown_done:
            logger.debug("Controller shutdown ทำไปแล้ว — ข้าม")
            return
        logger.info("Starting controller shutdown")

        # ตัดไฟก่อนเป็นอันดับแรก — ภายในกัน exception เองอยู่แล้ว
        self._emergency_shutdown()

        self.monitor_running = False
        self.is_charging = False
        try:
            if self._charge_ctrl is not None:
                self._charge_ctrl.stop()
        except Exception as e:
            logger.error(f"Error stopping charge controller: {e}")
        time.sleep(0.2)

        hw_ok = False
        try:
            self.hw.shutdown_all()
            hw_ok = True
            logger.info("Hardware shutdown completed")
        except Exception as e:
            logger.error(f"Error during hardware shutdown: {e}")
        # latch idempotency เฉพาะเมื่อขั้นตัดไฟ/ตัดการเชื่อมต่อสำเร็จจริง — ถ้า fail
        # ให้การเรียกซ้ำครั้งถัดไป (เช่น bootstrapper.cleanup) ได้ลองตัดไฟใหม่
        # แทนที่จะโดนข้ามเพราะ flag ถูกตั้งไว้ตั้งแต่บรรทัดแรกแบบเดิม
        self._shutdown_done = hw_ok

        try:
            self.data.stop_logging()
            logger.info("Data logging stopped")
        except Exception as e:
            logger.error(f"Error stopping data logging: {e}")

        logger.info("Controller shutdown completed")
