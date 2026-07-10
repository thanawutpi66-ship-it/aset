import pyvisa
import pyvisa.constants as const
import serial
import serial.tools.list_ports
import threading
import time
import re
import logging

logger = logging.getLogger(__name__)

# PEL-3111 static-mode range ceilings (Programming Manual, Mode Subsystem).
_PEL3111_CRANGE_MAX = {"LOW": 2.1, "MIDDle": 21.0, "HIGH": 210.0}
_PEL3111_VRANGE_MAX = {"LOW": 15.0, "HIGH": 150.0}


def recommend_pel3111_ranges(max_current_a: float, pack_max_voltage_v: float,
                              margin: float = 0.75) -> tuple:
    """Pick the narrowest PEL-3111 CRANge/VRANge that still leaves headroom
    above the values actually used by this battery/config, so a real transient
    never lands right at (or over) the range ceiling.

    `margin` (default 0.75) means: only pick a narrower range if the value is
    at most 75% of that range's ceiling — i.e. keep >=25% headroom. This is
    intentionally conservative — e.g. a 12V lead-acid pack (pack_max ~14.7V) is
    too close to the 15V LOW-voltage-range ceiling (only ~2% headroom) and will
    correctly fall back to HIGH, forgoing the accuracy win rather than risk an
    out-of-range clip mid-test. Only packs comfortably below a range ceiling
    get the tighter range.
    """
    if max_current_a <= margin * _PEL3111_CRANGE_MAX["LOW"]:
        i_range = "LOW"
    elif max_current_a <= margin * _PEL3111_CRANGE_MAX["MIDDle"]:
        i_range = "MIDDle"
    else:
        i_range = "HIGH"

    v_range = "LOW" if pack_max_voltage_v <= margin * _PEL3111_VRANGE_MAX["LOW"] else "HIGH"
    return i_range, v_range


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

        # Calibration offsets (from SystemConfig)
        self._psu_voltage_offset: float = 0.0
        self._psu_current_offset: float = 0.0
        self._load_voltage_offset: float = 0.0
        self._load_current_offset: float = 0.0

    def apply_calibration(self, psu_v, psu_i, load_v, load_i):
        self._psu_voltage_offset = psu_v
        self._psu_current_offset = psu_i
        self._load_voltage_offset = load_v
        self._load_current_offset = load_i

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
                except Exception as e:
                    import logging
                    logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
                setattr(self, attr, None)
        self.is_connected = False
        self.is_psu_connected = False
        self.is_load_connected = False
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
            self.is_psu_connected = True
        except Exception as e:
            try:
                psu.close()
                load.close()
            except Exception as e2:
                import logging
                logging.getLogger(__name__).error('Ignored exception: %s', e2, exc_info=True)
            msg = f"PSU ที่พอร์ต {psu_port} ไม่ตอบสนอง — เลือกพอร์ตผิดหรืออุปกรณ์ไม่พร้อม\n({e})"
            self.connect_error = msg
            self.is_connected = False
            raise RuntimeError(msg)

        try:
            load_idn = load.query("*IDN?").strip()
            logger.info("Load IDN: %s", load_idn)
            self.is_load_connected = True
        except Exception as e:
            try:
                psu.close()
                load.close()
            except Exception as e2:
                import logging
                logging.getLogger(__name__).error('Ignored exception: %s', e2, exc_info=True)
            msg = f"Load ที่พอร์ต {load_port} ไม่ตอบสนอง — เลือกพอร์ตผิดหรืออุปกรณ์ไม่พร้อม\n({e})"
            self.connect_error = msg
            self.is_connected = False
            raise RuntimeError(msg)

        self.psu_inst  = psu
        self.load_inst = load
        self.is_connected = True

        # Flush any stale entries left in each instrument's SCPI error queue from a
        # previous run/session (see _drain_error_queue docstring) — must happen
        # before apply_default_safety_protection()'s SYST:ERR? checks, or old
        # errors get misattributed to this session's protection writes.
        self._drain_error_queue(self.psu_inst, "PSU")
        self._drain_error_queue(self.load_inst, "Load")

        # Safe idle state after connect: ensure PSU output and Load input are OFF.
        try:
            self.psu_inst.write(":OUTP OFF")
        except Exception as e:
            import logging
            logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
        try:
            self.load_inst.write(":INP OFF")
        except Exception as e:
            import logging
            logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
        self._psu_output_on = False

        # NOTE: calibrate_psu_zero() is NOT called here because at this point the
        # SSR state is unknown — if it was left ON from a previous session, the
        # PSU is still electrically joined to the battery and "calibrating" would
        # just capture real battery current as if it were offset. It is called
        # instead from connect_esp32(), right after set_ssr(False) forces the SSR
        # open (PSU genuinely isolated from the battery loop at that point).

    def set_psu(self, state, voltage_val="0", current_val="1.0") -> bool:
        """Manual PSU control (CV with a CC current limit).

        current_val is the CC limit (A) — a safety ceiling, not a target. It used to
        be hardcoded to 5.0 A, which could blast up to 5 A into a small/deeply-
        discharged battery on manual ON; it is now caller-supplied so the UI can pass
        a gentle limit (e.g. 0.25–1 A for recovery).

        Returns True if the SCPI write(s) actually succeeded, False otherwise (see
        the industrial-grade audit's G9 finding: a failed write used to only be
        logged, so a caller — e.g. the manual PSU ON/OFF buttons — had no way to
        know the instrument didn't actually change state). set_ssr() is only called
        on success now too: previously it ran unconditionally, so a failed PSU write
        could leave the SSR relay state out of sync with the PSU's real output.
        """
        if not self.is_connected:
            return False
        ok = True
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
                ok = False
        if ok:
            self.set_ssr(bool(state))
        return ok

    def set_load(self, state, current_val="0") -> bool:
        """Returns True if the SCPI write(s) actually succeeded, False otherwise —
        see set_psu()'s docstring for why this matters (G9, industrial-grade audit).
        """
        if not self.is_connected:
            return False
        with self.inst_lock:
            try:
                if state:
                    # PSU is disconnected (SSR OFF) during discharge — the load sees
                    # exactly the requested current and the battery supplies only that.
                    self.load_inst.write(f":CURR {current_val}")
                    self.load_inst.write(":INP ON")
                else:
                    self.load_inst.write(":INP OFF")
                return True
            except Exception as e:
                logger.error(f"Load Command Error: {e}")
                return False

    def set_load_raw(self, target):
        with self.inst_lock:
            try:
                self.load_inst.write(f":CURR {abs(target)}")
            except Exception as e:
                logger.error(f"set_load_raw error: {e}")

    def set_load_range(self, current_range: str, voltage_range: str) -> None:
        """Set the PEL-3111 static-mode current/voltage range (CC/CR/CV/CP share
        the same range setting). Accuracy is specified as %-of-full-scale-of-
        range, so a narrower range tightens the error term substantially (e.g.
        MIDDle vs HIGH current range is a 10x tighter current-accuracy floor).
        Non-fatal: connect must never fail just because this SCPI write did."""
        if self.load_inst is None:
            return
        with self.inst_lock:
            try:
                self.load_inst.write(f":CRANge {current_range}")
                self.load_inst.write(f":VRANge {voltage_range}")
                logger.info("Load range set: CRANge=%s VRANge=%s", current_range, voltage_range)
            except Exception as e:
                logger.warning("set_load_range failed (non-fatal): %s", e)

    def _check_scpi_error(self, inst, label: str) -> str:
        """Query SYSTem:ERRor? once (verified syntax, both instruments — returns
        "<code>,\"<message>\"", code 0 = clean). A malformed/out-of-range SCPI write
        is otherwise silently dropped by the instrument with no exception on the
        PyVISA side — this is the only way to actually notice. Never raises."""
        if inst is None:
            return ""
        try:
            resp = inst.query("SYST:ERR?").strip()
            if resp.startswith("0,") or resp.startswith("+0,"):
                return ""
            logger.warning("%s SYST:ERR? -> %s", label, resp)
            return resp
        except Exception as e:
            return f"({label} error-queue check itself failed: {e})"

    def _drain_error_queue(self, inst, label: str, max_entries: int = 20) -> None:
        """Pop every pending entry out of the instrument's SCPI error queue.

        The queue is FIFO and persists on the instrument itself for as long as it
        stays powered — it is NOT reset by opening a new VISA session, so leftover
        errors from a previous run (e.g. before a protection-setup bug was fixed)
        keep surfacing on the *next* connect's SYST:ERR? check and get misread as
        freshly caused by the current session's writes. Call this once right after
        connect, before any protection/config writes, so later _check_scpi_error()
        calls only ever see errors this session actually caused."""
        if inst is None:
            return
        try:
            for _ in range(max_entries):
                resp = inst.query("SYST:ERR?").strip()
                if resp.startswith("0,") or resp.startswith("+0,"):
                    return
                logger.info("%s stale error-queue entry drained: %s", label, resp)
        except Exception as e:
            logger.debug("%s error-queue drain failed (non-fatal): %s", label, e)

    def set_load_protection(self, ocp_a: float = None, uvp_v: float = None,
                            ovp_v: float = None) -> str:
        """Set PEL-3111 hardware trip points — verified syntax:
        [:CONFigure]:OCP {<NRf>|LIMit|LOFF}, [:CONFigure]:UVP {<NRf>}.
        A backstop independent of the PC's own software safety_limits checks: this
        trips at the *instrument* even if the PC hangs/crashes. OCP mode is forced
        to LOFF (shut the load off) rather than LIMit (clamp and keep going) — a
        clamp would silently keep discharging past the requested current instead
        of stopping. Returns the SCPI error-queue message if the instrument
        rejected anything, else "". Never raises — a rejected/skipped protection
        write must not block Connect."""
        if self.load_inst is None:
            return ""
        with self.inst_lock:
            try:
                if ocp_a is not None:
                    self.load_inst.write(":CONFigure:OCP LOFF")
                    self.load_inst.write(f":CONFigure:OCP {ocp_a}")
                if uvp_v is not None:
                    self.load_inst.write(f":CONFigure:UVP {uvp_v}")
                # Note: PEL-3111 does not support :CONFigure:OVP (throws -113 Undefined header)
                # if ovp_v is not None:
                #     self.load_inst.write(f":CONFigure:OVP {ovp_v}")
                logger.info("Load protection set: OCP=%s UVP=%s", ocp_a, uvp_v)
            except Exception as e:
                logger.warning("set_load_protection failed (non-fatal): %s", e)
                return str(e)
            return self._check_scpi_error(self.load_inst, "Load")

    def set_psu_protection(self, ocp_a: float = None, ovp_v: float = None) -> str:
        """Set PSW hardware OCP/OVP trip points — verified syntax:
        [SOURce:]CURRent:PROTection[:LEVel] {<NRf>}, :STATe {ON|OFF},
        [SOURce:]VOLTage:PROTection[:LEVel] {<NRf>}. Same backstop rationale as
        set_load_protection(). Returns the SCPI error-queue message, else ""."""
        if self.psu_inst is None:
            return ""
        with self.inst_lock:
            try:
                # GW Instek PSW series requires OVP/OCP to be >= 10% of rated max.
                # For PSW80-40.5 (80V/40.5A), min OVP is 8.0V and min OCP is 4.05A.
                # OCP also cannot exceed the unit's own 40.5 A rated max output —
                # a battery's discharge-side max_current * 1.25 margin (meant for
                # the Load) can easily be higher than that and was previously sent
                # unclamped, tripping -222 "Data out of range" on every connect.
                if ocp_a is not None:
                    safe_ocp = min(max(ocp_a, 4.05), 40.5)
                    self.psu_inst.write(f":CURR:PROT:LEV {safe_ocp}")
                    self.psu_inst.write(":CURR:PROT:STAT ON")
                if ovp_v is not None:
                    safe_ovp = max(ovp_v, 8.0)
                    self.psu_inst.write(f":VOLT:PROT:LEV {safe_ovp}")
                logger.info("PSU protection set: OCP=%s OVP=%s", ocp_a, ovp_v)
            except Exception as e:
                logger.warning("set_psu_protection failed (non-fatal): %s", e)
                return str(e)
            return self._check_scpi_error(self.psu_inst, "PSU")

    def get_psu_protection_tripped(self) -> bool:
        """OUTPut:PROTection:TRIPped? — True if OVP/OCP/OTP has tripped on the PSU.
        Query it when a PSU command starts failing/timing out, to surface *why*
        instead of a generic connection-lost message."""
        if self.psu_inst is None:
            return False
        with self.inst_lock:
            try:
                return self.psu_inst.query("OUTP:PROT:TRIP?").strip().lstrip("+") == "1"
            except Exception:
                return False

    def clear_psu_protection(self) -> bool:
        """OUTPut:PROTection:CLEar — clears an OVP/OCP/OTP trip (not AC-fail, which
        the manual says cannot be cleared remotely). Deliberately a separate,
        explicitly-called method rather than something auto-retried on failure —
        a trip means something real happened; clearing it should be an operator
        decision, not silently automated."""
        if self.psu_inst is None:
            return False
        with self.inst_lock:
            try:
                self.psu_inst.write("OUTP:PROT:CLE")
                return True
            except Exception as e:
                logger.warning("clear_psu_protection failed: %s", e)
                return False

    def harden_instrument_config(self) -> None:
        """One-time defensive config applied on every connect:
        - PSU: disable auto-power-on-at-boot (SYSTem:CONFigure:OUTPut:PON) so a
          mains blip can't make it silently start outputting again on its own —
          verified: "only applied after the unit has been reset", so this is a
          no-op until the next power cycle, but harmless/cheap to send regardless.
        - Both: lock the front panel (SYSTem:KLOCk / :UTILity:REMote) so an
          operator can't hand-turn a knob mid-test and desync the instrument's
          real state from what the software believes it commanded — that class of
          bug produces no error, just silently wrong data. Released again by
          release_instrument_config() on disconnect.
        - Load: enable Short Safety (:CONFigure:SHORt:SAFety) — verified wording:
          "requires the load to already be turned on before it can be shorted" —
          and enable the load's own onboard alarm speaker (:UTILity:ALARm).
        - PSU: reset resistance emulation to 0.000Ω (:RES) — if a previous session
          left it dialed in for self_calibration_test.py and the operator now
          connects a real battery, an unexpected non-zero source resistance would
          silently corrupt every CV reading. Also sets measurement averaging to
          LOW (SENSe:AVERage:COUNt) — PSU-only, see set_psu_averaging()'s docstring
          for why this must never apply to the Load.
        Every write is independently non-fatal — connect must never fail because
        of this."""
        if self.psu_inst is not None:
            with self.inst_lock:
                for cmd in ("SYST:CONF:OUTP:PON OFF", "SYST:KLOC ON",
                            ":RES 0.000", "SENS:AVER:COUN LOW"):
                    try:
                        self.psu_inst.write(cmd)
                    except Exception as e:
                        logger.warning("PSU harden (%s) failed (non-fatal): %s", cmd, e)
        if self.load_inst is not None:
            with self.inst_lock:
                # :CONFigure:SHORt:SAFety ON — "requires the load to already be
                # turned on before it can be shorted" (verified manual wording) —
                # stops the short-circuit test function from engaging by accident.
                for cmd in (":UTIL:REM ON", ":CONFigure:SHORt:SAFety ON", ":UTIL:ALAR ON"):
                    try:
                        self.load_inst.write(cmd)
                    except Exception as e:
                        logger.warning("Load harden (%s) failed (non-fatal): %s", cmd, e)

    def release_instrument_config(self) -> None:
        """Undo harden_instrument_config()'s front-panel lock on disconnect, so the
        operator gets manual front-panel control back once the PC releases the
        instruments. Independently non-fatal."""
        if self.psu_inst is not None:
            with self.inst_lock:
                try:
                    self.psu_inst.write("SYST:KLOC OFF")
                except Exception as e:
                    logger.warning("PSU panel unlock failed (non-fatal): %s", e)
        if self.load_inst is not None:
            with self.inst_lock:
                try:
                    self.load_inst.write(":UTIL:REM OFF")
                except Exception as e:
                    logger.warning("Load panel unlock failed (non-fatal): %s", e)

    def get_instrument_info(self) -> dict:
        """Query model/serial/firmware for traceability (which exact unit/firmware
        ran a given session — useful if behavior ever differs across units or a
        firmware update). Verified syntax:
        PEL :UTILity:SYSTem? -> "MODEL,SERIAL,VERSION"
        PSW SYSTem:INFormation? -> IEEE-488.2 block data (starts with '#3212...'),
        so the PSW value is the raw response string, not parsed into fields."""
        info = {"psu": "", "load": ""}
        with self.inst_lock:
            if self.psu_inst is not None:
                try:
                    info["psu"] = self.psu_inst.query("SYST:INF?").strip()
                except Exception as e:
                    info["psu"] = f"(query failed: {e})"
            if self.load_inst is not None:
                try:
                    info["load"] = self.load_inst.query(":UTIL:SYST?").strip()
                except Exception as e:
                    info["load"] = f"(query failed: {e})"
        return info

    def apply_default_safety_protection(self, max_current_a: float, pack_max_voltage_v: float,
                                        min_voltage_v: float = 0.0) -> dict:
        """Apply the mandatory hardware-level safety backstop — PEL-3111 range
        auto-set, Load/PSU OVP/OCP/UVP protection trip limits, and instrument
        hardening (panel lock, PSU auto-power-on disable, Load short-safety) — call
        this right after connect_instruments() succeeds, from ANY entry point (the
        UI's Connect handler, a script, a test harness talking to real hardware),
        not just the GUI.

        G7 (industrial-grade audit): this setup used to live ONLY inline in
        isa101_views.py's _on_connect() handler — any OTHER way of connecting to
        real hardware got NO instrument-level backstop at all, silently. Margins
        are deliberately generous (this is a backstop against a hung/crashed PC or
        a software bug, not the primary cutoff — software safety_limits checks stay
        primary) so it doesn't nuisance-trip on normal HPPC pulses/transients.

        Every step is independently best-effort — a single SCPI write failing must
        never block using the rig, so failures are collected into "warnings"
        instead of raised.

        Returns {"info": {"psu": ..., "load": ...}, "warnings": [...]} — the
        caller decides how (or whether) to surface these.
        """
        warnings: list = []

        try:
            i_range, v_range = recommend_pel3111_ranges(max_current_a, pack_max_voltage_v)
            self.set_load_range(i_range, v_range)
        except Exception as exc:
            warnings.append(f"Load range auto-set skipped (non-fatal): {exc}")

        try:
            err = self.set_load_protection(
                ocp_a=round(max_current_a * 1.25, 2),
                uvp_v=round(min_voltage_v, 2) if min_voltage_v > 0 else None,
                ovp_v=round(pack_max_voltage_v * 1.1, 2),
            )
            if err:
                warnings.append(f"Load protection SCPI error (non-fatal): {err}")
        except Exception as exc:
            warnings.append(f"Load protection auto-set skipped (non-fatal): {exc}")

        try:
            err = self.set_psu_protection(
                ocp_a=round(max_current_a * 1.25, 2),
                ovp_v=round(pack_max_voltage_v * 1.1, 2),
            )
            if err:
                warnings.append(f"PSU protection SCPI error (non-fatal): {err}")
        except Exception as exc:
            warnings.append(f"PSU protection auto-set skipped (non-fatal): {exc}")

        try:
            self.harden_instrument_config()
        except Exception as exc:
            warnings.append(f"Instrument hardening skipped (non-fatal): {exc}")

        try:
            info = self.get_instrument_info()
        except Exception:
            info = {"psu": "", "load": ""}

        return {"info": info, "warnings": warnings}

    def beep(self, seconds: float = 1.0) -> None:
        """Audible alert on the PSU (SYSTem:BEEPer[:IMMediate] {<NR1>}, 0-3600s —
        verified syntax). PEL has no equivalent single "beep now" trigger (its
        :UTILity:ALARm only enables/disables the onboard alarm sounds, set once in
        harden_instrument_config()). Non-fatal — a failed beep must never break
        whatever alarm flow called it."""
        if self.psu_inst is None:
            return
        with self.inst_lock:
            try:
                self.psu_inst.write(f"SYST:BEEP {seconds}")
            except Exception as e:
                logger.warning("beep() failed (non-fatal): %s", e)



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
        for attempt in range(5):
            with self.inst_lock:
                if self.psu_inst:
                    try:
                        i = float(self.psu_inst.query("MEAS:CURR?").strip())
                        samples.append(i)
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
            # Sleep outside the lock to prevent starving background telemetry
            time.sleep(0.1)
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
                v, i = float(p[0]) - (self._load_voltage_offset if "load" in which else self._psu_voltage_offset), \
                       float(p[1]) - (self._load_current_offset if "load" in which else self._psu_current_offset)
                if cap is None:
                    setattr(self, which, True)
                return v, i
            except Exception:
                setattr(self, which, False)        # not supported → stop trying
        # Separate MEAS:VOLT? + MEAS:CURR? with one retry on transient VisaIOError.
        for attempt in range(2):
            try:
                v = float(inst.query("MEAS:VOLT?").strip()) - (self._load_voltage_offset if "load" in which else self._psu_voltage_offset)
                i = float(inst.query("MEAS:CURR?").strip()) - (self._load_current_offset if "load" in which else self._psu_current_offset)
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



    def transient_dcir_measure(self, current_target, delta_I):
        """วัด DCIR จาก transient voltage step"""
        with self.inst_lock:
            try:
                v_before = float(self.psu_inst.query("MEAS:VOLT?").strip()) - self._psu_voltage_offset
                self.load_inst.write(f":CURR {abs(current_target)}")
                time.sleep(0.02)
                v_after = float(self.psu_inst.query("MEAS:VOLT?").strip()) - self._psu_voltage_offset
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

        # SSR is now confirmed open → PSU is truly isolated from the battery, so
        # any current the PSU reports is pure offset (the ~0.6A PSW bleeder
        # quirk), not battery current. Safe point to auto-calibrate it out.
        if getattr(self, "psu_inst", None) is not None:
            try:
                self.calibrate_psu_zero()
            except Exception as e:
                logger.warning("calibrate_psu_zero() failed on connect: %s", e)

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
            except Exception as e:
                import logging
                logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)

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

    def temp_is_stale(self, max_age_s: float = 10.0) -> bool:
        """True if no ESP32 temperature line has been successfully parsed in the last
        ``max_age_s`` seconds. ``current_temp`` has no timestamp of its own — it just
        holds whatever the last successful parse set it to — so a serial glitch, a
        full input buffer, or the ESP32 hanging would leave it silently frozen at an
        old value with nothing distinguishing it from a fresh reading. Callers that
        make safety decisions from current_temp (OTP cutoff, Rin/OCV temperature
        compensation) should check this first."""
        if not self.is_esp_connected:
            return True
        return (time.time() - self.last_esp_heartbeat) > max_age_s

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

    def _write_off_verified(self, inst, off_cmd: str, query_cmd: str, label: str) -> bool:
        """Write an output-off command and VERIFY the instrument actually turned
        off (query echoes 0/OFF), retrying once. A single unverified write used
        to be the only thing standing between app-exit and a PSU/load left
        sourcing power on the bench: if that one write timed out (instrument
        busy mid-measurement, USB hiccup), the exception was swallowed, the log
        still said "Hardware shutdown completed", and the output stayed ON with
        nobody attached to notice. Returns True only when OFF is confirmed."""
        for attempt in (1, 2):
            try:
                inst.write(off_cmd)
                state = inst.query(query_cmd).strip().upper()
                if state in ("0", "OFF"):
                    return True
                logger.error("%s still reports %r after %s (attempt %d)",
                             label, state, off_cmd, attempt)
            except Exception as exc:
                logger.error("%s %s failed (attempt %d): %s",
                             label, off_cmd, attempt, exc)
        return False

    def disconnect_instruments(self):
        self.is_connected = False
        self._psu_output_on = False
        self.set_ssr(False)   # defense in depth if ESP32 stays connected independently
        with self.inst_lock:
            psu_off = load_off = True
            try:
                if self.psu_inst:
                    psu_off = self._write_off_verified(
                        self.psu_inst, ":OUTP OFF", ":OUTP?", "PSU")
                    self.psu_inst.close()
            except Exception as e:
                import logging
                logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
            try:
                if self.load_inst:
                    load_off = self._write_off_verified(
                        self.load_inst, ":INP OFF", ":INP?", "Load")
                    self.load_inst.close()
            except Exception as e:
                import logging
                logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)
            self.psu_inst = None
            self.load_inst = None
        if not (psu_off and load_off):
            # SSR OFF above is the physical backstop (needs ESP32 wired); scream
            # anyway so a bench operator looking at the log knows the SCPI relay
            # state could NOT be confirmed and the instrument may still be live.
            logger.critical(
                "OUTPUT-OFF NOT CONFIRMED on disconnect (PSU ok=%s, Load ok=%s) — "
                "check the bench: instrument outputs may still be enabled!",
                psu_off, load_off)

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