"""
Independent background watcher & persistent queue for mid-flight user instructions.
Monitors `Workspace/user_request.json`, consumes once atomically, and renames to
`readed_user_request_<timestamp>.json` so requests are never re-read.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from lapo.config import RuntimeConfig
from lapo.events import EventBus, EventType
from lapo.models import (
    LiveUserRequest,
    atomic_write_json,
    read_json_safe,
    utc_now_iso,
)


def publish_live_request_file(
    workspace_dir: Path,
    prompt: str,
    phase: str = "active",
    request_id: Optional[str] = None,
    wait_if_busy_s: float = 2.0,
) -> LiveUserRequest:
    """
    Atomically publish `Workspace/user_request.json` without overwriting an unconsumed request.
    Used by Desktop GUI, CLI (`lapo request`), and automated tests.
    """
    ws = Path(workspace_dir).expanduser().resolve()
    ws.mkdir(parents=True, exist_ok=True)
    target = ws / "user_request.json"

    clean_prompt = (prompt or "").strip()
    if not clean_prompt:
        raise ValueError("Live request prompt cannot be empty.")

    # Wait briefly if a running watcher is about to consume an existing file
    deadline = time.time() + max(0.0, wait_if_busy_s)
    while target.exists() and time.time() < deadline:
        time.sleep(0.1)

    if target.exists():
        raise FileExistsError(
            "An unconsumed 'user_request.json' already exists in the workspace. "
            "Refusing to overwrite it before the orchestrator consumes it."
        )

    req = LiveUserRequest(
        request_id=request_id or f"req-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}",
        phase=str(phase or "active").strip(),
        prompt=clean_prompt,
        timestamp=utc_now_iso(),
    )
    payload = {
        "request_id": req.request_id,
        "phase": req.phase,
        "prompt": req.prompt,
        "timestamp": req.timestamp,
    }
    atomic_write_json(target, payload)
    return req


class LiveRequestManager:
    """
    Runs a dedicated background watcher thread independent of model SSE streaming,
    ensuring mid-flight user instructions are detected even during long thinking streams.
    """

    def __init__(self, config: RuntimeConfig, event_bus: Optional[EventBus] = None) -> None:
        self.config = config
        self.workspace_root = config.workspace_dir.resolve()
        self.events = event_bus or EventBus()

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._requests: Dict[str, LiveUserRequest] = {}

        self.load()

    @property
    def canonical_request_file(self) -> Path:
        return self.workspace_root / "user_request.json"

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            raw = read_json_safe(self.config.pending_requests_file, default=[])
            self._requests = {}
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict) and item.get("request_id"):
                        req = LiveUserRequest.from_dict(item)
                        self._requests[req.request_id] = req

    def save(self) -> None:
        with self._lock:
            ordered = sorted(self._requests.values(), key=lambda r: r.timestamp)
            atomic_write_json(
                self.config.pending_requests_file,
                [r.to_dict() for r in ordered],
            )

    # ------------------------------------------------------------------
    # Background Watcher Lifecycle
    # ------------------------------------------------------------------
    def start_watcher(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._watch_loop,
            name="LAPO-LiveRequestWatcher",
            daemon=True,
        )
        self._thread.start()

    def stop_watcher(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None

    def _watch_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception as err:
                self.events.emit(
                    EventType.LOG,
                    f"[LIVE REQUEST WATCHER] Warning: {err}",
                    level="warning",
                )
            self._stop_event.wait(timeout=max(0.2, self.config.live_request_poll_s))

    # ------------------------------------------------------------------
    # Atomic One-Time Consumption & `readed_` Rename
    # ------------------------------------------------------------------
    def _build_readed_filename(self) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        candidate = self.workspace_root / f"readed_user_request_{stamp}.json"
        counter = 1
        while candidate.exists():
            candidate = self.workspace_root / f"readed_user_request_{stamp}_{counter}.json"
            counter += 1
        return candidate

    def poll_once(self) -> Optional[LiveUserRequest]:
        """
        Check `Workspace/user_request.json` once.
        Strictly ignores any file with the `readed_` prefix.
        """
        src = self.canonical_request_file
        if not src.is_file():
            return None

        with self._lock:
            if not src.is_file():
                return None

            # Retry briefly in case an external editor is mid-write
            parsed: Optional[Dict[str, Any]] = None
            last_err: Optional[Exception] = None
            for _ in range(5):
                try:
                    raw_text = src.read_text(encoding="utf-8").strip()
                    if not raw_text:
                        time.sleep(0.1)
                        continue
                    obj = json.loads(raw_text)
                    if isinstance(obj, dict):
                        parsed = obj
                        break
                except Exception as err:
                    last_err = err
                    time.sleep(0.1)

            if parsed is None:
                self.events.emit(
                    EventType.LOG,
                    f"[LIVE REQUEST] Could not parse user_request.json yet: {last_err}",
                    level="warning",
                )
                return None

            prompt = str(parsed.get("prompt") or "").strip()
            if not prompt:
                # Rename empty/invalid request so it does not spin forever
                renamed = self._build_readed_filename()
                os.replace(src, renamed)
                return None

            req_id = str(
                parsed.get("request_id")
                or f"req-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
            ).strip()
            phase_target = str(parsed.get("phase") or "active").strip()
            ts = str(parsed.get("timestamp") or utc_now_iso())

            # Rename source file to readed_user_request_<timestamp>.json
            readed_path = self._build_readed_filename()
            os.replace(src, readed_path)

            # Save audit copy inside .orchestrator/requests/
            self.config.requests_audit_dir.mkdir(parents=True, exist_ok=True)
            audit_copy = self.config.requests_audit_dir / readed_path.name
            try:
                shutil.copy2(readed_path, audit_copy)
            except Exception:
                pass

            # Prevent duplicate ingestion of an already-known request_id
            if req_id in self._requests:
                return self._requests[req_id]

            req = LiveUserRequest(
                request_id=req_id,
                phase=phase_target,
                prompt=prompt,
                timestamp=ts,
                source_file="user_request.json",
                archived_file=readed_path.name,
                delivered_to_model=False,
                resolved=False,
            )
            self._requests[req.request_id] = req
            self.save()

            self.events.emit(
                EventType.LIVE_REQUEST_RECEIVED,
                f"[LIVE REQUEST] Consumed '{req.request_id}' (target phase={req.phase}) -> renamed to {readed_path.name}",
                data=req.to_dict(),
                level="warning",
            )
            return req

    # ------------------------------------------------------------------
    # Phase Routing & Acknowledgment Gate
    # ------------------------------------------------------------------
    @staticmethod
    def _matches_phase(req: LiveUserRequest, active_phase_id: int) -> bool:
        target = req.phase.strip().lower()
        if target in ("active", "current", "*", ""):
            return True
        try:
            return int(target) == int(active_phase_id)
        except ValueError:
            return target == f"phase {active_phase_id}"

    def get_unresolved_for_phase(self, active_phase_id: int) -> List[LiveUserRequest]:
        self.poll_once()
        with self._lock:
            return [
                r
                for r in sorted(self._requests.values(), key=lambda x: x.timestamp)
                if not r.resolved and self._matches_phase(r, active_phase_id)
            ]

    def pop_undelivered_for_phase(self, active_phase_id: int) -> List[LiveUserRequest]:
        """Return unresolved requests for active_phase_id that haven't been injected yet, marking them delivered."""
        self.poll_once()
        with self._lock:
            batch: List[LiveUserRequest] = []
            for req in sorted(self._requests.values(), key=lambda x: x.timestamp):
                if not req.resolved and not req.delivered_to_model and self._matches_phase(req, active_phase_id):
                    req.delivered_to_model = True
                    batch.append(req)
            if batch:
                self.save()
            return batch

    def reset_delivery_flags_for_phase(self, active_phase_id: int) -> None:
        """Called after context compaction so unresolved requests are cleanly re-shown in the new handoff."""
        with self._lock:
            changed = False
            for req in self._requests.values():
                if not req.resolved and self._matches_phase(req, active_phase_id):
                    req.delivered_to_model = False
                    changed = True
            if changed:
                self.save()

    def acknowledge_requests(self, active_phase_id: int, resolved_ids: List[str]) -> Tuple[List[str], List[LiveUserRequest]]:
        """
        Mark explicitly acknowledged request IDs as resolved and return:
        (accepted_ids, still_unresolved_requests_for_active_phase).
        """
        self.poll_once()
        with self._lock:
            id_set = {str(rid).strip() for rid in (resolved_ids or []) if str(rid).strip()}
            accepted: List[str] = []

            for req in self._requests.values():
                if not req.resolved and req.request_id in id_set and self._matches_phase(req, active_phase_id):
                    req.resolved = True
                    req.resolved_at = utc_now_iso()
                    accepted.append(req.request_id)

            if accepted:
                self.save()

            remaining = [
                r
                for r in sorted(self._requests.values(), key=lambda x: x.timestamp)
                if not r.resolved and self._matches_phase(r, active_phase_id)
            ]
            return accepted, remaining

    def list_all_dicts(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                r.to_dict()
                for r in sorted(self._requests.values(), key=lambda x: x.timestamp)
            ]