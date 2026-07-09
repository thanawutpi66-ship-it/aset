"""
Custom exceptions for ASET Battery Characterization System
"""
from typing import Optional

class ASETError(Exception):
    """Base exception for ASET system"""
    pass

class HardwareError(ASETError):
    """Hardware communication errors"""
    def __init__(self, message: str, device: Optional[str] = None):
        self.device = device
        super().__init__(f"Hardware error ({device or 'unknown'}): {message}")

class SafetyError(ASETError):
    """Safety limit violations"""
    def __init__(self, message: str, parameter: Optional[str] = None, value: Optional[float] = None):
        self.parameter = parameter
        self.value = value
        details = f" ({parameter}={value})" if parameter and value is not None else ""
        super().__init__(f"Safety violation: {message}{details}")

class ConfigurationError(ASETError):
    """Configuration-related errors"""
    pass
