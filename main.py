"""
Main entry point — ASET Universal Battery Tester (PySide6 GUI)

รัน: python main.py
ใช้ ApplicationBootstrapper (initialize / core components / cleanup) + Qt presentation:
QApplication + QtRootShim (ให้ controller/event-system เรียก root.after ได้) +
BatteryQtWindow แล้ว wire ผ่าน bootstrapper.create_ui
"""
import sys
import os
import logging

# Add current directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logger = logging.getLogger(__name__)


def main():
    """Main application entry point"""
    try:
        from app_bootstrapper import ApplicationBootstrapper
        from PySide6.QtWidgets import QApplication
        from ui.qt_views import BatteryQtWindow, QtRootShim
    except ImportError as e:
        print(f"Import error: {e}")
        print("ติดตั้ง dependencies ก่อน: pip install -r requirements.txt")
        sys.exit(1)

    bootstrapper = ApplicationBootstrapper()
    if not bootstrapper.initialize():
        logger.error("Failed to initialize application")
        sys.exit(1)

    app = QApplication(sys.argv)
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
    finally:
        bootstrapper.cleanup()


if __name__ == "__main__":
    main()
