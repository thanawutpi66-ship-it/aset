"""
Application bootstrapper for ASET Battery Characterization System
"""
import sys
import signal
import logging
import os
from typing import Optional
from contextlib import contextmanager

from config import ConfigManager
from logging_config import ASETLogger
from service_locator import ServiceLocator, ServiceProvider
from event_system import UIEventHandler
from exceptions import ASETError, ConfigurationError

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

    def create_ui(self, root):
        """Create and initialize the UI with event handling"""
        from auto_controller import AutoController
        from ui.ui_views import BatteryAppUI

        # Create event handler
        self.event_handler = UIEventHandler(root)
        self.event_handler.start()

        # Register event handler as service
        self.service_provider.register(UIEventHandler, self.event_handler)

        # Create core components
        self._create_core_components()

        # Create controller
        controller = ServiceLocator.get(AutoController)
        controller.root = root

        # Create UI
        app_ui = BatteryAppUI(root, controller)
        controller.set_ui(app_ui)

        # Attach UI callbacks to the event handler so events route to the UI
        try:
            self.event_handler.update_display = app_ui.update_display
            self.event_handler.update_status = app_ui._update_connection_status
            self.event_handler.handle_safety_trigger = app_ui.handle_safety_trigger
            self.event_handler.handle_profile_completed = app_ui.handle_profile_completed
        except Exception:
            logger.debug("Could not attach UI callbacks to event handler")

        # Offline analysis subsystem (AI grading) -> wire analyzer to controller + UI
        controller.analyzer = None
        try:
            from analysis_module import BatteryAnalyzer
            from battery_model import BatteryModel as _BatteryModel
            cfg = self.config_manager
            base_r0_mohm = _BatteryModel(
                cfg.battery.battery_type, cfg.battery.nominal_voltage,
                cfg.battery.cells_series, cfg.battery.cells_parallel,
            ).base_r0_mohm_pack
            controller.analyzer = BatteryAnalyzer(
                rated_capacity_ah=cfg.battery.rated_capacity,
                base_r0_mohm=base_r0_mohm,
                event_bus=self.event_handler.event_bus,
            )
            self.event_handler.handle_analysis_completed = app_ui.handle_analysis_completed
            logger.info("Analysis subsystem wired (BatteryAnalyzer)")
        except Exception as e:
            logger.warning(f"Analysis subsystem init failed: {e}")

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

        # Start optional web server when enabled in config
        self._web_server = None
        if config.system.enable_web_server:
            try:
                from web_server import ASETWebServer
                self._web_server = ASETWebServer(
                    config,
                    port=config.system.web_server_port,
                )
                self._web_server.start()
                logger.info(
                    "Web server started on port %s",
                    config.system.web_server_port,
                )
            except Exception as e:
                logger.warning(f"Web server failed to start: {e}")

        # Setup window close handler
        def on_closing():
            if self._confirm_shutdown():
                self.cleanup()
                root.destroy()

        root.protocol("WM_DELETE_WINDOW", on_closing)

        return app_ui

    def _create_core_components(self):
        """Create and register core application components"""
        from hardware_driver import HardwareController
        from data_utils import DataHandler
        from battery_model import BatteryModel
        from state_estimator import StateEstimator
        from auto_controller import AutoController
        from mock_hardware import MockHardwareController

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

    def _confirm_shutdown(self) -> bool:
        """Confirm application shutdown"""
        from tkinter import messagebox
        return messagebox.askokcancel("Quit", "Do you want to safely shut down the test?")

    def cleanup(self):
        """Cleanup application resources"""
        logger.info("Starting application cleanup")

        try:
            if self._web_server:
                self._web_server.stop()

            # Stop event handler
            if self.event_handler:
                self.event_handler.stop()

            # Shutdown services
            if self.service_provider:
                from auto_controller import AutoController
                # Get controller and shutdown
                try:
                    controller = ServiceLocator.get(AutoController)
                    controller.shutdown()
                except:
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

def create_application():
    """Factory function to create the application"""
    bootstrapper = ApplicationBootstrapper()

    # Initialize application
    if not bootstrapper.initialize():
        logger.error("Failed to initialize application")
        return

    try:
        import tkinter as tk

        # Create root window
        root = tk.Tk()
        root.title("🔬 ASET - Advanced Battery Characterization System v2.0")

        # Create UI
        app_ui = bootstrapper.create_ui(root)

        # Start the application
        logger.info("Entering Tk mainloop")
        root.mainloop()
        logger.info("Exited Tk mainloop")

    except Exception as e:
        logger.critical(f"Application error: {e}", exc_info=True)
    finally:
        # Cleanup after application closes
        bootstrapper.cleanup()

if __name__ == "__main__":
    create_application()
