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
        logger.error(f"Import error: {e}")
        logger.error("Install dependencies first: pip install -r requirements.txt")
        return 1

    bootstrapper = ApplicationBootstrapper()
    if not bootstrapper.initialize():
        logger.error("Failed to initialize application")
        return 1

    # Must run before isa101_views is imported so the very first widgets built
    # already read the right theme.* constants (retheme() can change them
    # live afterwards, but the initial construction still needs a starting
    # palette to build from).
    ui_theme = getattr(bootstrapper.config_manager.system, "ui_theme", "light")
    theme.set_theme(ui_theme)
    from aset_batt.ui.isa101_views import BatteryQtWindow, QtRootShim

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("aset.batterytester.app.1")
        except Exception as e:
            import logging
            logging.getLogger(__name__).error('Ignored exception: %s', e, exc_info=True)

    app = QApplication(sys.argv)
    QLocale.setDefault(QLocale(QLocale.Language.English, QLocale.Country.UnitedStates))

    # Signal-delivery wake-up: ระหว่าง app.exec() คอนโทรลอยู่ที่ event loop ฝั่ง C++
    # ของ Qt — Python จะประมวลผล SIGINT/SIGBREAK ที่ค้างอยู่ได้ก็ต่อเมื่อได้กลับมารัน
    # bytecode ของตัวเอง ถ้าไม่มี timer นี้ การกด Ctrl+C หรือปุ่ม Stop ของ IDE
    # (Thonny/VS Code) จะไปไม่ถึง handler ใน app_bootstrapper เลย → IDE รอไม่ไหว
    # แล้ว force-kill โปรเซส → PSU/Load ค้างสถานะเดิมโดยไม่มีการตัดไฟ
    from PySide6.QtCore import QTimer
    _signal_wakeup = QTimer()
    _signal_wakeup.timeout.connect(lambda: None)   # no-op — แค่ปลุก interpreter
    _signal_wakeup.start(200)

    try:
        # Routed through theme.get_material_stylesheet() (not qt_material.apply_
        # stylesheet() directly) so the built CSS is cached — if the user later
        # toggles back to this theme via _on_theme_toggle, it's instant instead
        # of re-paying the ~0.5-0.8s Jinja2 build cost.
        app.setStyle("Fusion")
        app.setStyleSheet(theme.get_material_stylesheet(ui_theme))
    except ImportError:
        logger.warning("qt-material not installed — falling back to the built-in "
                        "stylesheet only. Install it with: pip install qt-material")

    import os
    from PySide6.QtGui import QIcon
    _icon_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "aset_logo.png")
    if os.path.exists(_icon_path):
        app.setWindowIcon(QIcon(_icon_path))
        
    root = QtRootShim()
    window = BatteryQtWindow(bootstrapper.config_manager)
    # G5 (industrial-grade audit): config.json being corrupt used to silently fall
    # back to defaults (wiping calibration like harness_resistance_ohm) with only a
    # log line most operators never see. Surface it loudly before the main window
    # opens so it can't be missed and mistaken for a freshly-calibrated rig.
    if getattr(bootstrapper.config_manager, "load_error", None):
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(None, "Configuration Error", bootstrapper.config_manager.load_error)
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
