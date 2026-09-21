"""Small thread-safe owner for mutually exclusive hardware workflows.

UI text is deliberately not consulted here. A lease remains active until its
worker has completed its cleanup and the owning thread reports its exit.
"""

from dataclasses import dataclass, field
from enum import Enum
import threading
import uuid


class ApplicationState(str, Enum):
    IDLE = "IDLE"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    CANCELLING = "CANCELLING"
    CLEANUP = "CLEANUP"
    ESTOP_LATCHED = "ESTOP_LATCHED"
    FAULT = "FAULT"
    SHUTTING_DOWN = "SHUTTING_DOWN"


@dataclass
class OperationLease:
    run_id: str
    kind: str
    cancel: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None


class OperationState:
    """Application-wide exclusive workflow ownership and E-STOP latch."""

    def __init__(self):
        self._lock = threading.RLock()
        self.state = ApplicationState.IDLE
        self.active: OperationLease | None = None
        self.fault_reason = ""
        self.terminal_status = ""
        self.terminal_reason = ""

    def _set_terminal(self, status: str, reason: str):
        if not self.terminal_reason:
            self.terminal_status = status
            self.terminal_reason = reason

    def claim(self, kind: str) -> OperationLease | None:
        with self._lock:
            if self.state is not ApplicationState.IDLE or self.active is not None:
                return None
            self.terminal_status = ""
            self.terminal_reason = ""
            self.fault_reason = ""
            lease = OperationLease(uuid.uuid4().hex, kind)
            self.active = lease
            self.state = ApplicationState.STARTING
            return lease

    def running(self, lease: OperationLease) -> bool:
        with self._lock:
            if self.active is not lease:
                return False
            if self.state is ApplicationState.STARTING:
                self.state = ApplicationState.RUNNING
            return self.state is ApplicationState.RUNNING

    def request_cancel(self, lease: OperationLease) -> bool:
        with self._lock:
            if self.active is not lease:
                return False
            if self.state not in (ApplicationState.ESTOP_LATCHED,
                                  ApplicationState.SHUTTING_DOWN):
                self.state = ApplicationState.CANCELLING
            self._set_terminal("CANCELLED", "operator cancelled operation")
            lease.cancel.set()
            return True

    def cleanup(self, lease: OperationLease) -> bool:
        with self._lock:
            if self.active is not lease:
                return False
            if self.state not in (ApplicationState.ESTOP_LATCHED,
                                  ApplicationState.SHUTTING_DOWN,
                                  ApplicationState.FAULT):
                self.state = ApplicationState.CLEANUP
            return True

    def release(self, lease: OperationLease, *, fault: str = "") -> bool:
        with self._lock:
            if self.active is not lease:
                return False
            self.active = None
            if fault:
                self.fault_reason = fault
                self._set_terminal("ERROR", fault)
            if self.state not in (ApplicationState.ESTOP_LATCHED,
                                  ApplicationState.SHUTTING_DOWN):
                self.state = ApplicationState.FAULT if fault else ApplicationState.IDLE
            return True

    def latch_estop(self) -> OperationLease | None:
        with self._lock:
            self.state = ApplicationState.ESTOP_LATCHED
            self._set_terminal("ESTOP", "emergency stop")
            if self.active is not None:
                self.active.cancel.set()
            return self.active

    def begin_shutdown(self) -> OperationLease | None:
        with self._lock:
            self.state = ApplicationState.SHUTTING_DOWN
            self._set_terminal("APPLICATION_CLOSE", "application close")
            if self.active is not None:
                self.active.cancel.set()
            return self.active

    def reset_estop(self, *, workers_exited: bool, outputs_off_confirmed: bool) -> bool:
        with self._lock:
            if self.state is not ApplicationState.ESTOP_LATCHED:
                return False
            if self.active is not None or not workers_exited or not outputs_off_confirmed:
                return False
            self.fault_reason = ""
            self.terminal_status = ""
            self.terminal_reason = ""
            self.state = ApplicationState.IDLE
            return True

    @property
    def owns_hardware(self) -> bool:
        with self._lock:
            return self.active is not None
