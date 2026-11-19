"""
Deterministic local HTTP/SSE mock server emulating Strata / llama.cpp / OpenAI-compatible APIs.
Records every incoming payload so integration tests can assert exact phase handoffs,
KV-cache-preserving tool schemas during compaction, UTF-8 SSE decoding, and unload calls.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Union


ScriptedReply = Union[Dict[str, Any], Callable[[Dict[str, Any]], Dict[str, Any]]]


class MockStrataServer:
    """Thread-safe local HTTP server bound to an ephemeral loopback port."""

    def __init__(
        self,
        model_id: str = "mock-coder-32b",
        n_ctx: int = 8192,
        enable_tokenize: bool = True,
        unload_succeeds: bool = True,
    ) -> None:
        self.model_id = model_id
        self.n_ctx = n_ctx
        self.enable_tokenize = enable_tokenize
        self.unload_succeeds = unload_succeeds

        self.scripted_replies: List[ScriptedReply] = []
        self.recorded_chat_requests: List[Dict[str, Any]] = []
        self.recorded_unload_requests: List[Dict[str, Any]] = []
        self.recorded_tokenize_requests: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

        parent = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                # Silence HTTP access logs during unittest runs
                return

            def _send_json(self, status: int, obj: Any) -> None:
                raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _read_json_body(self) -> Dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length > 0 else b"{}"
                return json.loads(raw.decode("utf-8", errors="replace"))

            def do_GET(self) -> None:
                if self.path in ("/props", "/v1/props"):
                    self._send_json(
                        200,
                        {
                            "default_generation_settings": {"n_ctx": parent.n_ctx},
                            "model_alias": parent.model_id,
                        },
                    )
                    return

                if self.path == "/v1/models":
                    self._send_json(
                        200,
                        {
                            "object": "list",
                            "data": [
                                {
                                    "id": parent.model_id,
                                    "object": "model",
                                    "context_length": parent.n_ctx,
                                }
                            ],
                        },
                    )
                    return

                if self.path == "/status":
                    self._send_json(200, {"status": "ok"})
                    return

                if self.path == "/slots":
                    self._send_json(200, [{"id": 0, "n_ctx": parent.n_ctx, "state": 0}])
                    return

                self._send_json(404, {"error": f"Unknown GET path: {self.path}"})

            def do_POST(self) -> None:
                body = self._read_json_body()

                if self.path in ("/tokenize", "/v1/tokenize"):
                    with parent._lock:
                        parent.recorded_tokenize_requests.append(body)
                    if not parent.enable_tokenize:
                        self._send_json(404, {"error": "tokenize disabled"})
                        return
                    text = str(body.get("content") or "")
                    # Deterministic synthetic token count (~4 chars/token)
                    tok_len = max(1, len(text) // 4)
                    self._send_json(200, {"tokens": list(range(tok_len))})
                    return

                if self.path in ("/v1/models/unload", "/models/unload", "/unload"):
                    with parent._lock:
                        parent.recorded_unload_requests.append(body)
                    if parent.unload_succeeds:
                        self._send_json(200, {"status": "unloaded", "model": body.get("model")})
                    else:
                        self._send_json(501, {"error": "unload unsupported"})
                    return

                if self.path == "/v1/chat/completions":
                    with parent._lock:
                        parent.recorded_chat_requests.append(body)
                        if not parent.scripted_replies:
                            self._send_json(500, {"error": "MockStrataServer has no scripted reply left."})
                            return
                        spec = parent.scripted_replies.pop(0)

                    reply = spec(body) if callable(spec) else spec

                    if reply.get("http_error"):
                        self._send_json(int(reply["http_error"]), {"error": reply.get("error_msg", "forced error")})
                        return

                    if reply.get("non_stream") or not body.get("stream", False):
                        self._send_json(200, parent._build_non_stream_payload(reply))
                        return

                    self._send_sse_stream(reply)
                    return

                self._send_json(404, {"error": f"Unknown POST path: {self.path}"})

            def _send_sse_stream(self, reply: Dict[str, Any]) -> None:
                # Deliberately omit `charset=utf-8` on SSE header to verify adapter UTF-8 byte decoding!
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()

                def emit(chunk_obj: Dict[str, Any]) -> None:
                    line = f"data: {json.dumps(chunk_obj, ensure_ascii=False)}\n\n".encode("utf-8")
                    self.wfile.write(line)
                    self.wfile.flush()

                reasoning = reply.get("reasoning_content") or ""
                if reasoning:
                    mid = max(1, len(reasoning) // 2)
                    for part in (reasoning[:mid], reasoning[mid:]):
                        if part:
                            emit({"choices": [{"delta": {"reasoning_content": part}, "finish_reason": None}]})

                content = reply.get("content") or ""
                if content:
                    mid = max(1, len(content) // 2)
                    for part in (content[:mid], content[mid:]):
                        if part:
                            emit({"choices": [{"delta": {"content": part}, "finish_reason": None}]})

                tool_calls = reply.get("tool_calls") or []
                for idx, tc in enumerate(tool_calls):
                    tc_id = tc.get("id") or f"call_{idx}"
                    fn_name = tc["function"]["name"]
                    fn_args = tc["function"]["arguments"]
                    # Split arguments across two SSE chunks to test fragmented aggregation
                    split_at = max(1, len(fn_args) // 2)
                    emit(
                        {
                            "choices": [
                                {
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": idx,
                                                "id": tc_id,
                                                "type": "function",
                                                "function": {"name": fn_name, "arguments": fn_args[:split_at]},
                                            }
                                        ]
                                    },
                                    "finish_reason": None,
                                }
                            ]
                        }
                    )
                    if fn_args[split_at:]:
                        emit(
                            {
                                "choices": [
                                    {
                                        "delta": {
                                            "tool_calls": [
                                                {
                                                    "index": idx,
                                                    "function": {"arguments": fn_args[split_at:]},
                                                }
                                            ]
                                        },
                                        "finish_reason": None,
                                    }
                                ]
                            }
                        )

                finish_reason = reply.get("finish_reason") or ("tool_calls" if tool_calls else "stop")
                final_chunk: Dict[str, Any] = {
                    "choices": [{"delta": {}, "finish_reason": finish_reason}],
                }
                if reply.get("usage"):
                    final_chunk["usage"] = reply["usage"]
                if reply.get("timings"):
                    final_chunk["timings"] = reply["timings"]
                emit(final_chunk)

                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread: Optional[threading.Thread] = None

    @staticmethod
    def _build_non_stream_payload(reply: Dict[str, Any]) -> Dict[str, Any]:
        msg: Dict[str, Any] = {"role": "assistant", "content": reply.get("content", "")}
        if reply.get("reasoning_content"):
            msg["reasoning_content"] = reply["reasoning_content"]
        if reply.get("tool_calls"):
            msg["tool_calls"] = reply["tool_calls"]
        out: Dict[str, Any] = {
            "choices": [{"message": msg, "finish_reason": reply.get("finish_reason", "stop")}],
        }
        if reply.get("usage"):
            out["usage"] = reply["usage"]
        if reply.get("timings"):
            out["timings"] = reply["timings"]
        return out

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def queue_reply(self, reply: ScriptedReply) -> None:
        with self._lock:
            self.scripted_replies.append(reply)

    def start(self) -> "MockStrataServer":
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=3.0)

    def __enter__(self) -> "MockStrataServer":
        return self.start()

    def __exit__(self, *_args: Any) -> None:
        self.stop()