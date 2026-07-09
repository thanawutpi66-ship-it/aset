import threading
import time
import logging
from typing import Optional, Dict, Any

from aset_batt.services.service_locator import ServiceLocator
from aset_batt.services.event_system import EventType, UIEventHandler
from aset_batt.services.exceptions import SafetyError, HardwareError
from aset_batt.core.iec61960_standard import IEC61960Standard, TestType

logger = logging.getLogger(__name__)

class AutoController:
    """Advanced controller for battery testing operations"""

    # G8 (industrial-grade audit): sustained-staleness escalation threshold — see
    # _monitor_loop's temp_is_stale() handling below. Deliberately much larger than
    # HardwareController.temp_is_stale()'s own 10s default so a momentary serial
    # glitch only warns, not trips.
    _TEMP_STALE_TRIP_S = 60.0

    def __init__(self, root, hw, data, estimator, config):
        self.root = root
        self.hw = hw
        self.data = data
        self.estimator = estimator
        self.config = config
        self.ui = None  # จะถูกเซ็ตจาก main.py

        # System States
        self.monitor_running = False
        self.live_readback_running = False   # lightweight pre-test Connect readback
        self.is_profile_running = False
        self.is_charging = False
        self.safety_triggered = False
        self.profile_data = []
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

        logger.info("AutoController initialized")

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

    def start_monitor(self):
        """เริ่มลูปอ่านค่าจาก Hardware"""
        if not self.monitor_running:
            self.stop_live_readback()   # real monitor takes over V/I/temp display
            self._last_update_time = None
            self.monitor_running = True
            # เปิด session ใหม่เฉพาะเมื่อยังไม่มีการบันทึกอยู่ — เดิมสร้างไฟล์ใหม่
            # ทุกครั้งแบบไม่มีเงื่อนไข ทำให้ session ที่ sequence เปิดไว้ตั้งแต่
            # PREPARE (_ensure_logging(label="HPPC") ฯลฯ) โดนสลับทิ้งกลางคันตอน
            # start_charge() เรียก start_monitor(): ไฟล์ติด label พร้อมข้อมูล
            # OCV-settle หลายนาทีถูกทอดทิ้ง แล้วทุกอย่างไปลงไฟล์ใหม่ไร้ label ที่
            # เวลา elapsed ถูกรีเซ็ตเป็นศูนย์ — ตรงกับหลักฐานในไฟล์เทสจริง
            # (test_20260708_152502.csv: ไม่มี label HPPC, เริ่ม t=0 มี rest แค่
            # ~3 แถวก่อนกระแสชาร์จไหล ทั้งที่ PREPARE รอ OCV จริงหลายนาที) และคือ
            # ต้นเหตุที่ _quality_flags ฟ้อง "no clear rest before load" ทุกเทส.
            if not self.data.is_recording:
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
            elif self._start_time is None:
                self._start_time = time.time()
                self._start_mono = time.perf_counter()
            threading.Thread(target=self._monitor_loop, daemon=True).start()

    def stop_monitor(self):
        """Stop the hardware monitoring loop"""
        if self.monitor_running:
            self.monitor_running = False
            logger.info("Monitor loop stopped by user")

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
                    if load_i > 0.02:
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

            # sleep แบบ interruptible (ทุก 0.5s ตรวจ is_connected + cancel_check)
            t_end = _t.time() + interval
            while _t.time() < t_end:
                if not self.hw.is_connected:
                    raise HardwareError("Hardware disconnected during OCV settle")
                if cancel_check is not None and not cancel_check():
                    break
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
                    if load_i > 0.02:
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
                    if temp > limits.get("max_temperature", 60.0):
                        self._trigger_safety(
                            f"Temperature {temp:.1f}°C exceeds limit {limits['max_temperature']}°C")
                        break
                    if abs(i_net) > limits.get("max_current", 30.0):
                        self._trigger_safety(
                            f"Current {i_net:.2f}A exceeds limit {limits['max_current']}A")
                        break

                    # อัปเดต State Estimator ด้วย dt จริงต่อรอบ (ไม่ hardcode 0.1)
                    # `now` was already stamped right after read_vi() above.
                    dt = (now - self._last_update_time) if self._last_update_time else 0.1
                    self._last_update_time = now
                    state = self.estimator.update(
                        v, i_net, dt=dt, temp=self.hw.current_temp
                    )

                    # ส่งค่าไปอัปเดต UI (thread-safe ผ่าน root.after)
                    if self.ui and self.root:
                        self.root.after(
                            0,
                            self.ui.update_display,
                            v,
                            i_net,
                            state["soc"],
                            state["rin"],
                            self.hw.current_temp,
                            state["soh"],
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
    # Profile Control
    # ------------------------------------------------------------------

    def start_profile(self):
        """เริ่มรัน Profile"""
        if self.is_profile_running:
            return
        if not self.profile_data:
            if self.ui:
                self.root.after(0, self.ui.set_profile_status, "Status: No profile loaded")
            return

        self.is_profile_running = True
        self.safety_triggered = False

        # Set loading state
        if self.ui and self.root:
            self.root.after(0, lambda: self.ui.set_loading_state("btn_start_profile", True, "RUNNING PROFILE..."))
            self.root.after(0, lambda: self.ui.set_button_enabled("btn_start_profile", False))

        threading.Thread(target=self._run_profile_loop, daemon=True).start()

    def _run_profile_loop(self):
        """ลอจิกควบคุมกระแสตาม Profile"""
        prev_current = 0.0

        for current_target, duration in self.profile_data:
            if not self.is_profile_running or self.safety_triggered:
                break

            if self.ui and self.root:
                self.root.after(0, self.ui.set_profile_status, "Status: RUNNING", "#f97316")

            delta_I = current_target - prev_current
            if abs(delta_I) > 0.05:
                self.hw.transient_dcir_measure(current_target, delta_I)
            else:
                self.hw.set_load_raw(current_target)

            prev_current = current_target
            t_end = time.time() + duration
            while time.time() < t_end:
                if not self.is_profile_running or self.safety_triggered:
                    break
                time.sleep(0.05)

        # เมื่อจบโปรไฟล์
        self.is_profile_running = False
        try:
            with self.hw.inst_lock:
                self.hw.load_inst.write(":INP OFF")
        except Exception:
            pass

        # auto-analyze หลังจบโปรไฟล์ (ถ้าไม่ได้ถูกสั่งหยุดกลางคัน)
        if not self.safety_triggered:
            self._auto_analyze()

        if self.ui and self.root:
            self.root.after(0, lambda: self.ui.set_loading_state("btn_start_profile", False))
            self.root.after(0, lambda: self.ui.set_button_enabled("btn_start_profile", True))
            if not self.safety_triggered:
                self.root.after(0, self.ui.set_profile_status, "Status: Profile Completed")

    def stop_profile(self):
        """Stop running profile"""
        if not self.is_profile_running:
            logger.info("Profile stop requested (not running)")
            return

        logger.info("Stopping profile execution")
        self.is_profile_running = False
        self.safety_triggered = True  # ensure any safety-gated loops exit

        # Reset UI state
        if self.ui and self.root:
            self.root.after(0, lambda: self.ui.set_loading_state("btn_start_profile", False))
            self.root.after(0, lambda: self.ui.set_button_enabled("btn_start_profile", True))
            self.root.after(0, self.ui.set_profile_status, "Status: Profile Stopped", "#dc2626")

    # ------------------------------------------------------------------
    # Chemistry-aware Charging (3-stage lead-acid / CC-CV lithium)
    # ------------------------------------------------------------------

    def start_charge(self, float_hold_s: float = 0.0, strategy: str = None,
                     bulk_c_rate_override: float = None):
        """เริ่มชาร์จ; strategy=None → เลือกตามเคมีของแบตอัตโนมัติ
        (LeadAcid → 3-stage, Lithium → CC-CV). ส่ง strategy เพื่อ override จาก dropdown:
        "three_stage" หรือ "cc_cv". รันใน thread แยก; monitor loop ยัง log+safety ระหว่างชาร์จ
        """
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
            self.start_monitor()
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
    # IEC 61960 Standard Tests
    # ------------------------------------------------------------------

    def start_iec61960_test(self, test_id: str, iec_standard):
        """เริ่ม IEC 61960 standard test"""
        try:
            from aset_batt.core.iec61960_standard import TestType
            profile = iec_standard.get_test_profile(test_id)
            if not profile:
                raise ValueError(f"IEC 61960 test profile '{test_id}' not found")

            logger.info(f"Starting IEC 61960 test: {profile.name}")

            # สร้าง test data structure
            test_data = {
                'test_type': 'iec61960',
                'test_id': test_id,
                'profile': profile,
                'iec_standard': iec_standard,
                'start_time': time.time(),
                'results': {}
            }

            # เริ่ม monitoring thread
            self.is_profile_running = True
            self.profile_data = test_data

            # อัปเดต UI status
            if self.ui and self.root:
                self.root.after(0, self.ui.set_profile_status, "Running IEC 61960 Test", "#059669")
                self.root.after(0, lambda: self.ui.set_button_enabled("btn_start_profile", False))

            # ส่ง event ไปยัง UI
            if self.event_handler:
                self.event_handler.post_event(EventType.UPDATE_STATUS)

            # เริ่ม IEC 61960 test thread
            import threading
            test_thread = threading.Thread(target=self._run_iec61960_test, args=(test_data,))
            test_thread.daemon = True
            test_thread.start()

        except Exception as e:
            logger.error(f"Error starting IEC 61960 test: {e}")
            if self.ui:
                self.ui.set_profile_status("❌ Test Failed", "#dc2626")
            raise

    def _run_iec61960_test(self, test_data: dict):
        """รัน IEC 61960 test ใน background thread"""
        try:
            profile = test_data['profile']
            iec_standard = test_data['iec_standard']

            logger.info(f"Running IEC 61960 test: {profile.name}")

            # เลือก test procedure ตามประเภท
            if profile.test_type == TestType.CAPACITY_MEASUREMENT:
                self._run_capacity_test(profile, test_data)
            elif profile.test_type == TestType.ENERGY_DENSITY:
                self._run_energy_density_test(profile, test_data)
            elif profile.test_type == TestType.INTERNAL_RESISTANCE:
                self._run_internal_resistance_test(profile, test_data)
            elif profile.test_type == TestType.CYCLE_LIFE:
                self._run_cycle_life_test(profile, test_data)
            elif profile.test_type == TestType.SAFETY_TEST:
                self._run_safety_test(profile, test_data)
            else:
                raise ValueError(f"Unsupported test type: {profile.test_type}")

            # คำนวณผลและสร้าง report
            results = self._calculate_test_results(test_data)
            report = iec_standard.generate_test_report(test_data['test_id'], results)

            # บันทึกผล
            test_data['results'] = results
            test_data['report'] = report
            test_data['end_time'] = time.time()

            logger.info(f"IEC 61960 test completed: {profile.name}")

            # auto-analyze: ให้ AI grade ผลที่เพิ่ง log ลง CSV
            self._auto_analyze()

            # ส่ง event completion
            if self.event_handler:
                self.event_handler.post_event(
                    EventType.PROFILE_COMPLETED,
                    {"success": True, "results": results, "report": report},
                )

        except Exception as e:
            logger.error(f"IEC 61960 test failed: {e}")
            test_data['error'] = str(e)
            # R7 (industrial-grade audit): an unhandled exception inside any of the
            # _run_*_test() loops above used to just get logged here — the loop
            # itself (e.g. _run_cycle_life_test) has no try/except of its own, so a
            # genuinely unexpected fault (not a checked safety-limit breach, which
            # already calls _trigger_safety via check_safety_limits) left the PSU/
            # Load in whatever state they were in when the exception fired, with no
            # attempt to cut power. Treat any test-loop crash as unsafe by default.
            self._emergency_shutdown()

            if self.event_handler:
                self.event_handler.post_event(
                    EventType.PROFILE_COMPLETED,
                    {"success": False, "error": str(e)},
                )

        finally:
            self.is_profile_running = False
            if self.ui and self.root:
                self.root.after(0, self.ui.set_profile_status, "Test Completed", "#059669")
                self.root.after(0, lambda: self.ui.set_button_enabled("btn_start_profile", True))

    def _run_capacity_test(self, profile, test_data: dict):
        """รัน capacity measurement test ตาม IEC 61960"""
        logger.info("Running capacity measurement test")

        # ตั้ง discharge current ตาม C-rate
        discharge_current = profile.discharge_rate.value * self.config.battery.rated_capacity

        # ให้ dashboard เห็นข้อมูล IEC test แบบ live -> เปิด logging + อ้างอิงเวลาเริ่ม
        # (ก่อน set_load() — ต้องเปิด logging ให้พร้อมก่อน จะได้ log sample แรกสุด
        # ที่ขอบกระแสได้ทันที แทนที่จะรอ loop รอบแรกที่ 1Hz)
        if not self.data.is_recording:
            from aset_batt.storage.data_utils import DataHandler, write_session_metadata
            csv_path = DataHandler.make_session_path()
            ok, _ = self.data.start_logging(csv_path)
            if ok:
                write_session_metadata(csv_path, self.config)   # R3: audit trail
        if self._start_time is None:
            self._start_time = time.time()
            self._start_mono = time.perf_counter()

        # เริ่ม discharge จาก max voltage จนถึง min voltage
        self.hw.set_load(True, discharge_current)
        # Immediate post-edge sample before the 1Hz loop below reaches its first
        # iteration — identify_dcir()'s single-step method needs a post-edge sample
        # within _DCIR_MAX_STEP_DT (0.5s) of the true current transition, and this
        # loop's own pacing (1Hz) is 2x that gate (same root cause already fixed
        # for the HPPC/IEC/Quick Scan/Cycle Life sequences in sequences.py).
        try:
            v0, i0 = self.hw.read_measurements(prefer_load_v=True)
            self._log_sample(v0, i0)   # log-only (cached soc/rin) — no estimator.update()
        except Exception:
            pass

        # Monitor จนกระทั่ง voltage ตกลงถึง cutoff
        start_time = time.time()
        last_t = start_time           # ใช้คำนวณ dt จริงต่อรอบ (ไม่ hardcode 1.0)
        voltage_data = []
        current_data = []
        time_data = []

        while self.is_profile_running:
            voltage, current = self.hw.read_measurements(prefer_load_v=True)  # discharge → V from load
            temp = self.hw.current_temp  # ใช้อุณหภูมิจริงจาก ESP (ไม่ hardcode 25°C)
            now = time.time()
            dt = now - last_t            # เวลาจริงต่อรอบ (รวม SCPI latency + sleep)
            last_t = now
            elapsed = now - start_time

            voltage_data.append(voltage)
            current_data.append(current)
            time_data.append(elapsed)

            # Check safety limits ด้วยอุณหภูมิจริง
            if not self.check_safety_limits(voltage, current, temp):
                break

            # อัปเดต SoC/Rin ด้วย dt จริง (รอบจริง > 1.0s เพราะ SCPI + sleep) แล้ว log
            state = self.estimator.update(voltage, current, dt=dt, temp=temp)
            self.data.log_row(
                (time.perf_counter() - self._start_mono
                 if self._start_mono is not None else time.time() - self._start_time),
                voltage, current,
                state["soc"], state["rin"] * 1000, temp,
                rin_calibrated=state.get("rin_calibrated", True),
            )

            # Check end condition (เทียบกับ cutoff ระดับแพ็ค)
            if voltage <= self.config.battery.pack_min_voltage:
                break

            time.sleep(1.0)  # 1Hz sampling

        # หยุด discharge
        self.hw.set_load(False)
        # Same low-latency edge sample as the discharge-start above, for the
        # discharge-end transition.
        try:
            v_end, i_end = self.hw.read_measurements(prefer_load_v=False)
            self._log_sample(v_end, i_end)
        except Exception:
            pass
        self._ocv_reset_after_rest("discharge")

        # บันทึก test data
        test_data['voltage_data'] = voltage_data
        test_data['current_data'] = current_data
        test_data['time_data'] = time_data

    def _run_energy_density_test(self, profile, test_data: dict):
        """รัน energy density test"""
        logger.info("Running energy density test")
        # Implementation คล้าย capacity test แต่คำนวณ energy density เพิ่มเติม
        self._run_capacity_test(profile, test_data)

    def _run_internal_resistance_test(self, profile, test_data: dict):
        """วัด DCIR ตาม IEC 61960 Clause 6.4 — two-pulse method

        discharge 0.2C นาน 10s (วัด V1,I1) → สลับเป็น 1C นาน 1s ทันที (วัด V2,I2)
        → DCIR = (V1−V2)/(I2−I1).  กระแสถูก clamp ที่ max_current ของ rig เพื่อความปลอดภัย
        (ยังใช้สูตร two-point เดิมกับกระแสที่จ่ายจริง)
        """
        logger.info("Running IEC 61960 DCIR test (two-pulse 0.2C/1C)")
        self._ensure_logging()

        rated = self.config.battery.rated_capacity
        max_i = self.config.battery.max_current
        i1_set = min(0.2 * rated, max_i)        # 0.2C
        i2_set = min(1.0 * rated, max_i)        # 1C (clamp ตาม rig)
        if i2_set <= i1_set:
            i2_set = min(max_i, i1_set * 1.5)

        if not self.is_profile_running:
            return

        # Pulse 1: 0.2C นาน 10s
        self.hw.set_load(True, i1_set)
        t_end = time.time() + 10.0
        while time.time() < t_end and self.is_profile_running:
            v, i = self.hw.read_measurements(prefer_load_v=True)   # discharge → V from load
            if not self.check_safety_limits(v, i, self.hw.current_temp):
                self.hw.set_load(False)
                return
            self._log_sample(v, i)
            time.sleep(1.0)
        v1, i1 = self.hw.read_measurements(prefer_load_v=True)

        # Pulse 2: 1C นาน 1s (สลับทันทีเพื่อลด relaxation effect ตามมาตรฐาน)
        self.hw.set_load(True, i2_set)
        time.sleep(1.0)
        v2, i2 = self.hw.read_measurements(prefer_load_v=True)
        self._log_sample(v2, i2)
        self.hw.set_load(False)

        test_data['dcir_pulses'] = {"v1": v1, "i1": i1, "v2": v2, "i2": i2}

    def _run_cycle_life_test(self, profile, test_data: dict):
        """รัน cycle life test"""
        logger.info("Running cycle life test")

        capacity_history = []
        cycle_count = 0

        while self.is_profile_running and cycle_count < profile.cycles:
            cycle_count += 1
            logger.info(f"Starting cycle {cycle_count}/{profile.cycles}")

            # Charge phase (ถ้ามี charger control)
            if hasattr(self.hw, 'set_charge'):
                self.hw.set_charge(True, profile.charge_rate * self.config.battery.rated_capacity)
                # Monitor จน charge เต็ม
                while self.hw.read_measurements()[0] < self.config.battery.pack_max_voltage and self.is_profile_running:
                    time.sleep(10)

            # Discharge phase
            discharge_current = profile.discharge_rate.value * self.config.battery.rated_capacity
            self.hw.set_load(True, discharge_current)
            self._ensure_logging()

            # Monitor discharge — integrate the MEASURED current (real coulomb counting)
            # instead of assuming the setpoint flowed for the whole phase.
            start_time = time.time()
            last_t = start_time
            last_i = 0.0
            cap_ah = 0.0
            while self.is_profile_running:
                voltage, current = self.hw.read_measurements(prefer_load_v=True)  # discharge → V from load
                now = time.time()
                dt = now - last_t
                last_t = now
                i_dis = max(0.0, current)            # discharge-positive; ignore any charge
                cap_ah += 0.5 * (i_dis + last_i) * dt / 3600.0   # trapezoidal Ah
                last_i = i_dis
                self._log_sample(voltage, current)   # log the measured current, not the setpoint
                if voltage <= self.config.battery.pack_min_voltage:
                    break
                time.sleep(10)

            # capacity ของ cycle นี้ = ประจุที่วัดได้จริง (Ah)
            capacity_history.append(cap_ah)

            # Rest period
            self.hw.set_load(False)
            time.sleep(profile.rest_time_minutes * 60)

        test_data['capacity_history'] = capacity_history
        test_data['cycle_count'] = cycle_count

    def _run_safety_test(self, profile, test_data: dict):
        """รัน safety test"""
        logger.info("Running safety test")

        # Overcharge protection test
        if "overcharge" in profile.name.lower():
            # พยายาม charge เกิน limit
            if hasattr(self.hw, 'set_charge'):
                self.hw.set_charge(True, 2.0)  # High current

                start_time = time.time()
                while time.time() - start_time < profile.duration_hours * 3600 and self.is_profile_running:
                    voltage, current = self.hw.read_measurements()

                    # Check if protection activated
                    if current < 0.1:  # Protection triggered
                        test_data['protection_activated'] = True
                        test_data['protection_time'] = time.time() - start_time
                        break

                    time.sleep(1.0)

                self.hw.set_charge(False)

    def _calculate_test_results(self, test_data: dict) -> dict:
        """คำนวณผลการทดสอบตาม IEC 61960"""
        profile = test_data['profile']
        iec_standard = test_data['iec_standard']

        results = {}

        if profile.test_type == TestType.CAPACITY_MEASUREMENT:
            if 'voltage_data' in test_data:
                results = iec_standard.calculate_capacity(
                    test_data['voltage_data'],
                    test_data['current_data'],
                    test_data['time_data']
                )

        elif profile.test_type == TestType.ENERGY_DENSITY:
            if 'voltage_data' in test_data:
                capacity_results = iec_standard.calculate_capacity(
                    test_data['voltage_data'],
                    test_data['current_data'],
                    test_data['time_data']
                )
                energy_results = iec_standard.calculate_energy_density(
                    capacity_results['capacity_ah'],
                    self.config.battery.mass_grams,
                    avg_voltage=capacity_results.get('average_voltage'),
                )
                results = {**capacity_results, **energy_results}

        elif profile.test_type == TestType.INTERNAL_RESISTANCE:
            p = test_data.get('dcir_pulses')
            if p:
                results = iec_standard.calculate_dcir_two_pulse(
                    p["v1"], p["i1"], p["v2"], p["i2"]
                )

        elif profile.test_type == TestType.CYCLE_LIFE:
            if "capacity_history" in test_data:
                results = iec_standard.assess_cycle_life(test_data["capacity_history"])

        return results

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
        closeEvent ของ Qt เรียกตัดไฟทันที + bootstrapper.cleanup() เรียกอีกครั้ง)"""
        if self._shutdown_done:
            logger.debug("Controller shutdown ทำไปแล้ว — ข้าม")
            return
        self._shutdown_done = True
        logger.info("Starting controller shutdown")

        self.monitor_running = False
        self.is_profile_running = False
        self.is_charging = False
        if self._charge_ctrl is not None:
            self._charge_ctrl.stop()
        time.sleep(0.2)

        try:
            self.hw.shutdown_all()
            logger.info("Hardware shutdown completed")
        except Exception as e:
            logger.error(f"Error during hardware shutdown: {e}")

        try:
            self.data.stop_logging()
            logger.info("Data logging stopped")
        except Exception as e:
            logger.error(f"Error stopping data logging: {e}")

        logger.info("Controller shutdown completed")
