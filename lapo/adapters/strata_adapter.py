"""
Resilient Strata / OpenAI-compatible HTTP & SSE adapter with:
- Automatic loopback & private LAN (e.g. MikroTik 192.168.x.x / 10.x.x.x) proxy bypass
- Optional explicit Primary -> Fallback proxy routing for remote/segmented LLM endpoints
- Explicit UTF-8 SSE streaming (reasoning_content / reasoning / thinking / inline <think> tags)
  and live tool-argument progress telemetry
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from lapo.config import (
    LOCAL_BYPASS_PROXIES,
    RuntimeConfig,
    is_private_network_url,
    resolve_active_proxy,
)
from lapo.events import EventBus, EventType
from lapo.models import ModelAvailability, ModelInfo


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def is_loopback_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").strip().lower()
        return host in LOOPBACK_HOSTS
    except Exception:
        return False


def _sanitize_error(err: Exception, api_key: str) -> str:
    text = str(err)
    if api_key and len(api_key) >= 4:
        text = text.replace(api_key, "***REDACTED***")
    return text


def _resolve_cli_binary(name: str) -> Optional[str]:
    found = shutil.which(name)
    if not found and os.name == "nt":
        found = shutil.which(f"{name}.exe") or shutil.which(f"{name}.cmd")
    return found


class _InlineThinkSplitter:
    """
    Separates <think>...</think> and <thought>...</thought> blocks streamed
    inside `delta.content` (or non-streaming `message.content`), even when
    tags are split across SSE chunk boundaries.
    """

    _OPEN_TAGS = ("<think>", "<thought>")
    _CLOSE_TAGS = ("</think>", "</thought>")

    def __init__(self) -> None:
        self.in_think = False
        self._buf = ""

    def feed(self, text: str) -> Tuple[str, str]:
        if not text:
            return "", ""
        self._buf += text
        r_out: List[str] = []
        c_out: List[str] = []

        while self._buf:
            tags = self._CLOSE_TAGS if self.in_think else self._OPEN_TAGS
            lower_buf = self._buf.lower()
            best_idx = -1
            best_tag = ""
            for tag in tags:
                idx = lower_buf.find(tag)
                if idx != -1 and (best_idx == -1 or idx < best_idx):
                    best_idx = idx
                    best_tag = tag

            if best_idx != -1:
                before = self._buf[:best_idx]
                if before:
                    (r_out if self.in_think else c_out).append(before)
                self._buf = self._buf[best_idx + len(best_tag):]
                self.in_think = not self.in_think
                continue

            # Hold back any trailing substring that could be the prefix of an opening/closing tag
            keep = 0
            max_len = max(len(t) for t in tags) - 1
            start_scan = max(0, len(lower_buf) - max_len)
            for i in range(start_scan, len(lower_buf)):
                tail = lower_buf[i:]
                if any(t.startswith(tail) for t in tags):
                    keep = len(tail)
                    break

            emit_len = len(self._buf) - keep
            if emit_len > 0:
                chunk = self._buf[:emit_len]
                (r_out if self.in_think else c_out).append(chunk)
                self._buf = self._buf[emit_len:]
            break

        return "".join(r_out), "".join(c_out)

    def flush(self) -> Tuple[str, str]:
        if not self._buf:
            return "", ""
        rem = self._buf
        self._buf = ""
        return (rem, "") if self.in_think else ("", rem)


ENGINE_STARTING = "ENGINE_STARTING"
ENGINE_READY = "ENGINE_READY"
ENGINE_REQUEST_FAILED = "ENGINE_REQUEST_FAILED"
ENGINE_RESTARTING = "ENGINE_RESTARTING"
ENGINE_TERMINATED = "ENGINE_TERMINATED"

_TERMINATED_MARKERS = (
    "engine terminated",
    "stopped restarting",
    "restart limit",
    "max restarts",
    "crashed 3 times",
    "crash limit",
    "permanently stopped",
)


class EngineTerminatedError(RuntimeError):
    """Raised when the inference engine has permanently terminated (e.g. Strata 0.1.67 3-crash cap)."""


@dataclass
class ServerInspection:
    reachable: bool
    detected_context_tokens: Optional[int] = None
    active_model_ids: List[str] = field(default_factory=list)
    supports_tokenize: bool = False
    server_metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    engine_version: Optional[str] = None
    engine_state: str = ENGINE_READY


@dataclass
class ChatCompletionResult:
    message: Dict[str, Any]
    finish_reason: str
    usage: Dict[str, Any] = field(default_factory=dict)
    timings: Dict[str, Any] = field(default_factory=dict)
    elapsed_s: float = 0.0
    retries_used: int = 0
    was_stopped: bool = False
    watchdog_triggered: bool = False
    watchdog_reason: Optional[str] = None
    engine_state: str = ENGINE_READY


class StrataAdapter:
    """
    Handles model discovery, server telemetry, exact POST /tokenize counting,
    resilient UTF-8 SSE streaming (reasoning + content + fragmented tool_calls),
    smart LAN/proxy routing, and verified model unload.
    """

    def __init__(self, config: RuntimeConfig, event_bus: Optional[EventBus] = None) -> None:
        self.config = config
        self.events = event_bus or EventBus()
        self._proxies, self.api_route_summary = self._resolve_request_proxies()

    def _resolve_request_proxies(self) -> Tuple[Optional[Dict[str, Optional[str]]], str]:
        """
        Determine exact proxy dictionary for LLM API requests:
        1. Loopback (127.0.0.1/localhost/::1): ALWAYS direct bypass.
        2. Explicit `route_api_via_proxy=True`: route via active Primary -> Fallback proxy.
        3. Private LAN (192.168.x.x / 10.x.x.x / 172.16-31.x.x / .local): direct bypass so
           OS VPN/proxy never breaks LAN/MikroTik private-IP model servers.
        4. Public/External URL with configured proxies: use active Primary -> Fallback proxy.
        """
        url = self.config.api_base_url
        if is_loopback_url(url):
            return LOCAL_BYPASS_PROXIES, "direct (loopback bypass)"

        if self.config.route_api_via_proxy:
            active, label = resolve_active_proxy(self.config.primary_proxy, self.config.fallback_proxy)
            if active:
                return {"http": active, "https": active}, f"proxy ({label}: {active})"
            return LOCAL_BYPASS_PROXIES, "direct (configured proxy unreachable)"

        if is_private_network_url(url):
            return LOCAL_BYPASS_PROXIES, "direct (private LAN / MikroTik bypass)"

        active, label = resolve_active_proxy(self.config.primary_proxy, self.config.fallback_proxy)
        if active:
            return {"http": active, "https": active}, f"proxy ({label}: {active})"

        return None, "system default"

    @property
    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _safe_get_json(self, url: str, timeout: float = 8.0) -> Optional[Any]:
        try:
            resp = requests.get(
                url,
                headers=self._headers,
                proxies=self._proxies,
                timeout=timeout,
            )
            if resp.status_code == 200 and resp.content:
                text = resp.content.decode("utf-8", errors="replace").strip()
                if text.startswith("{") or text.startswith("["):
                    return json.loads(text)
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Model Discovery (Strata CLI inventory + Live API /v1/models)
    # ------------------------------------------------------------------
    def _query_strata_cli_installed_models(self) -> Dict[str, Dict[str, Any]]:
        strata_bin = _resolve_cli_binary("strata")
        if not strata_bin:
            return {}

        installed: Dict[str, Dict[str, Any]] = {}

        try:
            proc = subprocess.run(
                [strata_bin, "models", "list", "--installed", "--output", "json"],
                shell=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10,
                encoding="utf-8",
                errors="replace",
            )
            if proc.returncode == 0 and proc.stdout.strip():
                raw = json.loads(proc.stdout)
                items = raw if isinstance(raw, list) else raw.get("models") or raw.get("data") or []
                for item in items:
                    if isinstance(item, dict):
                        mid = item.get("id") or item.get("name") or item.get("model")
                        if mid:
                            installed[str(mid)] = item
                    elif isinstance(item, str) and item.strip():
                        installed[item.strip()] = {}
                if installed:
                    return installed
        except Exception:
            pass

        try:
            proc = subprocess.run(
                [strata_bin, "models", "list", "--installed"],
                shell=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=10,
                encoding="utf-8",
                errors="replace",
            )
            if proc.returncode == 0 and proc.stdout.strip():
                for line in proc.stdout.splitlines():
                    cleaned = line.strip()
                    if not cleaned or cleaned.lower().startswith(("name", "id", "model", "---", "===")):
                        continue
                    token = re.split(r"\s{2,}|\t", cleaned)[0].strip()
                    if token and not token.startswith("-"):
                        installed[token] = {"raw_line": cleaned}
        except Exception:
            pass

        return installed

    def _query_api_models(self) -> Tuple[bool, Dict[str, Dict[str, Any]], Optional[str]]:
        url = f"{self.config.api_base_url}/models"
        try:
            resp = requests.get(
                url,
                headers=self._headers,
                proxies=self._proxies,
                timeout=min(10, self.config.connect_timeout_s),
            )
            resp.raise_for_status()
            data = json.loads(resp.content.decode("utf-8", errors="replace"))
            model_list = data.get("data") or data.get("models") or []
            api_models: Dict[str, Dict[str, Any]] = {}
            for item in model_list:
                if isinstance(item, dict):
                    mid = item.get("id") or item.get("name")
                    if mid:
                        api_models[str(mid)] = item
                elif isinstance(item, str) and item.strip():
                    api_models[item.strip()] = {}
            return True, api_models, None
        except Exception as err:
            return False, {}, _sanitize_error(err, self.config.api_key)

    @staticmethod
    def _extract_context_from_model_obj(obj: Dict[str, Any]) -> Optional[int]:
        if not isinstance(obj, dict):
            return None
        meta = obj.get("meta") if isinstance(obj.get("meta"), dict) else {}
        candidates = (
            obj.get("context_length"),
            obj.get("max_model_len"),
            obj.get("n_ctx"),
            obj.get("max_context_length"),
            meta.get("n_ctx"),
            meta.get("n_ctx_train"),
        )
        for val in candidates:
            if val is not None:
                try:
                    ival = int(val)
                    if ival > 0:
                        return ival
                except (TypeError, ValueError):
                    pass
        return None

    def discover_models(self) -> Tuple[List[ModelInfo], Optional[str]]:
        installed_map = self._query_strata_cli_installed_models()
        api_ok, api_map, api_err = self._query_api_models()

        results: List[ModelInfo] = []
        seen: set[str] = set()

        for mid, mdata in api_map.items():
            seen.add(mid)
            ctx = self._extract_context_from_model_obj(mdata)
            if mid in installed_map:
                avail = ModelAvailability.ACTIVE
                source = "strata+api"
                merged_details = {**installed_map[mid], **mdata}
                if ctx is None:
                    ctx = self._extract_context_from_model_obj(installed_map[mid])
            else:
                avail = ModelAvailability.API_ONLY
                source = "api"
                merged_details = mdata

            results.append(
                ModelInfo(
                    id=mid,
                    availability=avail,
                    context_length=ctx,
                    source=source,
                    details=merged_details,
                )
            )

        for mid, idata in installed_map.items():
            if mid in seen:
                continue
            ctx = self._extract_context_from_model_obj(idata)
            results.append(
                ModelInfo(
                    id=mid,
                    availability=ModelAvailability.INSTALLED_NOT_LOADED,
                    context_length=ctx,
                    source="strata_cli",
                    details=idata,
                )
            )

        error_msg = None if api_ok else api_err
        return results, error_msg

    # ------------------------------------------------------------------
    # Server Inspection & Exact Context Detection
    # ------------------------------------------------------------------
    def inspect_server(self) -> ServerInspection:
        metadata: Dict[str, Any] = {"api_route": self.api_route_summary}
        detected_ctx: Optional[int] = None
        reachable = False

        for props_url in (f"{self.config.server_url}/props", f"{self.config.api_base_url}/props"):
            props = self._safe_get_json(props_url)
            if isinstance(props, dict):
                reachable = True
                metadata["props"] = props
                gen_settings = props.get("default_generation_settings")
                if isinstance(gen_settings, dict) and gen_settings.get("n_ctx"):
                    try:
                        val = int(gen_settings["n_ctx"])
                        if val > 0:
                            detected_ctx = val
                    except (TypeError, ValueError):
                        pass
                if detected_ctx is None and props.get("n_ctx"):
                    try:
                        val = int(props["n_ctx"])
                        if val > 0:
                            detected_ctx = val
                    except (TypeError, ValueError):
                        pass
                break

        slots = self._safe_get_json(f"{self.config.server_url}/slots")
        if isinstance(slots, list) and slots:
            reachable = True
            metadata["slots_count"] = len(slots)
            if detected_ctx is None and isinstance(slots[0], dict) and slots[0].get("n_ctx"):
                try:
                    val = int(slots[0]["n_ctx"])
                    if val > 0:
                        detected_ctx = val
                except (TypeError, ValueError):
                    pass

        engine_version: Optional[str] = None
        status_obj: Optional[Dict[str, Any]] = None
        for status_url in (f"{self.config.api_base_url}/status", f"{self.config.server_url}/status"):
            st = self._safe_get_json(status_url)
            if isinstance(st, dict):
                reachable = True
                status_obj = st
                metadata["status"] = st
                if st.get("engine"):
                    engine_version = str(st["engine"])
                if detected_ctx is None:
                    for ctx_key in ("cache_max_tokens", "context", "max_context", "n_ctx"):
                        raw_c = st.get(ctx_key)
                        if isinstance(raw_c, int) and raw_c >= 2048:
                            detected_ctx = raw_c
                            break
                break

        api_ok, api_map, api_err = self._query_api_models()
        active_ids = list(api_map.keys())
        if api_ok:
            reachable = True
            if detected_ctx is None:
                target_obj = api_map.get(self.config.model_name) or (
                    next(iter(api_map.values())) if api_map else None
                )
                if target_obj:
                    detected_ctx = self._extract_context_from_model_obj(target_obj)

        _, tok_exact = self.count_tokens("hello")

        engine_state, state_detail = self._classify_engine_state(status_obj, reachable=reachable)
        if engine_state == ENGINE_TERMINATED and state_detail:
            metadata["engine_state_detail"] = state_detail

        return ServerInspection(
            reachable=reachable,
            detected_context_tokens=detected_ctx,
            active_model_ids=active_ids,
            supports_tokenize=tok_exact,
            server_metadata=metadata,
            error=state_detail if engine_state == ENGINE_TERMINATED else (None if reachable else (api_err or f"Cannot reach {self.config.api_base_url}")),
            engine_version=engine_version,
            engine_state=engine_state,
        )

    @staticmethod
    def _classify_engine_state(status: Optional[Dict[str, Any]], reachable: bool) -> Tuple[str, Optional[str]]:
        if not isinstance(status, dict):
            return (ENGINE_READY, None) if reachable else (ENGINE_REQUEST_FAILED, "Server unreachable")
        blob_parts = [
            str(status.get("state") or ""),
            str(status.get("status") or ""),
            str(status.get("activity") or ""),
            str(status.get("error") or ""),
            str(status.get("engine_error") or ""),
            str(status.get("service") or ""),
        ]
        blob = " ".join(blob_parts).lower()
        if any(m in blob for m in _TERMINATED_MARKERS) or status.get("terminated") is True:
            return ENGINE_TERMINATED, (str(status.get("error") or status.get("engine_error") or "Engine terminated after repeated crashes."))
        if "restarting" in blob:
            return ENGINE_RESTARTING, "Engine restarting"
        if "starting" in blob or "loading" in blob:
            return ENGINE_STARTING, "Engine starting/loading model"
        if status.get("loaded") is False and (status.get("error") or status.get("engine_error")):
            return ENGINE_TERMINATED, str(status.get("error") or status.get("engine_error"))
        return ENGINE_READY, None

    def probe_engine_state(self) -> Tuple[str, Optional[str]]:
        for status_url in (f"{self.config.api_base_url}/status", f"{self.config.server_url}/status"):
            st = self._safe_get_json(status_url, timeout=5.0)
            if isinstance(st, dict):
                return self._classify_engine_state(st, reachable=True)
        return ENGINE_REQUEST_FAILED, "Status endpoint unreachable"

    def fetch_strata_metrics(self) -> Optional[Dict[str, Any]]:
        if not getattr(self.config, "strata_telemetry_enabled", True):
            return None
        data = self._safe_get_json(f"{self.config.server_url}/metrics", timeout=6.0)
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------------
    # Exact Pre-Flight Token Counting (POST /tokenize + Fallback)
    # ------------------------------------------------------------------
    def count_tokens(self, text: str) -> Tuple[int, bool]:
        for tok_url in (f"{self.config.server_url}/tokenize", f"{self.config.api_base_url}/tokenize"):
            try:
                resp = requests.post(
                    tok_url,
                    headers=self._headers,
                    proxies=self._proxies,
                    json={"content": text, "add_special": True},
                    timeout=8,
                )
                if resp.status_code == 200 and resp.content:
                    body = resp.content.decode("utf-8", errors="replace").strip()
                    if body.startswith("{"):
                        data = json.loads(body)
                        tokens = data.get("tokens")
                        if isinstance(tokens, list):
                            return len(tokens) + 32, True
                        if isinstance(data.get("count"), int):
                            return int(data["count"]) + 32, True
            except Exception:
                pass

        return int(len(text) / 3.2) + 128, False

    def wait_until_alive(
        self,
        max_checks: int = 30,
        interval_s: float = 10.0,
        stop_event: Optional[threading.Event] = None,
    ) -> bool:
        for attempt in range(1, max_checks + 1):
            if stop_event and stop_event.is_set():
                return False
            api_ok, _, _ = self._query_api_models()
            if api_ok:
                return True
            self.events.emit(
                EventType.LOG,
                f"[RECONNECT] Waiting for server at {self.config.api_base_url} (check {attempt}/{max_checks})...",
                level="warning",
            )
            deadline = time.time() + interval_s
            while time.time() < deadline:
                if stop_event and stop_event.is_set():
                    return False
                time.sleep(0.25)
        return False

    # ------------------------------------------------------------------
    # Resilient UTF-8 SSE Streaming & Non-Streaming Fallback
    # ------------------------------------------------------------------
    def _consume_sse_stream(
        self,
        resp: requests.Response,
        start_ts: float,
        stop_event: Optional[threading.Event] = None,
        thought_watchdog_max_chars: int = 0,
    ) -> Tuple[Dict[str, Any], str, Dict[str, Any], Dict[str, Any], bool]:
        reasoning_parts: List[str] = []
        content_parts: List[str] = []
        think_splitter = _InlineThinkSplitter()
        tool_calls_map: Dict[int, Dict[str, Any]] = {}
        finish_reason = "stop"
        usage_data: Dict[str, Any] = {}
        timings_data: Dict[str, Any] = {}
        last_heartbeat = time.time()
        was_stopped = False
        r_chars_live = 0

        for raw_bytes in resp.iter_lines(decode_unicode=False):
            if stop_event and stop_event.is_set():
                was_stopped = True
                break
            if not raw_bytes:
                continue

            line = raw_bytes.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break

            try:
                chunk = json.loads(data_str)
            except Exception:
                continue

            if chunk.get("usage"):
                usage_data = chunk["usage"]
            if chunk.get("timings"):
                timings_data = chunk["timings"]

            choices = chunk.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            if choice.get("finish_reason"):
                finish_reason = str(choice["finish_reason"])

            delta = choice.get("delta") or {}

            r_delta = (
                delta.get("reasoning_content")
                or delta.get("reasoning")
                or delta.get("thinking")
            )
            if isinstance(r_delta, str) and r_delta:
                reasoning_parts.append(r_delta)
                r_chars_live += len(r_delta)
                self.events.emit(EventType.THINKING_DELTA, r_delta)

            c_delta = delta.get("content")
            if isinstance(c_delta, str) and c_delta:
                r_inline, c_clean = think_splitter.feed(c_delta)
                if r_inline:
                    reasoning_parts.append(r_inline)
                    r_chars_live += len(r_inline)
                    self.events.emit(EventType.THINKING_DELTA, r_inline)
                if c_clean:
                    content_parts.append(c_clean)
                    self.events.emit(EventType.CONTENT_DELTA, c_clean)

            if delta.get("tool_calls"):
                for tc_delta in delta["tool_calls"]:
                    idx = int(tc_delta.get("index") or 0)
                    if idx not in tool_calls_map:
                        tool_calls_map[idx] = {
                            "id": tc_delta.get("id") or f"call_{idx}_{int(time.time())}",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }
                    elif tc_delta.get("id"):
                        tool_calls_map[idx]["id"] = tc_delta["id"]

                    fn_delta = tc_delta.get("function") or {}
                    if fn_delta.get("name"):
                        tool_calls_map[idx]["function"]["name"] += fn_delta["name"]
                    if fn_delta.get("arguments"):
                        tool_calls_map[idx]["function"]["arguments"] += fn_delta["arguments"]

            if (
                thought_watchdog_max_chars > 0
                and not content_parts
                and not tool_calls_map
                and r_chars_live >= thought_watchdog_max_chars
            ):
                finish_reason = "thought_watchdog"
                self.events.emit(
                    EventType.LOG,
                    f"[WATCHDOG] Reasoning reached {r_chars_live:,} chars (limit {thought_watchdog_max_chars:,}) "
                    "with 0 output and 0 tool calls; interrupting stream cleanly to redirect model.",
                    level="warning",
                )
                try:
                    resp.close()
                except Exception:
                    pass
                break

            now = time.time()
            if now - last_heartbeat >= 20.0:
                r_chars = sum(len(p) for p in reasoning_parts)
                c_chars = sum(len(p) for p in content_parts)
                arg_chars = sum(
                    len((tc.get("function") or {}).get("arguments") or "")
                    for tc in tool_calls_map.values()
                )
                self.events.emit(
                    EventType.LOG,
                    f"  [STREAMING {now - start_ts:.0f}s] Thought: {r_chars} chars | "
                    f"Output: {c_chars} chars | ToolCalls: {len(tool_calls_map)} (Args: {arg_chars} chars)",
                )
                last_heartbeat = now

        r_tail, c_tail = think_splitter.flush()
        if r_tail:
            reasoning_parts.append(r_tail)
            self.events.emit(EventType.THINKING_DELTA, r_tail)
        if c_tail:
            content_parts.append(c_tail)
            self.events.emit(EventType.CONTENT_DELTA, c_tail)

        content_str = "".join(content_parts)
        has_tools = len(tool_calls_map) > 0
        assembled_msg: Dict[str, Any] = {
            "role": "assistant",
            "content": content_str if (content_str or not has_tools) else None,
        }
        if reasoning_parts:
            assembled_msg["reasoning_content"] = "".join(reasoning_parts)
        if has_tools:
            assembled_msg["tool_calls"] = [tool_calls_map[i] for i in sorted(tool_calls_map.keys())]

        return assembled_msg, finish_reason, usage_data, timings_data, was_stopped

    def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: int,
        tools_schema: Optional[List[Dict[str, Any]]] = None,
        stream: bool = True,
        stop_event: Optional[threading.Event] = None,
        allow_thought_watchdog: bool = True,
    ) -> ChatCompletionResult:
        include_stream_options = stream
        retries_used = 0
        last_err: Optional[Exception] = None

        for attempt in range(1, self.config.max_api_retries + 1):
            if stop_event and stop_event.is_set():
                return ChatCompletionResult(
                    message={"role": "assistant", "content": ""},
                    finish_reason="stopped",
                    was_stopped=True,
                )

            payload: Dict[str, Any] = {
                "model": self.config.model_name,
                "messages": messages,
                "temperature": self.config.temperature,
                "max_tokens": max_tokens,
                "stream": stream,
            }
            if include_stream_options and stream:
                payload["stream_options"] = {"include_usage": True}

            if tools_schema and self.config.tool_protocol in ("auto", "native"):
                payload["tools"] = tools_schema
                payload["tool_choice"] = "auto"

            start_ts = time.time()
            try:
                self.events.emit(
                    EventType.STATUS,
                    f"Requesting '{self.config.model_name}' (Attempt {attempt}/{self.config.max_api_retries}, MaxGen: {max_tokens})",
                )
                with requests.post(
                    f"{self.config.api_base_url}/chat/completions",
                    headers={
                        **self._headers,
                        "Accept": "text/event-stream, application/json",
                        "Connection": "keep-alive",
                    },
                    proxies=self._proxies,
                    json=payload,
                    stream=stream,
                    timeout=(self.config.connect_timeout_s, self.config.read_timeout_s),
                ) as resp:
                    if resp.status_code in (400, 422) and include_stream_options:
                        body_preview = resp.content[:300].decode("utf-8", errors="replace").lower()
                        if "stream_options" in body_preview or "include_usage" in body_preview:
                            include_stream_options = False
                            self.events.emit(
                                EventType.LOG,
                                "[ADAPTER] Server rejected `stream_options`; retrying without it.",
                                level="warning",
                            )
                            continue

                    resp.raise_for_status()
                    ctype = (resp.headers.get("Content-Type") or "").lower()

                    if stream and "text/event-stream" in ctype:
                        wd_limit = (
                            int(getattr(self.config, "thought_watchdog_max_chars", 0))
                            if (allow_thought_watchdog and getattr(self.config, "thought_watchdog_enabled", False))
                            else 0
                        )
                        msg, finish_reason, usage, timings, stopped = self._consume_sse_stream(
                            resp,
                            start_ts,
                            stop_event=stop_event,
                            thought_watchdog_max_chars=wd_limit,
                        )
                    else:
                        data = json.loads(resp.content.decode("utf-8", errors="replace"))
                        choice = (data.get("choices") or [{}])[0]
                        msg = choice.get("message") or {"role": "assistant", "content": ""}
                        finish_reason = str(choice.get("finish_reason") or "stop")
                        usage = data.get("usage") or {}
                        timings = data.get("timings") or {}
                        stopped = False

                        r_text = (
                            msg.get("reasoning_content")
                            or msg.get("reasoning")
                            or msg.get("thinking")
                            or ""
                        )
                        raw_content = msg.get("content") or ""
                        if not r_text and isinstance(raw_content, str) and raw_content:
                            sp = _InlineThinkSplitter()
                            r1, c1 = sp.feed(raw_content)
                            r2, c2 = sp.flush()
                            if r1 or r2:
                                r_text = r1 + r2
                                raw_content = c1 + c2
                                msg["content"] = raw_content

                        if isinstance(r_text, str) and r_text:
                            msg["reasoning_content"] = r_text
                            self.events.emit(EventType.THINKING_DELTA, r_text)
                        if isinstance(raw_content, str) and raw_content:
                            self.events.emit(EventType.CONTENT_DELTA, raw_content)

                elapsed = time.time() - start_ts
                wd_fired = finish_reason == "thought_watchdog"
                return ChatCompletionResult(
                    message=msg,
                    finish_reason=finish_reason,
                    usage=usage,
                    timings=timings,
                    elapsed_s=elapsed,
                    retries_used=retries_used,
                    was_stopped=stopped,
                    watchdog_triggered=wd_fired,
                    watchdog_reason="Reasoning exceeded watchdog limit without output/tool call" if wd_fired else None,
                    engine_state=ENGINE_READY,
                )

            except (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.HTTPError,
                json.JSONDecodeError,
            ) as err:
                elapsed = time.time() - start_ts
                last_err = err
                retries_used += 1
                safe_err = _sanitize_error(err, self.config.api_key)
                err_body = ""
                if isinstance(err, requests.exceptions.HTTPError) and err.response is not None:
                    try:
                        err_body = err.response.content[:400].decode("utf-8", errors="replace").lower()
                    except Exception:
                        err_body = ""
                if any(m in safe_err.lower() or m in err_body for m in _TERMINATED_MARKERS):
                    self.events.emit(
                        EventType.ERROR,
                        f"[ADAPTER FATAL] Engine state={ENGINE_TERMINATED}: {safe_err}",
                        level="error",
                    )
                    raise EngineTerminatedError(f"Inference engine terminated ({ENGINE_TERMINATED}): {safe_err}") from err
                eng_state, eng_detail = self.probe_engine_state()
                if eng_state == ENGINE_TERMINATED:
                    self.events.emit(
                        EventType.ERROR,
                        f"[ADAPTER FATAL] Engine state={ENGINE_TERMINATED}: {eng_detail}",
                        level="error",
                    )
                    raise EngineTerminatedError(f"Inference engine terminated ({ENGINE_TERMINATED}): {eng_detail}") from err
                self.events.emit(
                    EventType.LOG,
                    f"[ADAPTER WARNING] Request failed after {elapsed:.1f}s "
                    f"(Attempt {attempt}/{self.config.max_api_retries}, engine_state={eng_state}): {safe_err}",
                    level="warning",
                )
                if attempt < self.config.max_api_retries:
                    if stop_event and stop_event.is_set():
                        break
                    self.events.emit(
                        EventType.LOG,
                        f"[ADAPTER RETRY] Backing off {self.config.retry_backoff_s}s before retry...",
                        level="warning",
                    )
                    deadline = time.time() + self.config.retry_backoff_s
                    while time.time() < deadline:
                        if stop_event and stop_event.is_set():
                            return ChatCompletionResult(
                                message={"role": "assistant", "content": ""},
                                finish_reason="stopped",
                                retries_used=retries_used,
                                was_stopped=True,
                            )
                        time.sleep(0.25)
                    self.wait_until_alive(max_checks=15, interval_s=5.0, stop_event=stop_event)

        safe_final = _sanitize_error(last_err, self.config.api_key) if last_err else "Unknown error"
        raise RuntimeError(
            f"Model request failed after {self.config.max_api_retries} attempts: {safe_final}"
        )

    def unload_model(self, model_name: Optional[str] = None) -> Tuple[bool, str]:
        target_model = model_name or self.config.model_name
        endpoints = [
            (f"{self.config.api_base_url}/models/unload", {"model": target_model}),
            (f"{self.config.server_url}/models/unload", {"model": target_model}),
            (f"{self.config.server_url}/unload", {"model": target_model}),
        ]

        attempts_log: List[str] = []
        for url, body in endpoints:
            try:
                resp = requests.post(
                    url,
                    headers=self._headers,
                    proxies=self._proxies,
                    json=body,
                    timeout=15,
                )
                if 200 <= resp.status_code < 300:
                    return True, f"Model '{target_model}' unloaded via {url} (HTTP {resp.status_code})."
                attempts_log.append(f"{url} -> HTTP {resp.status_code}")
            except Exception as err:
                attempts_log.append(f"{url} -> {_sanitize_error(err, self.config.api_key)}")

        strata_bin = _resolve_cli_binary("strata")
        if strata_bin:
            for cmd in (
                [strata_bin, "models", "unload", target_model],
                [strata_bin, "unload", target_model],
            ):
                try:
                    proc = subprocess.run(
                        cmd,
                        shell=False,
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        text=True,
                        timeout=20,
                        encoding="utf-8",
                        errors="replace",
                    )
                    if proc.returncode == 0:
                        return True, f"Model '{target_model}' unloaded via Strata CLI: {proc.stdout.strip()}"
                    attempts_log.append(f"CLI {' '.join(cmd[1:])} -> exit {proc.returncode}")
                except Exception as err:
                    attempts_log.append(f"CLI error -> {_sanitize_error(err, self.config.api_key)}")

        detail = "; ".join(attempts_log) if attempts_log else "No unload mechanism available."
        return (
            False,
            f"Could not verify automatic unload for '{target_model}'. "
            f"Please unload manually in your server UI/CLI. Details: {detail}",
        )