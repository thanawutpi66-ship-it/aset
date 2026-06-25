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

    def __init__(self, root, hw, data, estimator, config):
        self.root = root
        self.hw = hw
        self.data = data
        self.estimator = estimator
        self.config = config
        self.ui = None  # จะถูกเซ็ตจาก main.py

        # System States
        self.monitor_running = False
        self.is_profile_running = False
        self.is_charging = False
        self.safety_triggered = False
        self.profile_data = []
        self._charge_ctrl = None
        self._shutdown_done = False   # กัน shutdown ทำงานซ้ำ (idempotent)

        # เวลาเริ่มต้นสำหรับคำนวณ elapsed time ใน CSV
        self._start_time = None
        self._last_update_time = None  # ใช้คำนวณ dt จริงต่อรอบ (coulomb counting)

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
            self.hw.psu_off()
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
            self._start_time = time.time()
            self._last_update_time = None
            self.monitor_running = True
            # เริ่ม data logging ทันทีที่ connect
            csv_path = self.config.system.csv_filepath
            ok, msg = self.data.start_logging(csv_path)
            if not ok:
                import logging
                logging.getLogger(__name__).error(f"Cannot start CSV logging: {msg}")
            threading.Thread(target=self._monitor_loop, daemon=True).start()

    def stop_monitor(self):
        """Stop the hardware monitoring loop"""
        if self.monitor_running:
            self.monitor_running = False
            logger.info("Monitor loop stopped by user")

    def calibrate_from_ocv(self):
        """Calibrate SoC from OCV reading when battery is rested"""
        if not self.hw.is_connected:
            raise HardwareError("Hardware must be connected to calibrate from OCV")

        v, psu_i, load_i = self.hw.read_vi()
        ocv_temp = self.hw.current_temp
        soc = self.estimator.sync_with_ocv(v, ocv_temp)
        logger.info(f"Calibrated SoC from OCV: {v:.3f}V @ {ocv_temp:.1f}°C -> {soc:.1f}%")
        return soc

    def _monitor_loop(self):
        """ลูปอ่าน Voltage, Current และอัปเดต SoC/UI"""
        while self.monitor_running:
            if self.hw.is_connected:
                try:
                    # อ่านค่าจาก Hardware
                    # Convention: discharge = บวก (ให้ตรงกับ StateEstimator,
                    # CSV/dashboard และ generate_sample_data)
                    # load_i = กระแส discharge ที่ load ดึงออก, psu_i = กระแสชาร์จเข้า
                    v, psu_i, load_i = self.hw.read_vi()
                    i_net = load_i - psu_i

                    # Check safety limits
                    if not self.check_safety_limits(v, i_net, self.hw.current_temp):
                        break

                    # อัปเดต State Estimator ด้วย dt จริงต่อรอบ (ไม่ hardcode 0.1)
                    now = time.time()
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

                    # คำนวณ elapsed seconds จากเวลาเริ่มต้น
                    elapsed = time.time() - self._start_time
                    self.data.log_row(
                        elapsed, v, i_net,
                        state['soc'], state['rin'] * 1000,  # แปลงเป็น mOhm
                        self.hw.current_temp
                    )
                except HardwareError as e:
                    logger.error(f"Hardware error in monitor loop: {e}")
                    if self.event_handler:
                        self.event_handler.post_event(
                            EventType.SHOW_MESSAGE,
                            ("Hardware Error", str(e), "error")
                        )
                    break
                except SafetyError as e:
                    logger.error(f"Safety error in monitor loop: {e}")
                    break
                except Exception as e:
                    logger.error(f"Unexpected error in monitor loop: {e}", exc_info=True)
                    break
            time.sleep(0.1)
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

    def start_charge(self, float_hold_s: float = 0.0, strategy: str = None):
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
                         args=(float_hold_s, strategy), daemon=True).start()
        return True

    def _run_charge_loop(self, float_hold_s: float, strategy: str = None):
        from aset_batt.core.charge_controller import ChargeController
        logger.info("Charge loop started (strategy=%s)", strategy or "auto")
        try:
            # ปิด load ก่อนชาร์จ (กันชาร์จ-ดิสชาร์จพร้อมกัน)
            self.hw.load_off()
            self._charge_ctrl = ChargeController(
                self.hw, self.config, self.estimator.battery_model,
                on_update=self._on_charge_update, strategy=strategy,
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

        # เริ่ม discharge จาก max voltage จนถึง min voltage
        self.hw.set_load(True, discharge_current)

        # ให้ dashboard เห็นข้อมูล IEC test แบบ live -> เปิด logging + อ้างอิงเวลาเริ่ม
        if not self.data.is_recording:
            self.data.start_logging(self.config.system.csv_filepath)
        if self._start_time is None:
            self._start_time = time.time()

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
                time.time() - self._start_time, voltage, current,
                state["soc"], state["rin"] * 1000, temp
            )

            # Check end condition (เทียบกับ cutoff ระดับแพ็ค)
            if voltage <= self.config.battery.pack_min_voltage:
                break

            time.sleep(1.0)  # 1Hz sampling

        # หยุด discharge
        self.hw.set_load(False)

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
    def _ensure_logging(self):
        """เปิด CSV logging + ตั้งเวลาเริ่ม ถ้ายังไม่ได้เปิด (ให้ IEC test โผล่บน dashboard)"""
        if not self.data.is_recording:
            self.data.start_logging(self.config.system.csv_filepath)
        if self._start_time is None:
            self._start_time = time.time()

    def _log_sample(self, voltage: float, current: float):
        """log หนึ่งแถว ใช้ค่า SoC/Rin ล่าสุดจาก estimator (สำหรับ IEC test ที่ไม่ผ่าน monitor loop)"""
        try:
            self.data.log_row(
                time.time() - self._start_time, voltage, current,
                self.estimator.soc, self.estimator.rin * 1000.0,
                self.hw.current_temp,
            )
        except Exception as e:
            logger.debug("log_sample error: %s", e)

    def _auto_analyze(self):
        """รัน unified analysis (ECM/grade) บน CSV ล่าสุด แล้ว post ANALYSIS_COMPLETED -> UI.
        ใช้วิธีวิเคราะห์เดียวกับ characterization test และปุ่ม Analyze CSV (วิธีเดียวทั้งระบบ)"""
        try:
            from aset_batt.acquisition.analysis import analyze_csv, profile_from_config
            res = analyze_csv(self.config.system.csv_filepath,
                              profile_from_config(self.config))
        except Exception as e:
            logger.warning("auto-analyze ล้มเหลว: %s", e)
            return
        if self.event_handler:
            self.event_handler.post_event(EventType.ANALYSIS_COMPLETED, res)

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
