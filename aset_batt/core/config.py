"""
Configuration management for ASET Battery Characterization System
"""
import json
import os
import logging
from typing import Dict, Any, Optional
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

@dataclass
class BatteryConfig:
    """Battery configuration parameters

    แรงดัน (nominal/max/min) เป็นค่า "ต่อเซลล์"; ใช้ pack_* properties สำหรับ
    ค่าระดับแพ็คที่ scale ด้วย cells_series แล้ว (วัด/ตัดไฟที่ระดับแพ็ค)
    rated_capacity เป็นความจุ "ทั้งแพ็ค" (Ah)
    """
    battery_type: str = "LiPO"  # Changed default to LiPO
    product_name: str = ""       # ชื่อรุ่น/product (เช่น "YTZ7V") — set เมื่อ user เลือก product
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
    safety_limits: Dict[str, float] = None

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
    auto_detect_ports: bool = True

class ConfigManager:
    """Centralized configuration management"""

    def __init__(self, config_file: str = "config.json"):
        self.config_file = config_file
        self.battery = BatteryConfig()
        self.system = SystemConfig()
        self.hardware = HardwareConfig()
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