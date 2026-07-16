"""
Configuration management for ASET Battery Characterization System
"""
import json
import os
import logging
from typing import Dict, Any, Optional
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

# Config-entry ceiling for BatteryConfig.harness_resistance_ohm — see the D2 comment
# in ConfigManager.validate_config(). The one calibrated value on record for this rig
# is ~0.065 Ω; 0.15 Ω leaves headroom for a genuinely longer/worse harness while still
# catching a decimal-place typo (e.g. 1.5 Ω) before it reaches grading.
HARNESS_RESISTANCE_MAX_OHM = 0.15

# Repo root (three levels up from this file: aset_batt/core/config.py -> aset_batt
# -> repo root) — anchors the DEFAULT config file location so it no longer depends
# on the process's current working directory. Previously ConfigManager()'s default
# "config.json" was a bare relative path: launching main.py from an IDE/shortcut
# whose CWD isn't the repo root made the app silently read/write a blank config in
# that other directory instead of the real one — no corruption, no error, just the
# operator's actual calibrated config never being found (and every "safety limits"
# edit landing in a config.json nobody will ever look at again). Callers that pass
# an explicit path (tests, recovery tooling) are unaffected — this only changes
# what happens when the argument is omitted.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_CONFIG_PATH = os.path.join(_REPO_ROOT, "config.json")

@dataclass
class BatteryConfig:
    """Battery configuration parameters

    แรงดัน (nominal/max/min) เป็นค่า "ต่อเซลล์"; ใช้ pack_* properties สำหรับ
    ค่าระดับแพ็คที่ scale ด้วย cells_series แล้ว (วัด/ตัดไฟที่ระดับแพ็ค)
    rated_capacity เป็นความจุ "ทั้งแพ็ค" (Ah)
    """
    battery_type: str = "LiPO"  # Changed default to LiPO
    product_name: str = ""       # ชื่อรุ่น/product (เช่น "YTZ7V") — set เมื่อ user เลือก product
    serial_number: str = ""      # Battery Serial / Batch ID
    nominal_voltage: float = 3.7  # per-cell nominal voltage
    rated_capacity: float = 2.0  # pack total capacity (Ah)
    max_voltage: float = 4.2     # per-cell max voltage
    min_voltage: float = 2.75    # per-cell min voltage (discharge cutoff)
    max_current: float = 5.0     # max discharge current (A)
    mass_grams: float = 100.0    # Battery mass for energy density calculation
    cells_series: int = 1        # จำนวนเซลล์อนุกรม (8 = 8S)
    cells_parallel: int = 1      # จำนวนเซลล์ขนาน
    temperature_compensation: bool = True
    iec61960_compliant: bool = True  # Enable IEC 61960 standard compliance
    # Test-rig cabling/contact resistance (Ω, pack-level) — purely ohmic, in series
    # with everything the rig measures, so it inflates DCIR/R0 by this fixed amount
    # regardless of the battery's real health. 0.0 = uncalibrated (no correction).
    # Calibrate once via a reference resistor, known short-circuit, or an external
    # ACIR/impedance meter at the battery terminals.
    harness_resistance_ohm: float = 0.0
    # HPPC regen (charge-direction) pulse leg (G6): skip scheduling a regen
    # pulse once live SoC is at/above this ceiling — repeatedly pushing charge
    # into an already-high pack via regen pulses is unsafe/uninformative. This
    # is a soft, SoC-based scheduling gate, independent of the hard ovp/
    # max_voltage voltage trip that still applies during whatever regen pulse
    # DOES run.
    hppc_regen_soc_ceiling_pct: float = 90.0

    @property
    def pack_nominal_voltage(self) -> float:
        return self.nominal_voltage * self.cells_series

    @property
    def pack_max_voltage(self) -> float:
        return self.max_voltage * self.cells_series

    @property
    def pack_min_voltage(self) -> float:
        return self.min_voltage * self.cells_series

