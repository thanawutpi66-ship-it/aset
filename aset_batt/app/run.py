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
        from aset_batt.ui import theme
    except ImportError as e:
        print(f"Import error: {e}")
        print("Install dependencies first: pip install -r requirements.txt")
        return 1

    bootstrapper = ApplicationBootstrapper()
    if not bootstrapper.initialize():
        logger.error("Failed to initialize application")
        return 1

    # Must run before isa101_views is imported: widget stylesheets bake the
    # palette constants in as literal strings at construction time.
    theme.set_theme(getattr(bootstrapper.config_manager.system, "ui_theme", "light"))
    from aset_batt.ui.isa101_views import BatteryQtWindow, QtRootShim

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("aset.batterytester.app.1")
        except Exception:
            pass

    app = QApplication(sys.argv)
    QLocale.setDefault(QLocale(QLocale.Language.English, QLocale.Country.UnitedStates))
    
    import os
    from PySide6.QtGui import QIcon
    _icon_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "aset_logo.png")
    if os.path.exists(_icon_path):
        app.setWindowIcon(QIcon(_icon_path))
        
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
