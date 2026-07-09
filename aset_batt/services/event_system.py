"""
Thread-safe event system for UI communication
"""
import threading
import queue
import time
from typing import Any, Callable, Optional
from enum import Enum

class EventType(Enum):
    """Event types for UI communication"""
    UPDATE_DISPLAY = "update_display"
    UPDATE_STATUS = "update_status"
    SHOW_MESSAGE = "show_message"
    UPDATE_PROGRESS = "update_progress"
    SAFETY_TRIGGERED = "safety_triggered"
    PROFILE_COMPLETED = "profile_completed"
    CONNECTION_CHANGED = "connection_changed"
    ANALYSIS_COMPLETED = "analysis_completed"

class Event:
    """Event object for thread-safe communication"""
    def __init__(self, event_type: EventType, data: Any = None, callback: Optional[Callable] = None):
        self.event_type = event_type
        self.data = data
        self.callback = callback
        self.timestamp = time.time()

class EventBus:
    """Thread-safe event bus for UI communication"""

    def __init__(self):
        self._queue = queue.Queue()
        self._listeners = {}
        self._running = False
        self._thread = None
        self._lock = threading.RLock()

    def start(self):
        """Start the event processing thread"""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._process_events, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the event processing thread"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)

    def post_event(self, event: Event):
        """Post an event to the queue"""
        self._queue.put(event)

    def add_listener(self, event_type: EventType, callback: Callable):
        """Add an event listener"""
        with self._lock:
            if event_type not in self._listeners:
                self._listeners[event_type] = []
            self._listeners[event_type].append(callback)

    def remove_listener(self, event_type: EventType, callback: Callable):
        """Remove an event listener"""
        with self._lock:
            if event_type in self._listeners:
                self._listeners[event_type].remove(callback)
                if not self._listeners[event_type]:
                    del self._listeners[event_type]

    def _process_events(self):
        """Process events from the queue"""
        while self._running:
            try:
                event = self._queue.get(timeout=0.1)

                # Notify listeners
                with self._lock:
                    if event.event_type in self._listeners:
                        for callback in self._listeners[event.event_type]:
                            try:
                                callback(event)
                            except Exception as e:
                                # Log error but continue processing other listeners
                                import logging
                                logging.getLogger(__name__).error(f"Event callback error: {e}")

                # Execute callback if provided
                if event.callback:
                    try:
                        event.callback()
                    except Exception as e:
                        import logging
                        logging.getLogger(__name__).error(f"Event callback error: {e}")

                self._queue.task_done()

            except queue.Empty:
                continue
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Event processing error: {e}")

class UIEventHandler:
    """Thread-safe event handler bridging AutoController (background threads)
    to the PySide6 UI. Handler methods (update_display, show_message, etc.) are
    monkey-patched onto instances of this class from the real Qt window — see
    app_bootstrapper._wire_runtime."""

    def __init__(self, root):
        self.root = root
        self.event_bus = EventBus()
        self._setup_event_handlers()

    def _setup_event_handlers(self):
        """Setup event handlers for common UI events"""
        # Update display event
        self.event_bus.add_listener(EventType.UPDATE_DISPLAY, self._handle_update_display)

        # Update status event
        self.event_bus.add_listener(EventType.UPDATE_STATUS, self._handle_update_status)

        # Show message event
        self.event_bus.add_listener(EventType.SHOW_MESSAGE, self._handle_show_message)

        # Safety triggered event
        self.event_bus.add_listener(EventType.SAFETY_TRIGGERED, self._handle_safety_triggered)

        # Profile completed event
        self.event_bus.add_listener(EventType.PROFILE_COMPLETED, self._handle_profile_completed)

        # Analysis completed event
        self.event_bus.add_listener(EventType.ANALYSIS_COMPLETED, self._handle_analysis_completed)

    def _handle_update_display(self, event: Event):
        """Handle display update events.

        หมายเหตุ: เส้นทางหลักคือ AutoController เรียก ui.update_display ตรงผ่าน
        root.after; handler นี้รองรับเผื่อมีการ post UPDATE_DISPLAY event โดยส่งผ่าน
        อาร์กิวเมนต์ทั้งหมด (ไม่ fix 4 ตัว → ตรงกับ signature ปัจจุบัน v,i,soc,rin,temp,soh)
        """
        if hasattr(self, 'update_display') and isinstance(event.data, (list, tuple)):
            data = tuple(event.data)
            self.root.after(0, lambda: self.update_display(*data))

    def _handle_update_status(self, event: Event):
        """Handle status update events"""
        if hasattr(self, 'update_status_bar'):
            self.root.after(0, self.update_status_bar)

    def _handle_show_message(self, event: Event):
        """Handle message display events — routed to the real UI's show_message
        (wired in app_bootstrapper._wire_runtime), same hasattr-guarded pattern
        as the other handlers above. No fallback: this app is PySide6-only, and
        a fallback that "shows something anyway" (e.g. tkinter) previously
        masked the fact that safety-relevant messages weren't reaching the
        operator at all — better to be visibly unwired than silently swallowed."""
        title, message, msg_type = event.data
        if hasattr(self, 'show_message'):
            self.root.after(0, self.show_message, title, message, msg_type)

    def _handle_safety_triggered(self, event: Event):
        """Handle safety trigger events"""
        if hasattr(self, 'handle_safety_trigger'):
            self.root.after(0, self.handle_safety_trigger, event.data)

    def _handle_profile_completed(self, event: Event):
        """Handle profile completion events"""
        if hasattr(self, 'handle_profile_completed'):
            self.root.after(0, self.handle_profile_completed, event.data)

    def _handle_analysis_completed(self, event: Event):
        """Handle analysis (AI grading) completion events"""
        if hasattr(self, 'handle_analysis_completed'):
            self.root.after(0, self.handle_analysis_completed, event.data)

    def start(self):
        """Start the event bus"""
        self.event_bus.start()

    def stop(self):
        """Stop the event bus"""
        self.event_bus.stop()

    def post_event(self, event_type: EventType, data: Any = None, callback: Optional[Callable] = None):
        """Post an event to the bus"""
        event = Event(event_type, data, callback)
        self.event_bus.post_event(event)