@dataclass
class SystemConfig:
    """System configuration parameters"""
    max_points: int = 100
    simulation_mode: bool = False
    enable_web_server: bool = False
    csv_filepath: str = "battery_data.csv"
    log_level: str = "INFO"
    auto_backup: bool = True
    # Auto-push ขึ้น cloud dashboard (token อ่านจาก env INGEST_TOKEN / cloud_token.txt — ไม่เก็บใน config)
    cloud_push_enabled: bool = False
    cloud_dashboard_url: str = ""
    cloud_push_interval: float = 5.0
    cloud_analysis_interval: float = 60.0
    ui_theme: str = "light"  # "light" or "dark" — read once at startup, before the GUI is built
    safety_limits: Dict[str, float] = None
    # R3 (industrial-grade audit): who ran a given session — no operator identity
    # was captured anywhere before this, so a graded result could never be traced
    # back to who tested it. Plain free-text (no auth/login system — this is a
    # single-workstation lab tool, not a multi-user system), written into each
    # session's metadata sidecar (see data_utils.write_session_metadata). Empty
    # string falls back to the OS username at write time rather than at rest,
    # so it always reflects who was actually logged into Windows for that session.
    operator_name: str = ""

    def __post_init__(self):
        if self.safety_limits is None:
            self.safety_limits = {
                "max_voltage": 4.5,
                "min_voltage": 2.0,
                "max_current": 15.0,
                "max_temperature": 60.0,
                "min_temperature": -10.0
            }

@dataclass
class HardwareConfig:
    """Hardware configuration parameters"""
    psu_port: str = ""
    load_port: str = ""
    esp_port: str = ""
    visa_timeout: int = 5000
    serial_baudrate: int = 9600
    psu_v_offset: float = 0.0
    psu_i_offset: float = 0.0
    load_v_offset: float = 0.0
    load_i_offset: float = 0.0
    auto_detect_ports: bool = True

