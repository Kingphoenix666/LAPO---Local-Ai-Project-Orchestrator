"""Thread-safe EventBus connecting the shared orchestration core to GUI and CLI."""

from __future__ import annotations

import queue
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List

from lapo.models import utc_now_iso


class EventType(str, Enum):
    LOG = "log"
    STATUS = "status"
    THINKING_DELTA = "thinking_delta"
    CONTENT_DELTA = "content_delta"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    INLINE_FILE_WRITTEN = "inline_file_written"
    CONTEXT_UPDATE = "context_update"
    PHASE_CHANGED = "phase_changed"
    VERIFICATION_RESULT = "verification_result"
    LOCK_UPDATED = "lock_updated"
    LIVE_REQUEST_RECEIVED = "live_request_received"
    COMPACTION_STARTED = "compaction_started"
    COMPACTION_COMPLETED = "compaction_completed"
    SESSION_COMPLETED = "session_completed"
    ERROR = "error"


@dataclass
class OrchestratorEvent:
    type: EventType
    message: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    level: str = "info"  # "info" | "warning" | "error" | "success"
    timestamp: str = field(default_factory=utc_now_iso)


EventSubscriber = Callable[[OrchestratorEvent], None]


class EventBus:
    """
    Delivers events from background worker threads safely to consumers.
    - Desktop GUI polls `drain()` from Tkinter's main event loop (`root.after`).
    - CLI can register direct synchronous subscribers via `subscribe()`.
    """

    def __init__(self, max_queue_size: int = 10000) -> None:
        self._queue: "queue.Queue[OrchestratorEvent]" = queue.Queue(maxsize=max_queue_size)
        self._subscribers: List[EventSubscriber] = []

    def subscribe(self, callback: EventSubscriber) -> None:
        self._subscribers.append(callback)

    def emit(
        self,
        event_type: EventType,
        message: str = "",
        data: Dict[str, Any] | None = None,
        level: str = "info",
    ) -> None:
        evt = OrchestratorEvent(
            type=event_type,
            message=message,
            data=data or {},
            level=level,
        )
        try:
            self._queue.put_nowait(evt)
        except queue.Full:
            # Drop oldest delta event to prevent unbounded memory growth under heavy streams
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(evt)
            except queue.Empty:
                pass

        for sub in self._subscribers:
            try:
                sub(evt)
            except Exception:
                pass

    def drain(self, max_items: int = 200) -> List[OrchestratorEvent]:
        items: List[OrchestratorEvent] = []
        for _ in range(max_items):
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return items