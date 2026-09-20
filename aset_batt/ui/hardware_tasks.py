"""Small QRunnable wrapper for blocking GUI-initiated hardware operations."""

from PySide6.QtCore import QObject, QRunnable, Signal


class HardwareTaskSignals(QObject):
    finished = Signal(str, object)


class HardwareTask(QRunnable):
    """Run one hardware callable off the GUI thread and return its result."""

    def __init__(self, name, operation):
        super().__init__()
        self.name = name
        self.operation = operation
        self.signals = HardwareTaskSignals()

    def run(self):
        try:
            result = {"ok": True, "value": self.operation()}
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        self.signals.finished.emit(self.name, result)
