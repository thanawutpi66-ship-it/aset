"""
Application bootstrapper for ASET Battery Characterization System
"""
import sys
import signal
import logging
import os
from typing import Optional
from contextlib import contextmanager

from aset_batt.core.config import ConfigManager
from aset_batt.services.logging_config import ASETLogger
from aset_batt.services.service_locator import ServiceLocator, ServiceProvider
from aset_batt.services.event_system import UIEventHandler
from aset_batt.services.exceptions import ASETError, ConfigurationError

logger = logging.getLogger(__name__)

class ApplicationBootstrapper:
    """Application bootstrapper with proper initialization and cleanup"""

    def __init__(self):
        self.config_manager: Optional[ConfigManager] = None
        self.event_handler: Optional[UIEventHandler] = None
        self.service_provider: Optional[ServiceProvider] = None
        self.logger: Optional[ASETLogger] = None
        self._initialized = False

    def initialize(self) -> bool:
        """Initialize the application with proper error handling"""
        try:
            # Initialize logging first
            self._initialize_logging()

            # Load configuration
            self._initialize_configuration()

            # Setup signal handlers
            self._setup_signal_handlers()

            # Initialize service locator
            self._initialize_services()

            self._initialized = True
            logger.info("Application initialized successfully")
            return True

        except Exception as e:
            logger.critical(f"Failed to initialize application: {e}", exc_info=True)
            self.cleanup()
            return False

    def _initialize_logging(self):
        """Initialize logging system"""
        # Get log level from environment or use default
        log_level = os.environ.get('ASET_LOG_LEVEL', 'INFO')

        # Create logs directory if needed
        os.makedirs('logs', exist_ok=True)

        self.logger = ASETLogger(log_level=log_level)
        logger.info("Logging system initialized")

    def _initialize_configuration(self):
        """Initialize configuration management"""
        self.config_manager = ConfigManager()

        if not self.config_manager.validate_config():
            raise ConfigurationError("Invalid configuration")

        logger.info("Configuration loaded and validated")

    def _setup_signal_handlers(self):
        """Setup signal handlers for graceful shutdown"""
        def signal_handler(signum, frame):
            logger.info(f"Received signal {signum}, initiating shutdown")
            self.cleanup()
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        # Handle SIGBREAK on Windows
        if hasattr(signal, 'SIGBREAK'):
            signal.signal(signal.SIGBREAK, signal_handler)

    def _initialize_services(self):
        """Initialize service locator with core services"""
        self.service_provider = ServiceProvider()

        # Register core services
        self.service_provider.register(ConfigManager, self.config_manager)

        logger.info("Core services registered")

    def _wire_runtime(self, app_ui, root, controller):
        """Wiring ที่ใช้ร่วมกัน: event callbacks, analyzer, auto-connect mock
        hardware, web server, cloud push. UI ต้องมี method ชื่อ update_display,
        _update_connection_status, handle_safety_trigger, handle_profile_completed,
        handle_analysis_completed
        """
        # Attach UI callbacks to the event handler so events route to the UI
        try:
            self.event_handler.update_display = app_ui.update_display
            self.event_handler.update_status = app_ui._update_connection_status
            self.event_handler.handle_safety_trigger = app_ui.handle_safety_trigger
            self.event_handler.handle_profile_completed = app_ui.handle_profile_completed
        except Exception:
            logger.debug("Could not attach UI callbacks to event handler")

        # Single analysis method (aset_batt.acquisition.analysis): the controller's
        # auto-analyze posts ANALYSIS_COMPLETED → this UI callback. (No separate
        # BatteryAnalyzer/ML grader is wired — one grading path for the whole app.)
        try:
            self.event_handler.handle_analysis_completed = app_ui.handle_analysis_completed
        except Exception as e:
            logger.warning(f"Analysis routing init failed: {e}")

        # Auto-connect mock hardware in simulation mode
        config = self.config_manager
        if config.system.simulation_mode:
            try:
                visa = controller.hw.get_visa_ports()
                if len(visa) >= 2:
                    controller.hw.connect_instruments(visa[0], visa[1])
                    coms = controller.hw.get_com_ports()
                    if coms:
                        controller.hw.connect_esp32(coms[0])
                    app_ui._update_connection_status()
                    logger.info("Auto-connected mock hardware (simulation mode)")
            except Exception as e:
                logger.warning(f"Simulation auto-connect failed: {e}")

        # Local web server removed — cloud dashboard is the primary interface
        self._web_server = None

        # Auto-push ขึ้น cloud dashboard (ถ้าเปิดใน config + มี token) — delegate ไปที่
        # window._cloud_push_start() (single source of truth) กัน double-push/duplicate
        # sessions ที่จะเกิดถ้ามีทั้ง bootstrapper และ GUI สร้าง CloudPusher คนละตัว
        self._window = app_ui
        try:
            app_ui._cloud_push_start()
        except Exception as e:
            logger.warning(f"Cloud auto-push init failed: {e}")

    def create_ui(self, root, window):
        """สร้าง event handler + core components + wire Qt window เข้ากับ controller
        (root = QtRootShim สำหรับ marshaling cross-thread แทน Tk root)"""
        from aset_batt.app.auto_controller import AutoController

        self.event_handler = UIEventHandler(root)
        self.event_handler.start()
        self.service_provider.register(UIEventHandler, self.event_handler)

        self._create_core_components()

        controller = ServiceLocator.get(AutoController)
        controller.root = root

        window.bind_controller(controller)
        controller.set_ui(window)

        self._wire_runtime(window, root, controller)
        return window

    def _create_core_components(self):
        """Create and register core application components"""
        from aset_batt.hardware.hardware_driver import HardwareController
        from aset_batt.storage.data_utils import DataHandler
        from aset_batt.core.battery_model import BatteryModel
        from aset_batt.core.state_estimator import StateEstimator
        from aset_batt.app.auto_controller import AutoController
        from aset_batt.hardware.mock_hardware import MockHardwareController

        config = self.config_manager

        # Create hardware controller
        if config.system.simulation_mode:
            hw = MockHardwareController()
            logger.info("Using mock hardware controller (simulation mode)")
        else:
            hw = HardwareController()
            logger.info("Using real hardware controller")

        # Create other components
        data = DataHandler()
        battery_model = BatteryModel(
            battery_type=config.battery.battery_type,
            nominal_voltage=config.battery.nominal_voltage,
            series_cells=config.battery.cells_series,
            parallel_cells=config.battery.cells_parallel,
        )
        estimator = StateEstimator(config.battery.rated_capacity, battery_model)
        controller = AutoController(None, hw, data, estimator, config)

        # Register services
        self.service_provider.register(HardwareController, hw)
        self.service_provider.register(DataHandler, data)
        self.service_provider.register(BatteryModel, battery_model)
        self.service_provider.register(StateEstimator, estimator)
        self.service_provider.register(AutoController, controller)

    def cleanup(self):
        """Cleanup application resources"""
        logger.info("Starting application cleanup")

        try:
            window = getattr(self, "_window", None)
            if window is not None:
                window._cloud_push_stop()

            # Stop event handler
            if self.event_handler:
                self.event_handler.stop()

            # Shutdown services
            if self.service_provider:
                from aset_batt.app.auto_controller import AutoController
                # Get controller and shutdown
                try:
                    controller = ServiceLocator.get(AutoController)
                    controller.shutdown()
                except Exception:
                    pass  # Controller might not be registered yet

                # Clear all services
                ServiceLocator.clear()

            logger.info("Application cleanup completed")

        except Exception as e:
            logger.error(f"Error during cleanup: {e}", exc_info=True)

    @contextmanager
    def application_context(self):
        """Context manager for application lifecycle"""
        try:
            if not self.initialize():
                raise ASETError("Failed to initialize application")
            yield self
        finally:
            self.cleanup()
