"""Resilient Strata / OpenAI-compatible HTTP & SSE adapter with loopback proxy bypass."""

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

from lapo.config import LOCAL_BYPASS_PROXIES, RuntimeConfig
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


@dataclass
class ServerInspection:
    reachable: bool
    detected_context_tokens: Optional[int] = None
    active_model_ids: List[str] = field(default_factory=list)
    supports_tokenize: bool = False
    server_metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class ChatCompletionResult:
    message: Dict[str, Any]
    finish_reason: str
    usage: Dict[str, Any] = field(default_factory=dict)
    timings: Dict[str, Any] = field(default_factory=dict)
    elapsed_s: float = 0.0
    retries_used: int = 0
    was_stopped: bool = False


class StrataAdapter:
    """
    Handles model discovery, server telemetry, exact POST /tokenize counting,
    resilient UTF-8 SSE streaming (reasoning + content + fragmented tool_calls),
    and verified model unload.
    """

    def __init__(self, config: RuntimeConfig, event_bus: Optional[EventBus] = None) -> None:
        self.config = config
        self.events = event_bus or EventBus()
        self._proxies = LOCAL_BYPASS_PROXIES if is_loopback_url(config.api_base_url) else None

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
        """Query `strata models list --installed --output json` with plain-text fallback."""
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
        metadata: Dict[str, Any] = {}
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

        status = self._safe_get_json(f"{self.config.server_url}/status")
        if isinstance(status, dict):
            reachable = True
            metadata["status"] = status

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

        return ServerInspection(
            reachable=reachable,
            detected_context_tokens=detected_ctx,
            active_model_ids=active_ids,
            supports_tokenize=tok_exact,
            server_metadata=metadata,
            error=None if reachable else (api_err or f"Cannot reach {self.config.api_base_url}"),
        )

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
    ) -> Tuple[Dict[str, Any], str, Dict[str, Any], Dict[str, Any], bool]:
        reasoning_parts: List[str] = []
        content_parts: List[str] = []
        tool_calls_map: Dict[int, Dict[str, Any]] = {}
        finish_reason = "stop"
        usage_data: Dict[str, Any] = {}
        timings_data: Dict[str, Any] = {}
        last_heartbeat = time.time()
        was_stopped = False

        # Always decode raw bytes explicitly as UTF-8
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

            r_delta = delta.get("reasoning_content") or delta.get("reasoning")
            if r_delta:
                reasoning_parts.append(r_delta)
                self.events.emit(EventType.THINKING_DELTA, r_delta)

            c_delta = delta.get("content")
            if c_delta:
                content_parts.append(c_delta)
                self.events.emit(EventType.CONTENT_DELTA, c_delta)

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
                        msg, finish_reason, usage, timings, stopped = self._consume_sse_stream(
                            resp, start_ts, stop_event=stop_event
                        )
                    else:
                        data = json.loads(resp.content.decode("utf-8", errors="replace"))
                        choice = (data.get("choices") or [{}])[0]
                        msg = choice.get("message") or {"role": "assistant", "content": ""}
                        finish_reason = str(choice.get("finish_reason") or "stop")
                        usage = data.get("usage") or {}
                        timings = data.get("timings") or {}
                        stopped = False

                        r_text = msg.get("reasoning_content") or msg.get("reasoning")
                        if r_text:
                            msg["reasoning_content"] = r_text
                            self.events.emit(EventType.THINKING_DELTA, r_text)
                        if msg.get("content"):
                            self.events.emit(EventType.CONTENT_DELTA, msg["content"])

                elapsed = time.time() - start_ts
                return ChatCompletionResult(
                    message=msg,
                    finish_reason=finish_reason,
                    usage=usage,
                    timings=timings,
                    elapsed_s=elapsed,
                    retries_used=retries_used,
                    was_stopped=stopped,
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
                self.events.emit(
                    EventType.LOG,
                    f"[ADAPTER WARNING] Request failed after {elapsed:.1f}s "
                    f"(Attempt {attempt}/{self.config.max_api_retries}): {safe_err}",
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