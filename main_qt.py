"""
Entry point สำหรับ GUI ใหม่ (PySide6) — Universal Battery Tester

ใช้ ApplicationBootstrapper เดิม (initialize / core components / cleanup) แต่สลับชั้น
presentation เป็น Qt: สร้าง QApplication + QtRootShim (ให้ controller/event-system เรียก
root.after ได้) + BatteryQtWindow แล้ว wire ผ่าน create_ui_qt

รัน: python main_qt.py   (GUI เดิม Tkinter ยังรันได้ที่ python main.py)
"""
import sys
import logging

from app_bootstrapper import ApplicationBootstrapper

logger = logging.getLogger(__name__)


def main():
    from PySide6.QtWidgets import QApplication
    from ui.qt_views import BatteryQtWindow, QtRootShim

    bootstrapper = ApplicationBootstrapper()
    if not bootstrapper.initialize():
        logger.error("Failed to initialize application")
        return 1

    app = QApplication(sys.argv)
    root = QtRootShim()
    window = BatteryQtWindow(bootstrapper.config_manager)

    try:
        bootstrapper.create_ui_qt(root, window)
        window.show()
        logger.info("Entering Qt event loop")
        app.exec()
        logger.info("Exited Qt event loop")
    except Exception as e:
        logger.critical(f"Application error: {e}", exc_info=True)
    finally:
        bootstrapper.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
