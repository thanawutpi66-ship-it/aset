"""
Logging configuration for ASET Battery Characterization System
"""
import logging
import logging.handlers
import os
import sys
from datetime import datetime
from typing import Optional

class ASETLogger:
    """Centralized logging configuration"""

    def __init__(self, log_level: str = "INFO", log_file: Optional[str] = None):
        self.log_level = getattr(logging, log_level.upper(), logging.INFO)
        self.log_file = log_file or f"logs/aset_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

        # Create logs directory if it doesn't exist
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)

        # Configure root logger
        self._configure_logging()

    def _configure_logging(self):
        """Configure logging with both file and console handlers"""
        # Clear existing handlers
        root_logger = logging.getLogger()
        root_logger.handlers.clear()

        # Set log level
        root_logger.setLevel(self.log_level)

        # Create formatters
        file_formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s'
        )
        console_formatter = logging.Formatter(
            '%(levelname)s - %(name)s - %(message)s'
        )

        # File handler with rotation
        file_handler = logging.handlers.RotatingFileHandler(
            self.log_file,
            maxBytes=10*1024*1024,  # 10MB
            backupCount=5
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(file_formatter)

        # Console handler — force UTF-8 so Thai log messages don't crash on
        # consoles stuck in a legacy codepage (e.g. cp1252 under Thonny on Windows).
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
        console_handler = logging.StreamHandler()
        console_handler.setLevel(self.log_level)
        console_handler.setFormatter(console_formatter)

        # Add handlers to root logger
        root_logger.addHandler(file_handler)
        root_logger.addHandler(console_handler)

        # Log startup message
        logger = logging.getLogger(__name__)
        logger.info("ASET Battery Characterization System logging initialized")
        logger.info(f"Log level: {logging.getLevelName(self.log_level)}")
        logger.info(f"Log file: {self.log_file}")

def get_logger(name: str) -> logging.Logger:
    """Get a logger instance with the specified name"""
    return logging.getLogger(name)

def log_performance(func):
    """Decorator to log function performance"""
    def wrapper(*args, **kwargs):
        logger = get_logger(func.__module__)
        start_time = datetime.now()
        logger.debug(f"Starting {func.__name__}")

        try:
            result = func(*args, **kwargs)
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            logger.debug(f"Completed {func.__name__} in {duration:.3f}s")
            return result
        except Exception as e:
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()
            logger.error(f"Failed {func.__name__} after {duration:.3f}s: {e}")
            raise

    return wrapper

def log_errors(func):
    """Decorator to log exceptions"""
    def wrapper(*args, **kwargs):
        logger = get_logger(func.__module__)
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.error(f"Exception in {func.__name__}: {e}", exc_info=True)
            raise

    return wrapper