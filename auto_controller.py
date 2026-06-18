import threading
import time
import logging
from typing import Optional, Dict, Any

from service_locator import ServiceLocator
from event_system import EventType, UIEventHandler
from exceptions import SafetyError, HardwareError
from iec61960_standard import IEC61960Standard, TestType

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
        self.safety_triggered = False
        self.profile_data = []

        # เวลาเริ่มต้นสำหรับคำนวณ elapsed time ใน CSV
        self._start_time = None

        # Get event handler from service locator (registered after UI bootstrap)
        self.event_handler = None

        logger.info("AutoController initialized")

    def check_safety_limits(self, voltage: float, current: float, temperature: float) -> bool:
        """Check if parameters are within safety limits"""
        limits = self.config.system.safety_limits

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
                    v, psu_i, load_i = self.hw.read_vi()
                    i_net = psu_i - load_i

                    # Check safety limits
                    if not self.check_safety_limits(v, i_net, self.hw.current_temp):
                        break

                    # อัปเดต State Estimator
                    state = self.estimator.update(
                        v, i_net, dt=0.1, temp=self.hw.current_temp
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
                self.root.after(
                    0, lambda: self.ui.lbl_profile_status.config(
                        text="Status: No profile loaded"
                    )
                )
            return

        self.is_profile_running = True
        self.safety_triggered = False

        # Set loading state
        if self.ui and self.root:
            self.root.after(0, lambda: self.ui.set_loading_state("btn_start_profile", True, "RUNNING PROFILE..."))
            self.root.after(0, lambda: self.ui.btn_start_profile.config(state="disabled"))

        threading.Thread(target=self._run_profile_loop, daemon=True).start()

    def _run_profile_loop(self):
        """ลอจิกควบคุมกระแสตาม Profile"""
        prev_current = 0.0

        for current_target, duration in self.profile_data:
            if not self.is_profile_running or self.safety_triggered:
                break

            if self.ui and self.root:
                self.root.after(
                    0,
                    lambda: self.ui.lbl_profile_status.config(
                        text="Status: RUNNING", foreground="#f97316"
                    ),
                )

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

        if self.ui and self.root:
            self.root.after(0, lambda: self.ui.set_loading_state("btn_start_profile", False))
            self.root.after(0, lambda: self.ui.btn_start_profile.config(state="normal"))
            if not self.safety_triggered:
                self.root.after(
                    0,
                    lambda: self.ui.lbl_profile_status.config(text="Status: Profile Completed"),
                )

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
            self.root.after(0, lambda: self.ui.btn_start_profile.config(state="normal"))
            self.root.after(
                0,
                lambda: self.ui.lbl_profile_status.config(
                    text="Status: Profile Stopped", foreground="#dc2626"
                ),
            )

    # ------------------------------------------------------------------
    # IEC 61960 Standard Tests
    # ------------------------------------------------------------------

    def start_iec61960_test(self, test_id: str, iec_standard):
        """เริ่ม IEC 61960 standard test"""
        try:
            from iec61960_standard import TestType
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
                self.root.after(
                    0,
                    lambda: self.ui.lbl_profile_status.config(
                        text="Running IEC 61960 Test", foreground="#059669"
                    ),
                )
                self.root.after(0, lambda: self.ui.btn_start_profile.config(state="disabled"))

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
                self.ui.lbl_profile_status.config(text="❌ Test Failed", foreground="#dc2626")
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
                self.root.after(
                    0,
                    lambda: self.ui.lbl_profile_status.config(
                        text="Test Completed", foreground="#059669"
                    ),
                )
                self.root.after(0, lambda: self.ui.btn_start_profile.config(state="normal"))

    def _run_capacity_test(self, profile, test_data: dict):
        """รัน capacity measurement test ตาม IEC 61960"""
        logger.info("Running capacity measurement test")

        # ตั้ง discharge current ตาม C-rate
        discharge_current = profile.discharge_rate.value * self.config.battery.rated_capacity

        # เริ่ม discharge จาก max voltage จนถึง min voltage
        self.hw.set_load(True, discharge_current)

        # Monitor จนกระทั่ง voltage ตกลงถึง cutoff
        start_time = time.time()
        voltage_data = []
        current_data = []
        time_data = []

        while self.is_profile_running:
            voltage, current = self.hw.read_measurements()
            elapsed = time.time() - start_time

            voltage_data.append(voltage)
            current_data.append(current)
            time_data.append(elapsed)

            # Check safety limits
            if not self.check_safety_limits(voltage, current, 25.0):  # Assume 25°C
                break

            # Check end condition
            if voltage <= self.config.battery.min_voltage:
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
        """รัน internal resistance measurement"""
        logger.info("Running internal resistance test")

        # วัด DCIR ที่ discharge current ต่างๆ
        test_currents = [0.5, 1.0, 2.0, 5.0]  # A
        dcir_results = []

        for current in test_currents:
            if not self.is_profile_running:
                break

            # วัด voltage ก่อน discharge
            voltage_before = self.hw.read_measurements()[0]
            time.sleep(0.1)

            # เริ่ม discharge
            self.hw.set_load(True, current)
            time.sleep(0.5)  # Stabilization

            # วัด voltage ขณะ discharge
            voltage_after = self.hw.read_measurements()[0]

            # หยุด discharge
            self.hw.set_load(False)
            time.sleep(1.0)  # Recovery

            # คำนวณ DCIR
            dcir = abs((voltage_before - voltage_after) / current) * 1000  # mΩ
            dcir_results.append({
                'current_a': current,
                'voltage_before_v': voltage_before,
                'voltage_after_v': voltage_after,
                'dcir_mohm': dcir
            })

        test_data['dcir_results'] = dcir_results

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
                while self.hw.read_measurements()[0] < self.config.battery.max_voltage and self.is_profile_running:
                    time.sleep(10)

            # Discharge phase
            self.hw.set_load(True, profile.discharge_rate.value * self.config.battery.rated_capacity)

            # Monitor discharge
            start_time = time.time()
            while self.is_profile_running:
                voltage = self.hw.read_measurements()[0]
                if voltage <= self.config.battery.min_voltage:
                    break
                time.sleep(10)

            # วัด capacity ของ cycle นี้
            discharge_time = time.time() - start_time
            capacity_ah = profile.discharge_rate.value * self.config.battery.rated_capacity * (discharge_time / 3600)
            capacity_history.append(capacity_ah)

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
                    self.config.battery.mass_grams
                )
                results = {**capacity_results, **energy_results}

        elif profile.test_type == TestType.INTERNAL_RESISTANCE:
            if 'dcir_results' in test_data and test_data['dcir_results']:
                # ใช้ค่าเฉลี่ย
                avg_dcir = sum(r['dcir_mohm'] for r in test_data['dcir_results']) / len(test_data['dcir_results'])
                results = iec_standard.calculate_internal_resistance(
                    test_data['dcir_results'][0]['voltage_before_v'],
                    test_data['dcir_results'][0]['voltage_after_v'],
                    test_data['dcir_results'][0]['current_a']
                )

        elif profile.test_type == TestType.CYCLE_LIFE:
            if "capacity_history" in test_data:
                results = iec_standard.assess_cycle_life(test_data["capacity_history"])

        return results

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self):
        """Graceful shutdown of all systems"""
        logger.info("Starting controller shutdown")

        self.monitor_running = False
        self.is_profile_running = False
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