class ConfigManager:
    """Centralized configuration management"""

    def __init__(self, config_file: Optional[str] = None):
        self.config_file = config_file if config_file is not None else DEFAULT_CONFIG_PATH
        self.battery = BatteryConfig()
        self.system = SystemConfig()
        self.hardware = HardwareConfig()
        # G5 (industrial-grade audit): set when _load_config() had to fall back to
        # defaults because the file was corrupt/unreadable — a silent fallback used
        # to wipe out calibration (e.g. harness_resistance_ohm) with no indication
        # to the operator beyond a log line nobody watches during normal use. The
        # GUI launcher (aset_batt/app/run.py) checks this and shows a blocking
        # warning dialog before the main window opens.
        self.load_error: Optional[str] = None
        self._load_config()

    def _load_config(self) -> None:
        """Load configuration from file with validation"""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                # Update configurations from file
                if "battery_type" in data:
                    self._update_from_dict(self.battery, data)
                    self._update_from_dict(self.system, data)
                    self._update_from_dict(self.hardware, data)
                else:
                    self._update_from_dict(self.battery, data.get("battery", {}))
                    self._update_from_dict(self.system, data.get("system", {}))
                    self._update_from_dict(self.hardware, data.get("hardware", {}))

                logger.info(f"Configuration loaded from {self.config_file}")
            else:
                logger.warning(f"Config file {self.config_file} not found, using defaults")
                self.save_config()

        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Error loading config: {e}, using defaults")
            # Preserve the corrupt file for manual recovery instead of just
            # overwriting it — the calibration values it may still contain
            # (harness_resistance_ohm, product selection, ...) are otherwise lost
            # the instant save_config() below writes fresh defaults over it.
            try:
                if os.path.exists(self.config_file):
                    backup_path = self.config_file + ".corrupt"
                    os.replace(self.config_file, backup_path)
                    logger.error(f"Backed up unreadable config to {backup_path}")
            except OSError as backup_exc:
                logger.error(f"Could not back up corrupt config file: {backup_exc}")
            self.load_error = (
                f"config.json ไม่สามารถอ่านได้ ({e}) — ใช้ค่าเริ่มต้นแทน "
                f"(ไฟล์เดิมสำรองไว้ที่ {self.config_file}.corrupt) "
                f"กรุณาตรวจสอบค่า calibration (harness_resistance_ohm ฯลฯ) ก่อนใช้งานเทสจริง"
            )
            self.save_config()

    def _update_from_dict(self, obj: Any, data: Dict[str, Any]) -> None:
        """Update dataclass object from dictionary"""
        for key, value in data.items():
            if hasattr(obj, key):
                setattr(obj, key, value)

    def save_config(self) -> bool:
        """Save current configuration to file"""
        try:
            config_data = {
                'battery': asdict(self.battery),
                'system': asdict(self.system),
                'hardware': asdict(self.hardware)
            }

            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(config_data, f, indent=2, ensure_ascii=False)

            logger.info(f"Configuration saved to {self.config_file}")
            return True

        except IOError as e:
            logger.error(f"Error saving config: {e}")
            return False

    def validate_config(self) -> bool:
        """Validate configuration parameters"""
        errors = []

        # Battery validation
        if self.battery.rated_capacity <= 0:
            errors.append("Battery capacity must be positive")
        if self.battery.max_voltage <= self.battery.min_voltage:
            errors.append("Max voltage must be greater than min voltage")
        # G4 (industrial-grade audit): these feed pack_nominal_voltage/pack_max_voltage
        # (properties below) directly — a mistyped 0/negative cells_series or
        # cells_parallel used to pass validate_config() cleanly and silently corrupt
        # every pack-voltage-derived safety window and grading baseline from then on.
        if self.battery.cells_series <= 0:
            errors.append("cells_series must be positive")
        if self.battery.cells_parallel <= 0:
            errors.append("cells_parallel must be positive")
        if self.battery.nominal_voltage <= 0:
            errors.append("nominal_voltage must be positive")
        if self.battery.mass_grams < 0:
            errors.append("mass_grams must not be negative")
        # D2 config-entry guard (defense-in-depth pair with the runtime warn-and-skip
        # check in aset_batt.acquisition.analysis._correct_for_harness_r): a test-rig
        # cabling/contact resistance above this is not a plausible wiring value for
        # this bench (the one calibrated value on record is ~0.065 Ω) — most likely a
        # decimal-place typo, which would otherwise silently floor every DCIR/R0
        # reading and grade every pack "A".
        if self.battery.harness_resistance_ohm < 0.0:
            errors.append("harness_resistance_ohm must not be negative")
        elif self.battery.harness_resistance_ohm > HARNESS_RESISTANCE_MAX_OHM:
            errors.append(
                f"harness_resistance_ohm ({self.battery.harness_resistance_ohm:.3f} Ω) "
                f"exceeds the plausible test-rig wiring/contact resistance ceiling "
                f"({HARNESS_RESISTANCE_MAX_OHM:.2f} Ω) — check for a calibration/entry "
                f"error before trusting DCIR/R0 grading")
        if not (0.0 < self.battery.hppc_regen_soc_ceiling_pct <= 100.0):
            errors.append("hppc_regen_soc_ceiling_pct must be in (0, 100]")

        # System validation
        if self.system.max_points <= 0:
            errors.append("Max points must be positive")

        # Hardware validation
        if self.hardware.visa_timeout <= 0:
            errors.append("VISA timeout must be positive")
        if self.hardware.serial_baudrate <= 0:
            errors.append("Serial baudrate must be positive")

        if errors:
            for error in errors:
                logger.error(f"Configuration validation error: {error}")
            return False

        return True

    def get_all_config(self) -> Dict[str, Any]:
        """Get all configuration as dictionary"""
        return {
            'battery': asdict(self.battery),
            'system': asdict(self.system),
            'hardware': asdict(self.hardware)
        }

# Global configuration instance
config_manager = ConfigManager()