"""Application launcher — builds the QApplication, ISA-101 window, and wires
the bootstrapper. Imported by both ``main.py`` (root shim) and
``python -m aset_batt`` (``aset_batt/__main__.py``)."""
import sys
import logging
from PySide6.QtCore import QLocale

logger = logging.getLogger(__name__)


def run() -> int:
    """Launch the integrated PySide6 GUI. Returns a process exit code."""
    try:
        from PySide6.QtWidgets import QApplication
        from aset_batt.app.app_bootstrapper import ApplicationBootstrapper
        from aset_batt.ui.isa101_views import BatteryQtWindow, QtRootShim
    except ImportError as e:
        print(f"Import error: {e}")
        print("Install dependencies first: pip install -r requirements.txt")
        return 1

    bootstrapper = ApplicationBootstrapper()
    if not bootstrapper.initialize():
        logger.error("Failed to initialize application")
        return 1

    app = QApplication(sys.argv)
    QLocale.setDefault(QLocale(QLocale.Language.English, QLocale.Country.UnitedStates))
    root = QtRootShim()
    window = BatteryQtWindow(bootstrapper.config_manager)
    try:
        bootstrapper.create_ui(root, window)
        window.show()
        logger.info("Entering Qt event loop")
        app.exec()
        logger.info("Exited Qt event loop")
    except Exception as e:
        logger.critical(f"Application error: {e}", exc_info=True)
        return 1
    finally:
        bootstrapper.cleanup()
    return 0
