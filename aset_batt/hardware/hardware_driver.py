import pyvisa
import pyvisa.constants as const
import serial
import serial.tools.list_ports
import threading
import time
import re
import logging

logger = logging.getLogger(__name__)

class HardwareController:
    def __init__(self):
        self.rm = pyvisa.ResourceManager()
        self.psu_inst = None
        self.load_inst = None
        self.is_connected = False
        self.inst_lock = threading.Lock()

        self.esp_serial = None
        self.is_esp_connected = False
        self.current_temp = 0.0
        self.last_esp_heartbeat = time.time()
        self.connect_error: str = ""       # ข้อความ error ล่าสุดของ PSU/Load — ว่างเปล่าเมื่อ connect สำเร็จ
        self.esp_connect_error: str = ""   # ข้อความ error ล่าสุดของ ESP32

        # SSR (solid-state relay) safety cutoff on ESP32 GPIO16 — physically gates
        # power to PSU + load, independent of each instrument's own output relay.
        self.ssr_state = None              # None=unknown, True=ON, False=OFF
        self._esp_write_lock = threading.Lock()   # guard writes vs. the read-only monitor thread

        # Combined-measurement capability per instrument (None=unknown, True/False=cached
        # after the first probe). MEAS:SCAL:ALL:DC? returns V,I,P from ONE instantaneous
        # measurement → V and I are simultaneous (no intra-sample skew) and it's one
        # round-trip instead of two. Probed lazily; falls back to separate MEAS queries.
        self._psu_all = None
        self._load_all = None

        # PSU current zero-offset: some units (e.g. PSW 80-40.5) read ~0.6 A on
        # MEAS:CURR? even with OUTPUT OFF.  calibrate_psu_zero() measures the offset
        # with output off and stores it here; _meas_vi subtracts it automatically.
        self._psu_current_offset: float = 0.0

        # Tracks whether PSU OUTPUT is currently ON.  Used by the monitor loop to
        # distinguish CHARGE (OUTPUT ON → i_net = −psu_i) from REST (OUTPUT OFF, SSR
        # physically disconnected → i_net ≈ 0, positive by convention).
        self._psu_output_on: bool = False

    def get_visa_ports(self):
        try:
            return self.rm.list_resources()
        except Exception:
            return []

    def get_com_ports(self):
        try:
            return [port.device for port in serial.tools.list_ports.comports()]
        except Exception:
            return []

    def connect_instruments(self, psu_port, load_port):
        for attr in ("psu_inst", "load_inst"):
            inst = getattr(self, attr, None)
            if inst is not None:
                try:
                    inst.close()
                except Exception:
                    pass
                setattr(self, attr, None)
        self.is_connected = False
        self.connect_error = ""

        psu  = self.rm.open_resource(psu_port)
        load = self.rm.open_resource(load_port)

        for inst in [psu, load]:
            inst.baud_rate = 9600
            inst.data_bits = 8
            inst.stop_bits = const.StopBits.one
            inst.parity = const.Parity.none
            inst.flow_control = const.ControlFlow.none
            inst.read_termination = '\n'
            inst.write_termination = '\n'
            inst.timeout = 5000

        # Verify both instruments actually respond before marking connected.
        # open_resource() succeeds on any valid port — *IDN? confirms a real instrument.
        try:
            psu_idn = psu.query("*IDN?").strip()
            logger.info("PSU IDN: %s", psu_idn)
        except Exception as e:
            try:
                psu.close()
                load.close()
            except Exception:
                pass
            msg = f"PSU ที่พอร์ต {psu_port} ไม่ตอบสนอง — เลือกพอร์ตผิดหรืออุปกรณ์ไม่พร้อม\n({e})"
            self.connect_error = msg
            raise RuntimeError(msg)

        try:
            load_idn = load.query("*IDN?").strip()
            logger.info("Load IDN: %s", load_idn)
        except Exception as e:
            try:
                psu.close()
                load.close()
            except Exception:
                pass
            msg = f"Load ที่พอร์ต {load_port} ไม่ตอบสนอง — เลือกพอร์ตผิดหรืออุปกรณ์ไม่พร้อม\n({e})"
            self.connect_error = msg
            raise RuntimeError(msg)

        self.psu_inst  = psu
        self.load_inst = load
        self.is_connected = True

        # Safe idle state after connect: ensure PSU output and Load input are OFF.
        try:
            self.psu_inst.write(":OUTP OFF")
        except Exception:
            pass
        try:
            self.load_inst.write(":INP OFF")
        except Exception:
            pass
        self._psu_output_on = False

        # NOTE: calibrate_psu_zero() is NOT called automatically here because when a
        # battery is connected the PSU already reads real battery current, not an
        # offset. Call it manually only when the output terminals are open-circuit.

    def set_psu(self, state, voltage_val="0", current_val="1.0"):
        """Manual PSU control (CV with a CC current limit).

        current_val is the CC limit (A) — a safety ceiling, not a target. It used to
        be hardcoded to 5.0 A, which could blast up to 5 A into a small/deeply-
        discharged battery on manual ON; it is now caller-supplied so the UI can pass
        a gentle limit (e.g. 0.25–1 A for recovery).
        """
        if not self.is_connected:
            return
        with self.inst_lock:
            try:
                if state:
                    self.psu_inst.write(f":VOLT {voltage_val}")
                    self.psu_inst.write(f":CURR {current_val}")
                    self.psu_inst.write(":OUTP ON")
                    self._psu_output_on = True
                else:
                    self.psu_inst.write(":OUTP OFF")
                    self._psu_output_on = False
            except Exception as e:
                logger.error(f"PSU Command Error: {e}")
        self.set_ssr(bool(state))

    def set_load(self, state, current_val="0"):
        if not self.is_connected:
            return
        with self.inst_lock:
            try:
                if state:
                    # PSU is disconnected (SSR OFF) during discharge — the load sees
                    # exactly the requested current and the battery supplies only that.
                    self.load_inst.write(f":CURR {current_val}")
                    self.load_inst.write(":INP ON")
                else:
                    self.load_inst.write(":INP OFF")
            except Exception as e:
                logger.error(f"Load Command Error: {e}")

    def set_load_raw(self, target):
        with self.inst_lock:
            try:
                self.load_inst.write(f":CURR {abs(target)}")
            except Exception as e:
                logger.error(f"set_load_raw error: {e}")

    def load_on(self):
        with self.inst_lock:
            try:
                self.load_inst.write(":INP ON")
            except Exception as e:
                logger.error(f"load_on error: {e}")

    def load_off(self):
        with self.inst_lock:
            try:
                self.load_inst.write(":INP OFF")
            except Exception as e:
                logger.error(f"load_off error: {e}")

    def psu_off(self):
        """ปิด output ของ PSU (ใช้โดย emergency shutdown + ChargeController)
        ตัด SSR (GPIO16) ตามไปด้วยเสมอ — PSU output OFF = ไม่ได้ชาร์จ = ตัดไฟ SSR"""
        with self.inst_lock:
            try:
                self.psu_inst.write(":OUTP OFF")
                self._psu_output_on = False
            except Exception as e:
                logger.error(f"psu_off error: {e}")
        self.set_ssr(False)

    def calibrate_psu_zero(self) -> float:
        """วัด current offset ของ PSU ขณะ OUTPUT OFF แล้วเก็บไว้ลบออกจากทุกการอ่าน
        ต้องเรียกหลัง connect (OUTPUT OFF อยู่แล้ว) หรือเมื่อรู้ว่าไม่มีกระแสไหลจริง
        คืนค่า offset ที่วัดได้ (A)"""
        samples = []
        with self.inst_lock:
            try:
                for _ in range(5):
                    try:
                        i = float(self.psu_inst.query("MEAS:CURR?").strip())
                        samples.append(i)
                    except Exception:
                        pass
                    time.sleep(0.1)
            except Exception as e:
                logger.error(f"calibrate_psu_zero error: {e}")
        offset = sum(samples) / len(samples) if samples else 0.0
        self._psu_current_offset = offset
        logger.info("PSU current zero-offset calibrated: %.4f A", offset)
        return offset

    def _meas_vi(self, inst, which):
        """(voltage, current) from ONE combined measurement when the instrument supports
        it (``MEAS:SCAL:ALL:DC?`` → ``V,I,P`` — measured at the same instant, single
        round-trip), else two separate ``MEAS:VOLT?``/``MEAS:CURR?`` queries. The
        capability is probed once and cached in ``which`` so an unsupported instrument
        isn't retried every sample. Caller must hold ``inst_lock``.

        A single transient VISA timeout is retried after 200 ms before propagating —
        this absorbs USB bus resets that clear on their own without killing the session."""
        cap = getattr(self, which)
        if cap is not False:                       # None (unknown) or True → try combined
            try:
                p = inst.query("MEAS:SCAL:ALL:DC?").strip().split(",")
                v, i = float(p[0]), float(p[1])
                if cap is None:
                    setattr(self, which, True)
                if which == "_psu_all":
                    i -= self._psu_current_offset
                return v, i
            except Exception:
                setattr(self, which, False)        # not supported → stop trying
        # Separate MEAS:VOLT? + MEAS:CURR? with one retry on transient VisaIOError.
        for attempt in range(2):
            try:
                v = float(inst.query("MEAS:VOLT?").strip())
                i = float(inst.query("MEAS:CURR?").strip())
                if which == "_psu_all":
                    i -= self._psu_current_offset
                return v, i
            except Exception as exc:
                if attempt == 0:
                    logger.debug("_meas_vi transient error (%s), retrying in 200 ms", exc)
                    time.sleep(0.2)
                else:
                    raise

    def read_vi(self):
        with self.inst_lock:
            # Battery terminal voltage is taken from the electronic LOAD, not the PSU.
            # The load senses terminal voltage continuously and reliably — even when
            # idle or charging — whereas the PSU reports ~0 V whenever its OUTPUT is
            # OFF (it measures the internal node after the output relay), which used to
            # make a perfectly good battery look dead at idle. The load's V and current
            # come from ONE ``MEAS:SCAL:ALL:DC?`` transaction → same instant, single
            # round-trip (aligned timestamp, fast).
            v, i_load = self._meas_vi(self.load_inst, "_load_all")
            # PSU current is still needed to see charge current: while charging the load
            # input is OFF (i_load = 0) and the current flows battery⇄PSU.
            v_psu, i_psu = self._meas_vi(self.psu_inst, "_psu_all")
            # Some e-loads return 0 V when their input is OFF (charge/rest phase).
            # Fall back to PSU terminal voltage in that case so the graph stays valid.
            if v < 1.0 and v_psu > 1.0:
                v = v_psu
            return v, i_psu, i_load

    def read_load_current(self):
        with self.inst_lock:
            try:
                return float(self.load_inst.query("MEAS:CURR?").strip())
            except Exception:
                return 0.0

    def transient_dcir_measure(self, current_target, delta_I):
        """วัด DCIR จาก transient voltage step"""
        with self.inst_lock:
            try:
                v_before = float(self.psu_inst.query("MEAS:VOLT?").strip())
                self.load_inst.write(f":CURR {abs(current_target)}")
                time.sleep(0.02)
                v_after = float(self.psu_inst.query("MEAS:VOLT?").strip())
                dcir_mohm = (abs(v_before - v_after) / abs(delta_I)) * 1000.0
                return dcir_mohm
            except Exception as e:
                logger.error(f"DCIR Transient Error: {e}")
                return 0.0

    def connect_esp32(self, port, baudrate=9600, callback=None):
        logger.info("Connecting ESP32 on %s at %d baud", port, baudrate)
        self.esp_serial = serial.Serial(port, baudrate, timeout=1)
        self.is_esp_connected = True
        self.last_esp_heartbeat = time.time()
        logger.info("ESP32 serial opened on %s", port)
        threading.Thread(
            target=self._esp_monitor_loop, args=(callback,), daemon=True
        ).start()
        # Defensive: force the relay OFF on every fresh connect, regardless of
        # whatever state it was left in by a previous session/crash. Firmware
        # already fail-safes to OFF on its own boot, but the ESP32 may still be
        # powered (not rebooted) with a stale ON state from before.
        self.set_ssr(False)

    def disconnect_esp32(self):
        # Force the relay OFF *before* marking disconnected/closing the serial port —
        # set_ssr() is a no-op once is_esp_connected is False, so this is the last
        # chance to command the relay. Otherwise SSR keeps whatever state it was in
        # (e.g. ON mid-charge) even after the operator disconnects.
        self.set_ssr(False)
        self.is_esp_connected = False
        self.ssr_state = None
        if self.esp_serial:
            try:
                self.esp_serial.close()
            except Exception:
                pass

    def set_ssr(self, state: bool) -> bool:
        """Switch the SSR safety-cutoff relay on ESP32 GPIO16 ON/OFF.

        The SSR physically gates power to the PSU + load, so this is used both
        for manual control and as a redundant hardware cutoff on E-STOP/safety
        trip — it fires even if an instrument's own SCPI output relay is stuck.
        Returns True if the command was sent, False if ESP32 isn't connected.
        """
        if not self.is_esp_connected or not self.esp_serial:
            return False
        cmd = b"SSR ON\n" if state else b"SSR OFF\n"
        with self._esp_write_lock:
            try:
                self.esp_serial.write(cmd)
                self.esp_serial.flush()
            except Exception as exc:
                logger.error("SSR command failed: %s", exc)
                return False
        self.ssr_state = bool(state)
        logger.info("SSR set to %s", "ON" if state else "OFF")
        return True

    def feed_watchdog(self) -> bool:
        """Send a PING heartbeat to the ESP32 so its firmware watchdog doesn't cut
        the SSR relay. Call this continuously (e.g. every ~1s from a UI timer) while
        connected — if this stops arriving (process killed/crashed/hung, USB
        unplugged), the ESP32 cuts power on its own after WATCHDOG_TIMEOUT_MS,
        regardless of whatever the PSU/e-load's own SCPI state still says.
        """
        if not self.is_esp_connected or not self.esp_serial:
            return False
        with self._esp_write_lock:
            try:
                self.esp_serial.write(b"PING\n")
                self.esp_serial.flush()
            except Exception as exc:
                logger.debug("Watchdog ping failed: %s", exc)
                return False
        return True

    # Ordered list of patterns tried against each serial line.
    # Each pattern must have one capture group returning the numeric temperature.
    _ESP_TEMP_PATTERNS = [
        re.compile(r"Object\s*=\s*([-+]?\d+\.?\d*)\s*\*?°?C", re.IGNORECASE),
        re.compile(r"Object\s+Temp[:\s]+([-+]?\d+\.?\d*)", re.IGNORECASE),
        re.compile(r"T_?obj[:\s]+([-+]?\d+\.?\d*)", re.IGNORECASE),
        re.compile(r"temp[:\s]+([-+]?\d+\.?\d*)", re.IGNORECASE),
    ]

    def _parse_esp_temp(self, line: str):
        """Return float temperature from a serial line, or None if not recognised."""
        for pat in self._ESP_TEMP_PATTERNS:
            m = pat.search(line)
            if m:
                return float(m.group(1))
        return None

    def _esp_monitor_loop(self, callback):
        self.last_esp_heartbeat = time.time()
        _unmatched_logged = set()   # avoid log-spamming the same unknown format
        _matched_once = False
        while self.is_esp_connected:
            try:
                if self.esp_serial.in_waiting > 0:
                    line = self.esp_serial.readline().decode('utf-8', errors='ignore').strip()
                    if not line:
                        continue
                    temp = self._parse_esp_temp(line)
                    if temp is not None:
                        if not _matched_once:
                            logger.info("ESP32 temp parsed OK (format: %r) → %.2f°C", line, temp)
                            _matched_once = True
                        self.current_temp = temp
                        self.last_esp_heartbeat = time.time()
                        if callback:
                            callback(temp)
                    else:
                        # Log unrecognised lines at WARNING (once per unique prefix)
                        key = line[:40]
                        if key not in _unmatched_logged:
                            logger.warning("ESP32 unmatched line (cannot parse temp): %r", line)
                            _unmatched_logged.add(key)
            except Exception as exc:
                logger.warning("ESP32 serial error: %s", exc)
            time.sleep(0.05)

    def shutdown_all(self):
        self.disconnect_instruments()
        self.disconnect_esp32()

    def disconnect_instruments(self):
        self.is_connected = False
        self._psu_output_on = False
        self.set_ssr(False)   # defense in depth if ESP32 stays connected independently
        with self.inst_lock:
            try:
                if self.psu_inst:
                    self.psu_inst.write(":OUTP OFF")
                    self.psu_inst.close()
            except Exception:
                pass
            try:
                if self.load_inst:
                    self.load_inst.write(":INP OFF")
                    self.load_inst.close()
            except Exception:
                pass
            self.psu_inst = None
            self.load_inst = None

    def read_measurements(self, prefer_load_v=False):
        """Return (terminal_voltage, current). Convention: discharge = positive.

        Read V and I from the instrument that is actually ACTIVE, so the terminal
        voltage is always authoritative and the idle instrument is not queried:

          * ``prefer_load_v=True`` (discharge) — V and I from the e-load. The PSU
            output is OFF; a switching PSU's ``MEAS:VOLT?`` may return 0 when off, so
            it must NOT be the voltage source during discharge. i_net = +i_load.
          * ``prefer_load_v=False`` (charge/idle) — V and I from the PSU (it is the
            active source). i_net = −i_psu.

        Each read uses a single combined ``MEAS:SCAL:ALL:DC?`` when the instrument
        supports it (V and I sampled simultaneously — important so the DCIR step isn't
        skewed — and one round-trip instead of two), else falls back to separate queries.

        NB: verify on the bench that the e-load reports the terminal voltage as
        expected (``scripts/bench_check.py``); behaviour of MEAS:VOLT? while an
        output/input is off is instrument-specific.
        """
        with self.inst_lock:
            if prefer_load_v:
                v, i_load = self._meas_vi(self.load_inst, "_load_all")
                # Discharge: PSU is disconnected (SSR OFF) → battery supplies exactly
                # the load current.
                return v, i_load
            # Charge / idle: read V from the load (it senses terminal voltage reliably even
            # when its input is OFF), fall back to PSU only if load returns near-zero (some
            # e-loads report 0 V when disconnected).  I always from PSU (only active source).
            v, _ = self._meas_vi(self.load_inst, "_load_all")
            v_psu, i_psu = self._meas_vi(self.psu_inst, "_psu_all")
            if v < 1.0 and v_psu > 1.0:
                v = v_psu
            return v, -i_psu

    def set_charge(self, state, current_val="0"):
        """Optional charge control hook for IEC cycle-life tests."""
        if not self.is_connected:
            return
        with self.inst_lock:
            try:
                if state:
                    self.psu_inst.write(f":CURR {current_val}")
                    self.psu_inst.write(":OUTP ON")
                    self._psu_output_on = True
                else:
                    self.psu_inst.write(":OUTP OFF")
                    self._psu_output_on = False
            except Exception as e:
                logger.error(f"Charge control error: {e}")
        self.set_ssr(bool(state))

    def set_psu_cccv(self, voltage, current):
        """ตั้ง PSU เป็น CC-CV: voltage = แรงดันเป้า (CV limit), current = กระแสจำกัด (CC limit)

        PSU ทำ CC↔CV ในฮาร์ดแวร์เอง: ถ้าแบตดึงกระแสถึง limit → CC ที่ current,
        เมื่อแรงดันแตะ voltage → CV ที่ voltage (กระแส taper ลง). ใช้โดย ChargeController
        (3-stage lead-acid / CC-CV lithium) — สั่งทั้งสอง limit พร้อมกันในคำสั่งเดียว
        """
        if not self.is_connected:
            return
        with self.inst_lock:
            try:
                self.psu_inst.write(f":VOLT {voltage}")
                self.psu_inst.write(f":CURR {current}")
                self.psu_inst.write(":OUTP ON")
                self._psu_output_on = True
            except Exception as e:
                logger.error(f"set_psu_cccv error: {e}")
        self.set_ssr(True)