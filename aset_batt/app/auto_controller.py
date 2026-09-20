import threading
import time
import logging
import os
import math
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
        self._monitor_thread = None
        self._monitor_generation = 0
        # A sequence logs and updates the estimator itself.  This gate is
        # separate from monitor_running because a monitor thread can be in a
        # hardware read when a sequence starts; it must never append an
        # unlabelled row into the sequence-owned CSV after that point.
        self.sequence_logging_owned = False
        self.live_readback_running = False   # lightweight pre-test Connect readback
        self._live_readback_thread = None
        self._live_readback_generation = 0
        self.is_charging = False
        self.safety_triggered = False
        self._charge_ctrl = None
        self._charge_thread = None
        self._charge_generation = 0
        self.operation_state = None
        self.last_charge_full_confirmed = False
        self.last_charge_error = None
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
        self._validation_ssr_event_seen: set[float] = set()

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
        # Cut the independent relay before waiting for either VISA command lock.
        # A measurement can hold inst_lock until its configured VISA timeout; if
        # SSR OFF is reached only through psu_off(), an E-STOP can leave the
        # physical path energized for that entire wait.
        failed = False
        set_ssr = getattr(self.hw, "set_ssr", None)
        if callable(set_ssr):
            try:
                if set_ssr(False) is False:
                    failed = True
                    logger.critical("Emergency shutdown: SSR OFF command was not confirmed")
            except Exception:
                failed = True
                logger.exception("Emergency shutdown: SSR OFF command failed")

        # Attempt both instrument outputs independently. A failure to stop one
        # must not prevent the other output from receiving its OFF command.
        for name in ("load_off", "psu_off"):
            try:
                if getattr(self.hw, name)() is False:
                    failed = True
                    logger.critical("Emergency shutdown: %s returned failure", name)
            except Exception:
                failed = True
                logger.exception("Emergency shutdown: %s failed", name)

        if failed:
            logger.critical("Emergency shutdown incomplete; hardware safe state is unconfirmed")
        else:
            logger.info("Emergency shutdown commands issued")

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
        if self.monitor_running:
            return True
        if self._monitor_thread is not None and self._monitor_thread.is_alive():
            return False
        if self._live_readback_thread is not None and self._live_readback_thread.is_alive():
            return False
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
                    self.data.stop_logging("interrupted", "new manual monitor session started")
                self._start_time = time.time()
                self._start_mono = time.perf_counter()
                from aset_batt.storage.data_utils import DataHandler, write_session_metadata
                csv_path = DataHandler.make_session_path()
                ok, msg = self.data.start_logging(csv_path, test_type="Manual Monitor")
                if not ok:
                    import logging
                    logging.getLogger(__name__).error(f"Cannot start CSV logging: {msg}")
                else:
                    write_session_metadata(
                        csv_path, self.config, session_id=self.data.session_id,
                        test_type=self.data.test_type,
                        hardware=self.hw,
                        extra={"protocol": {"id": "manual-monitor-v1",
                                            "purpose": "operator live monitoring"}},
                    )   # R3: audit trail
            self._monitor_generation += 1
            generation = self._monitor_generation
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop, args=(generation,),
                name=f"aset-monitor-{generation}", daemon=True)
            self._monitor_thread.start()
        return True

    def stop_monitor(self):
        """Stop the hardware monitoring loop"""
        if self.monitor_running:
            self.monitor_running = False
            self._monitor_generation += 1
            logger.info("Monitor loop stopped by user")

    def end_session(self, outcome: str = "completed", reason: str = ""):
        """ปิด CSV session ปัจจุบันอย่างชัดเจน (ให้ workflow ถัดไปเริ่ม session ใหม่
        แน่ๆ) — เรียกตอนจบ/ยกเลิก auto-sequence เท่านั้น. ไม่เรียกจาก stop_charge()/
        stop_monitor() ธรรมดา เพราะสองอันนั้นถูกเรียกกลาง sequence ระหว่างเปลี่ยน
        phase ด้วย (อยากให้ session เดิมยังอยู่); ไม่มีจุดนี้ is_recording จะค้าง True
        ตลอดไปหลัง sequence จบ ทำให้ sequence รอบถัดไปเผลอต่อท้ายไฟล์เดิม."""
        if self.data.is_recording:
            self.data.stop_logging(outcome, reason)
        self.sequence_logging_owned = False
        self._start_time = None
        self._start_mono = None
        self.clear_recovery_state()

    def begin_physical_session(self, test_type: str = ""):
        """Reset per-run accounting without discarding the calibrated battery model.

        A different battery/profile is reset by the UI identity contract; the same
        pack keeps its valid SoC/SoH anchor until that workflow takes a new OCV
        observation. This closes any abandoned recording before opening a new run.
        """
        if getattr(self.data, "is_recording", False):
            self.end_session("interrupted", f"superseded by new session: {test_type}")
        self._start_time = None
        self._start_mono = None
        self._last_update_time = None
        self._temp_stale_warned = False
        self.sequence_logging_owned = False
        self.last_charge_full_confirmed = False
        self.clear_recovery_state()

    # ------------------------------------------------------------------
    # Live readback — lightweight V/I/Temp display right after Connect, before
    # any test is running. No CSV logging, no state estimator (SoC/Rin need it).
    # Stops itself automatically once a real test starts start_monitor().
    # ------------------------------------------------------------------

    def start_live_readback(self):
        if self.live_readback_running or self.monitor_running:
            return True
        if self._live_readback_thread is not None and self._live_readback_thread.is_alive():
            return False
        self.live_readback_running = True
        self._live_readback_generation += 1
        generation = self._live_readback_generation
        self._live_readback_thread = threading.Thread(
            target=self._live_readback_loop, args=(generation,),
            name=f"aset-readback-{generation}", daemon=True)
        self._live_readback_thread.start()
        return True

    def stop_live_readback(self):
        self.live_readback_running = False
        self._live_readback_generation += 1

    def _live_readback_loop(self, generation=None):
        generation = self._live_readback_generation if generation is None else generation
        while (self.live_readback_running and not self.monitor_running
               and generation == self._live_readback_generation):
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
        """Use the shared rest/current-validated OCV path; never instant-anchor."""
        soc, _v, status = self.calibrate_from_ocv_stable()
        if status != "VALID_OCV":
            raise HardwareError(f"OCV anchor unavailable: {status}")
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
    _OCV_MAX_ABS_CURRENT_A = 0.10  # accommodates instrument zero/noise; reject meaningful current

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
                                  allow_bleed_off=True, min_rest_override=None,
                                  max_rest_override=None, interval_override=None,
                                  window_override=None, spread_override=None):
        """OCV calibration แบบ wait-for-settle ตามมาตรฐาน ΔV/Δt criterion.

        อ่านแรงดันทุก 5 วิ จนกว่าจะผ่านทั้งสองเงื่อนไข:
          1. ผ่านเวลาพักขั้นต่ำของแต่ละเคมี
          2. max(V) - min(V) < dv_thresh ในช่วงเวลา window

        on_progress(elapsed_s, voltage, dv_mv, status)
          status: "waiting" | "checking" | "settled" | "timeout"

        cancel_check: callable() → bool; คืน True เมื่อ sequence ยังทำงานอยู่
          (เช่น self._seq_running.is_set). ถ้าคืน False → หยุดรอทันที

        allow_bleed_off: set False when the caller is about to run an
        UNCONDITIONAL full CC-CV charge right after this anchor regardless of
        the SoC/OCV reading (HPPC/CycleLife PREPARE) — bleeding off surface
        charge only to have the charger immediately put it right back (plus
        more, to termination current) wastes ~5-10 min for zero effect on the
        test outcome. Keep True (default) whenever the reading feeds a real
        decision: a skip-charge threshold (IEC/AUTO sequence), or an
        immediately-following discharge/pulse test that needs an accurate
        rested baseline (Quick Scan, GITT, HPPC's post-charge rest, manual
        Calibrate button).

        คืน (soc, voltage, status)
        """
        import time as _t
        from aset_batt.acquisition.ocv_validation import (
            evaluate_ocv_window, OCV_STATUS_MEASUREMENT_INVALID,
            OCV_STATUS_OUT_OF_RANGE,
        )
        if not self.hw.is_connected:
            return float("nan"), float("nan"), OCV_STATUS_MEASUREMENT_INVALID
        try:
            self.hw.psu_off()
            self.hw.load_off()
            if bool(getattr(self.hw, "_psu_output_on", False)):
                raise RuntimeError("PSU output still reports ON")
        except Exception as exc:
            logger.warning("Could not confirm PSU/load OFF for OCV: %s", exc)
            return float("nan"), float("nan"), OCV_STATUS_MEASUREMENT_INVALID

        # A previous SoC (or the estimator's internal 50% seed) is not evidence
        # for the pack currently settling. Keep it out of the UI/CSV until the
        # final OCV anchor is established.
        if hasattr(self.estimator, "invalidate_soc"):
            self.estimator.invalidate_soc()

        chemistry = getattr(self.config.battery, "battery_type", "LiPO")
        min_rest, window, dv_thresh = self._OCV_SETTLE.get(
            chemistry, self._OCV_SETTLE["LiPO"]
        )
        if min_rest_override is not None:
            min_rest = float(min_rest_override)
        if window_override is not None:
            window = float(window_override)
        if spread_override is not None:
            dv_thresh = float(spread_override)
        timeout = (float(max_rest_override) if max_rest_override is not None
                   else max(min_rest * 4, 900))
        interval = (float(interval_override) if interval_override is not None else 5.0)

        readings  = []   # elapsed, voltage, measured current, temperature, validity
        t_start   = _t.perf_counter()
        settled   = False

        while True:
            if cancel_check is not None and not cancel_check():
                break

            elapsed = _t.perf_counter() - t_start
            if elapsed >= timeout:
                break

            try:
                v, psu_i, load_i = self.hw.read_vi()
                temp = self.hw.current_temp
                current = max(abs(float(psu_i)), abs(float(load_i)))
                current_net = float(load_i) - float(psu_i)
                temp_stale = getattr(self.hw, "temp_is_stale", lambda: False)()
                valid_sample = (all(math.isfinite(float(x)) for x in (v, psu_i, load_i, temp))
                                and float(v) > 0.0
                                and not temp_stale
                                and getattr(self.hw, "last_voltage_source", "unknown") != "unknown")
            except Exception as exc:
                logger.debug("OCV sample invalid: %s", exc)
                v = current = current_net = temp = float("nan")
                valid_sample = False

            self._raise_if_otp_tripped("during OCV settle")

            now = _t.perf_counter()
            readings.append((elapsed, float(v), float(current), float(temp), valid_sample))
            if elapsed >= min_rest and (elapsed - min_rest) % interval < 1.0:
                result = evaluate_ocv_window(
                    readings, outputs_off=True, min_rest_s=min_rest,
                    window_s=window, max_spread_v=dv_thresh,
                    max_abs_current_a=self._OCV_MAX_ABS_CURRENT_A, now_s=elapsed)
            else:
                result = {"valid": False, "status": "NOT_RESTED",
                          "voltage_window_v": None}
            dv = result["voltage_window_v"]
            status = result["status"]
            settled = bool(result["valid"])

            if on_progress:
                try:
                    on_progress(elapsed, v, dv * 1000 if dv is not None else float("nan"),
                                status, current_net, temp)
                except TypeError:
                    on_progress(elapsed, v, dv * 1000 if dv is not None else float("nan"), status)

            if settled:
                break

            # sleep แบบ interruptible (ทุก 0.5s ตรวจ is_connected + cancel_check + OTP)
            t_end = _t.perf_counter() + interval
            while _t.perf_counter() < t_end:
                if not self.hw.is_connected:
                    raise HardwareError("Hardware disconnected during OCV settle")
                if cancel_check is not None and not cancel_check():
                    break
                self._raise_if_otp_tripped("during OCV settle")
                _t.sleep(0.5)

        # Final sample independently passes the same measured-current gate.
        try:
            v_final, psu_i, load_i = self.hw.read_vi()
            temp_final = self.hw.current_temp
            current_final = max(abs(float(psu_i)), abs(float(load_i)))
            current_net_final = float(load_i) - float(psu_i)
            final_valid = (all(math.isfinite(float(x)) for x in
                               (v_final, psu_i, load_i, temp_final)) and float(v_final) > 0.0
                           and getattr(self.hw, "last_voltage_source", "unknown") != "unknown"
                           and not getattr(self.hw, "temp_is_stale", lambda: False)())
        except Exception:
            v_final = temp_final = current_final = float("nan")
            current_net_final = float("nan")
            final_valid = False
        elapsed_final = _t.perf_counter() - t_start
        readings.append((elapsed_final, float(v_final), float(current_final),
                         float(temp_final), final_valid))
        result = evaluate_ocv_window(
            readings, outputs_off=True, min_rest_s=min_rest, window_s=window,
            max_spread_v=dv_thresh, max_abs_current_a=self._OCV_MAX_ABS_CURRENT_A,
            now_s=elapsed_final)
        final_status = result["status"]
        # Surface-charge / not-actually-at-equilibrium check — see
        # BatteryModel.ocv_out_of_range_mv's docstring. This settle window is
        # tuned for coulomb-counting drift (seconds-to-minutes), not for lead-acid
        # surface charge (hours) — a reading outside the curve's own calibrated
        # range is flat/stable within the window without being at true rest.
        oor_mv = (self.estimator.battery_model.ocv_out_of_range_mv(v_final, temp_final)
                  if final_valid else 0.0)
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
            # helpful. One attempt only (allow_bleed_off=False on the recursive
            # re-check) — a pack still out of range after a real bleed-off is a
            # genuine anomaly to surface, not something to keep retrying.
            from aset_batt.core import battery_profiles
            chem_name = battery_profiles.get_chemistry(chemistry).name
            if oor_mv > 0.0 and allow_bleed_off and chem_name == "LeadAcid":
                if self._bleed_off_surface_charge(on_progress=on_progress,
                                                  cancel_check=cancel_check):
                    return self.calibrate_from_ocv_stable(
                        on_progress=on_progress, cancel_check=cancel_check,
                        allow_bleed_off=False)
            final_status = OCV_STATUS_OUT_OF_RANGE
        if result["valid"] and final_status != OCV_STATUS_OUT_OF_RANGE:
            soc = self.estimator.sync_with_ocv(v_final, temp_final)
        else:
            soc = float("nan")
            if elapsed_final >= timeout:
                if max_rest_override is not None:
                    final_status = "OCV_TIMEOUT"
                elif final_status not in {
                        "CURRENT_NOT_ZERO", "VOLTAGE_UNSTABLE", "MEASUREMENT_INVALID"}:
                    final_status = "TIMEOUT"
        logger.info(
            "OCV stable: %.3fV @ %.1f°C → SoC %s (%s, elapsed %.0fs)",
            v_final, temp_final, f"{soc:.1f}%" if math.isfinite(soc) else "N/A",
            final_status, _t.perf_counter() - t_start,
        )
        if on_progress:
            try:
                on_progress(elapsed_final, v_final,
                            result["voltage_window_v"] * 1000.0
                            if result["voltage_window_v"] is not None else float("nan"),
                            final_status, current_net_final, temp_final)
            except TypeError:
                on_progress(elapsed_final, v_final, 0.0, final_status)
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
            t0 = _t.perf_counter()
            while _t.perf_counter() - t0 < self._SURFACE_CHARGE_BLEED_DURATION_S:
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
                    on_progress(_t.perf_counter() - t0, v, float("nan"), "bleeding")
                t_end = _t.perf_counter() + self._SURFACE_CHARGE_BLEED_POLL_S
                while _t.perf_counter() < t_end:
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

    def _monitor_loop(self, generation=None):
        """ลูปอ่าน Voltage, Current และอัปเดต SoC/UI"""
        if not hasattr(self, "_monitor_generation"):
            self._monitor_generation = 0
        generation = self._monitor_generation if generation is None else generation
        consec_errors = 0
        while self.monitor_running and generation == self._monitor_generation:
            loop_t0 = time.perf_counter()
            # Sequence threads own both estimator input and CSV provenance.
            # Waiting here rather than racing a last monitor iteration against
            # the first OCV/mini-pulse row prevents blank Mode/Phase rows.
            if self.sequence_logging_owned:
                time.sleep(self._MONITOR_TARGET_PERIOD_S)
                continue
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
                    voltage_source = getattr(self.hw, "last_voltage_source", "unknown")
                    current_source = getattr(self.hw, "last_current_source", "unknown")

                    # Monitor loop only checks temperature & overcurrent —
                    # voltage OVP/UVP is handled by the discharge test loop and
                    # ChargeController so we don't kill live monitoring on a
                    # low-voltage battery that is being charged.
                    get_temp_info = getattr(self.hw, "temperature_measurement", None)
                    if callable(get_temp_info):
                        temp_info = get_temp_info()
                    else:
                        # Compatibility for third-party test/simulation HALs;
                        # concrete HardwareController uses the authoritative API.
                        stale = bool(getattr(self.hw, "temp_is_stale", lambda *_: True)())
                        temp_info = {"temperature_c": float(self.hw.current_temp),
                                     "temperature_valid": not stale and math.isfinite(float(self.hw.current_temp)),
                                     "temperature_age_s": None,
                                     "temperature_source": "legacy backend",
                                     "temperature_status": "STALE" if stale else "VALID"}
                    temp = temp_info["temperature_c"]
                    if not temp_info["temperature_valid"]:
                        self._trigger_safety(f"TEMP_SENSOR_{temp_info['temperature_status']}: OTP unavailable")
                        break
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
                    # No measured interval exists before the first readback.
                    # StateEstimator treats dt=0 as a timestamp initializer and
                    # performs no physical integration for that sample.
                    dt = (now - self._last_update_time) if self._last_update_time else 0.0
                    self._last_update_time = now
                    state = self.estimator.update(
                        v, i_net, dt=dt, temp=self.hw.current_temp
                    )
                    soc_for_publish = (state["soc"] if getattr(
                        self.estimator, "soc_is_initialized", True) else float("nan"))

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
                            soc_for_publish,
                            state["rin"],
                            self.hw.current_temp,
                            state["soh"],
                            _gen,
                        )

                    # คำนวณ elapsed seconds จากเวลาเริ่มต้น (monotonic — ดู _start_mono)
                    elapsed = (time.perf_counter() - self._start_mono
                               if self._start_mono is not None else 0.0)
                    self.data.log_row(
                        elapsed, v, i_net,
                        soc_for_publish, state['rin'] * 1000,  # NaN until OCV/endpoint anchor
                        self.hw.current_temp,
                        rin_calibrated=state.get('rin_calibrated', True),
                        voltage_source=voltage_source,
                        current_source=current_source,
                        expected_dt_s=self._DEFAULT_MONITOR_DT,
                        temperature_status=temp_info["temperature_status"],
                        temperature_age_s=temp_info["temperature_age_s"],
                        temperature_source=temp_info["temperature_source"],
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
        self._monitor_thread = None
        self._monitor_generation = 0
        self._monitor_thread = None
        self._monitor_generation = 0
        # Losing telemetry while an output may be active removes the software
        # safety feedback loop. Treat that as a safety trip and cut outputs;
        # merely stopping monitor_running would leave manual/direct load output
        # enabled with no further cutoff supervision.
        self.safety_triggered = True
        operation_state = getattr(self, "operation_state", None)
        if operation_state is not None:
            operation_state.latch_estop()
        self._emergency_shutdown()
        if self.event_handler:
            self.event_handler.post_event(
                EventType.SHOW_MESSAGE,
                ("Monitor Stopped", reason, "error")
            )
    # Chemistry-aware Charging (3-stage lead-acid / CC-CV lithium)
    # ------------------------------------------------------------------

    def start_charge(self, float_hold_s: float = 0.0, strategy: str = None,
                     bulk_c_rate_override: float = None, reuse_session: bool = False,
                     sample_callback=None, start_monitor: bool = True):
        """เริ่มชาร์จ; strategy=None → เลือกตามเคมีของแบตอัตโนมัติ
        (LeadAcid → 3-stage, Lithium → CC-CV). ส่ง strategy เพื่อ override จาก dropdown:
        "three_stage" 또는 "cc_cv". રันใน thread แยก; monitor loop ยัง log+safety ระหว่างชาร์จ

        reuse_session: ส่งเป็น True เมื่อเรียกจากกลาง auto-sequence (session เปิดไว้
        แล้วตั้งแต่ PREPARE) — ดู start_monitor()'s docstring. ปุ่มมือ Start Charge
        ใช้ค่าเริ่มต้น False เสมอ (อยากได้ session ใหม่ทุกครั้งที่ผู้ใช้กดเอง)."""
        if self.is_charging:
            logger.info("Charge already running")
            return False
        if self._charge_thread is not None and self._charge_thread.is_alive():
            logger.info("Previous charge worker is still cleaning up")
            return False
        if not self.hw.is_connected:
            logger.error("Cannot charge: hardware not connected")
            return False
        if self.safety_triggered:
            logger.error("Cannot charge: E-STOP latch requires explicit operator reset")
            return False
        if self.estimator is None or self.estimator.battery_model is None:
            logger.error("Cannot charge: battery model unavailable")
            return False

        self.last_charge_error = None
        self.last_charge_full_confirmed = False
        self.is_charging = True
        if not self.monitor_running and start_monitor:
            if not self.start_monitor(reuse_session=reuse_session):
                self.is_charging = False
                return False
        self._charge_generation += 1
        generation = self._charge_generation
        self._charge_thread = threading.Thread(
            target=self._run_charge_loop,
            args=(float_hold_s, strategy, bulk_c_rate_override, sample_callback, generation),
            name=f"aset-charge-{generation}", daemon=True)
        self._charge_thread.start()
        return True

    def _run_charge_loop(self, float_hold_s: float, strategy: str = None,
                         bulk_c_rate_override: float = None, sample_callback=None,
                         generation=None):
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

            def on_update(stage, voltage, i_charge, note):
                self._on_charge_update(stage, voltage, i_charge, note)
                if sample_callback is not None:
                    sample_callback(stage, voltage, i_charge, note)

            self._charge_ctrl = ChargeController(
                self.hw, self.config, self.estimator.battery_model,
                on_update=on_update, strategy=strategy,
                bulk_c_rate_override=bulk_c_rate_override,
            )
            final_stage = self._charge_ctrl.run(
                should_stop=lambda: (self.safety_triggered or not self.is_charging),
                float_hold_s=float_hold_s,
            )
            logger.info(f"Charge loop finished at stage: {final_stage}")
            self.last_charge_full_confirmed = bool(
                getattr(self._charge_ctrl, "full_charge_confirmed", False))
        except Exception as e:
            self.last_charge_full_confirmed = False
            self.last_charge_error = e
            logger.error(f"Charge loop error: {e}")
        finally:
            if generation is None or generation == self._charge_generation:
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
        self._charge_generation += 1
        if self._charge_ctrl is not None:
            self._charge_ctrl.stop()
        try:
            self.hw.psu_off()
        except Exception as e:
            logger.error(f"psu_off during stop_charge failed: {e}")

    # ------------------------------------------------------------------
    # Logging / auto-analysis helpers
    # ------------------------------------------------------------------
    def _ensure_logging(self, label: str = "", protocol: dict | None = None):
        """เปิด CSV logging + ตั้งเวลาเริ่ม ถ้ายังไม่ได้เปิด (ให้ IEC test โผล่บน dashboard)

        label (เช่น "HPPC", "QuickScan") ถูกฝังในชื่อไฟล์ session เพื่อบอกชนิดเทสต์."""
        if not self.data.is_recording:
            from aset_batt.storage.data_utils import DataHandler, write_session_metadata
            csv_path = DataHandler.make_session_path(label=label)
            ok, _ = self.data.start_logging(csv_path, test_type=label)
            if ok:
                write_session_metadata(
                    csv_path, self.config, session_id=self.data.session_id,
                    test_type=self.data.test_type,
                    hardware=self.hw,
                    extra={"protocol": protocol or {"id": f"{label or 'unknown'}-v1"}},
                )   # R3: audit trail
        if self._start_time is None:
            self._start_time = time.time()
            self._start_mono = time.perf_counter()

    def _log_sample(self, voltage: float, current: float, mode: str = "",
                    expected_dt_s: Optional[float] = None):
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
            soc = (self.estimator.soc if getattr(self.estimator, "soc_is_initialized", True)
                   else float("nan"))
            temp_info = self.hw.temperature_measurement()
            self.data.log_row(
                (time.perf_counter() - self._start_mono
                 if self._start_mono is not None else 0.0),
                voltage, current,
                soc, self.estimator.rin * 1000.0,
                temp_info["temperature_c"], rin_calibrated=calibrated,
                mode=mode, phase=mode,
                voltage_source=getattr(self.hw, "last_voltage_source", "unknown"),
                current_source=getattr(self.hw, "last_current_source", "unknown"),
                expected_dt_s=expected_dt_s,
                temperature_status=temp_info["temperature_status"],
                temperature_age_s=temp_info["temperature_age_s"],
                temperature_source=temp_info["temperature_source"],
            )
            # The current sample can only bound, never directly measure, SSR
            # switching latency.  Record the first near-zero read after a
            # successfully-issued OFF command while this session is active.
            event = getattr(self.hw, "last_ssr_event", None)
            if (isinstance(event, dict) and not event.get("state", True)
                    and abs(float(current)) <= self._LOAD_NOISE_FLOOR_A
                    and self._start_mono is not None):
                command_t = event.get("command_monotonic_s")
                if isinstance(command_t, (int, float)) and command_t >= self._start_mono:
                    marker = float(command_t)
                    if marker not in self._validation_ssr_event_seen:
                        from aset_batt.storage.data_utils import record_session_event
                        observed_elapsed = time.perf_counter() - self._start_mono
                        record_session_event(
                            self.data.current_path, "ssr_interruption_telemetry_bound", {
                                "command_elapsed_s": marker - self._start_mono,
                                "first_zero_sample_elapsed_s": observed_elapsed,
                                "upper_bound_s": max(0.0, observed_elapsed - (marker - self._start_mono)),
                                "current_a": float(current),
                                "zero_current_threshold_a": self._LOAD_NOISE_FLOOR_A,
                                "claim": "Observed command-to-next-near-zero-current upper bound; not relay switching time.",
                            })
                        self._validation_ssr_event_seen.add(marker)
        except Exception as e:
            from aset_batt.storage.data_utils import StorageError
            if isinstance(e, StorageError):
                raise
            logger.debug("log_sample error: %s", e)

    def _auto_analyze(self, force_hppc: bool = False, fit_ecm=None) -> dict | None:
        """รัน unified analysis (ECM/grade) บน CSV ล่าสุด แล้ว post ANALYSIS_COMPLETED -> UI.
        ใช้วิธีวิเคราะห์เดียวกับ characterization test และปุ่ม Analyze CSV (วิธีเดียวทั้งระบบ)

        ``fit_ecm``: pass True to attempt a pulse fit on a NON-HPPC record (e.g.
        Quick Scan's mini-pulse leg) without setting force_hppc — force_hppc=True
        would suppress the Quick Scan's observed/rate-normalised capacity fields.
        Those values are diagnostic estimates; the verified capacity grade remains
        reserved for a full C10 capacity sequence.

        Returns the result dict, or None on failure."""
        try:
            from aset_batt.acquisition.analysis import analyze_csv_mp, profile_from_config
            csv_path = self.data.current_path or self.config.system.csv_filepath
            # The sequence is still live while its result screen is produced.
            # Force the final pulse / cut-off rows out of the one-second buffer
            # before reading the CSV, otherwise a report can omit its most
            # important evidence.
            self.data.flush()
            res = analyze_csv_mp(csv_path, profile_from_config(self.config),
                                 force_hppc=force_hppc, fit_ecm=fit_ecm)
        except Exception as e:
            logger.warning("auto-analyze ล้มเหลว: %s", e)
            return None
        if self.event_handler:
            self.event_handler.post_event(EventType.ANALYSIS_COMPLETED, res)
        if str(getattr(self.data, "test_type", "")).lower() == "quickscan":
            try:
                from aset_batt.storage.data_utils import update_session_metadata
                update_session_metadata(csv_path, {
                    "q_removed_ah": res.get("q_removed_ah"),
                    "q_interval_removed_ah": res.get("q_interval_removed_ah"),
                    "quick_mean_discharge_a": res.get("quick_mean_discharge_a"),
                    "peukert_k": res.get("quick_peukert_k", res.get("peukert_k")),
                    "peukert_k_source": res.get("peukert_k_source", "LEGACY_UNKNOWN"),
                    "peukert_reference_hr": res.get("peukert_reference_hr"),
                    "peukert_reference_current_a": res.get("reference_current_c10_a"),
                    "peukert_factor": res.get("peukert_factor"),
                    "peukert_formula_version": "peukert-power-law-v1",
                    "reference_current_c10_a": res.get("reference_current_c10_a"),
                    "q_c10_interval_equivalent_ah": res.get("q_c10_interval_equivalent_ah"),
                    "soc_start": res.get("quick_soc_start_pct"),
                    "soc_end": res.get("quick_soc_end_pct"),
                    "ocv_start_soc_valid": res.get("quick_ocv_start_valid"),
                    "ocv_end_soc_valid": res.get("quick_ocv_end_valid"),
                    "quick_full_capacity_est_ah": res.get("quick_full_capacity_est_ah"),
                    "quick_soh_est_pct": res.get("quick_soh_est_pct"),
                    "quick_soh_status": res.get("quick_soh_status"),
                    "quick_capacity_est_ah": res.get("quick_capacity_est_ah"),
                    "quick_capacity_est_valid": res.get("quick_capacity_est_valid"),
                    "quick_capacity_est_status": res.get("quick_capacity_est_status"),
                    "quick_soh_est_valid": res.get("quick_soh_est_valid"),
                    "dcir_mohm": res.get("dcir_mohm"),
                    "dcir_valid": res.get("dcir_measured"),
                    "dcir_latency_s": res.get("dcir_latency_s"),
                    "dcir_source": res.get("dcir_source"),
                    "dcir_phase": res.get("dcir_phase"),
                    "dcir_edge_type": res.get("dcir_edge_type"),
                    "ecm_R0_mohm": res.get("r0_mohm") if res.get("ecm_identified") else None,
                    "ecm_R1_mohm": res.get("r1_mohm") if res.get("ecm_identified") else None,
                    "ecm_C1_F": res.get("c1_farad") if res.get("ecm_identified") else None,
                    "ecm_tau_s": res.get("tau_s") if res.get("ecm_identified") else None,
                    "ecm_fit_quality": res.get("ecm_r2") if res.get("ecm_identified") else None,
                    "cca_proxy_a": res.get("cca_est_a"),
                    "cca_proxy_source": res.get("cca_proxy_source", "UNKNOWN"),
                    "analysis_version": "quick-screen-v5",
                })
            except Exception as exc:
                logger.warning("Quick Scan result metadata update failed: %s", exc)
        campaign = getattr(self.config.system, "validation_campaign", {}) or {}
        if (campaign.get("enabled") and res.get("capacity_gradeable")
                and res.get("capacity_basis") == "MAIN_DISCHARGE"):
            # This is the only automatic calibration hand-off: a qualified C10
            # result becomes the independent capacity ruler for later GITT,
            # HPPC and EKF evidence of this in-memory campaign.
            campaign = dict(campaign)
            campaign["reference_capacity_ah"] = float(res.get("capacity_ah"))
            related = dict(campaign.get("related_sessions") or {})
            related["c10"] = os.path.basename(csv_path)
            campaign["related_sessions"] = related
            self.config.system.validation_campaign = campaign
            try:
                from aset_batt.storage.data_utils import record_session_event
                record_session_event(csv_path, "c10_reference_qualified", {
                    "capacity_ah": campaign["reference_capacity_ah"],
                    "soh_pct": res.get("soh"), "capacity_basis": res.get("capacity_basis"),
                })
            except Exception as exc:
                logger.warning("Could not record C10 validation reference: %s", exc)
        return res

    def _ocv_reset_after_rest(self, phase: str, rest_s: float = 30.0):
        """Re-anchor only if the shared OCV current/rest/stability gates pass."""
        if self.estimator is None or not self.hw.is_connected:
            return
        try:
            logger.info("OCV reset: resting %.0fs after %s …", rest_s, phase)
            time.sleep(rest_s)
            soc, v, status = self.calibrate_from_ocv_stable()
            logger.info("OCV observation after %s: %.3fV → SoC %s (%s)",
                        phase, v, f"{soc:.1f}%" if math.isfinite(soc) else "N/A", status)
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
            hw_ok = self.hw.shutdown_all() is not False
            if hw_ok:
                logger.info("Hardware shutdown completed")
            else:
                logger.critical("Hardware shutdown incomplete; OFF state was not confirmed")
        except Exception as e:
            logger.error(f"Error during hardware shutdown: {e}")
        # latch idempotency เฉพาะเมื่อขั้นตัดไฟ/ตัดการเชื่อมต่อสำเร็จจริง — ถ้า fail
        # ให้การเรียกซ้ำครั้งถัดไป (เช่น bootstrapper.cleanup) ได้ลองตัดไฟใหม่
        # แทนที่จะโดนข้ามเพราะ flag ถูกตั้งไว้ตั้งแต่บรรทัดแรกแบบเดิม
        self._shutdown_done = hw_ok

        try:
            self.data.stop_logging("interrupted", "application shutdown")
            logger.info("Data logging stopped")
        except Exception as e:
            logger.error(f"Error stopping data logging: {e}")

        logger.info("Controller shutdown completed")
