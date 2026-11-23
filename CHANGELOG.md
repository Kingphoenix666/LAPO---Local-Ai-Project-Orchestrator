# Changelog

All notable changes to **Local AI Project Orchestrator (LAPO)** are documented in this file.

## [1.0.3] - 2026-11-23
### Fixed
- **Universal Thinking Stream Parsing (`lapo/adapters/strata_adapter.py`):** Added support for the `thinking` delta/message field alongside `reasoning_content` and `reasoning`, plus an incremental `_InlineThinkSplitter` to separate inline `<think>...</think>` and `<thought>...</thought>` tags (including tags split across SSE chunks and non-streaming responses) into the live Thinking Stream tab.## [1.0.2] - 2026-11-19
### Added
- **GUI & CLI Proxy Failover Controls (`main_window.py`, `cli.py`, `config.py`):** Added Primary/Fallback proxy fields (`Proxy 1`, `Proxy 2`), a live TCP **Test** button, and an **API via Proxy** toggle for remote or tunneled endpoints.
- **Automatic Private LAN / MikroTik Bypass (`strata_adapter.py`, `config.py`):** Automatically bypasses system/VPN proxies for RFC1918 private LAN IPs (`192.168.x.x`, `10.x.x.x`, `172.16-31.x.x`, `.local`) in addition to loopback so local LAN GPU servers remain reachable.
- **Trusted Pre-Flight Dependency Bootstrap (`verification_manager.py`, `orchestrator.py`):** Automatically runs `npm install` via the active Primary/Fallback proxy on Start when `package.json` exists and `node_modules/` is missing (controlled by the `Auto-Install Deps` option).
- **Expanded Test Suite:** Added 5 new unit tests in `tests/test_proxy_and_bootstrap.py` (31 total automated tests).

### Fixed
- **GUI Config Persistence (`main_window.py`):** `Load Config` and `Save Config` now preserve `primary_proxy`, `fallback_proxy`, `route_api_via_proxy`, `auto_install_deps`, and `allowed_commands`.

## [1.0.1] - 2026-11-19
### Improved
- **Live Tool Argument Telemetry (`strata_adapter.py`):** Streaming heartbeats now report `ToolCalls: N (Args: X chars)` so long tool-call payloads show real-time generation progress instead of looking stalled.
- **Smarter Truncated JSON Recovery & Anti-Loop Escalation (`orchestrator.py`):** Tailors recovery instructions per tool (`patch_file` advises short 5–15 line snippets; `write_file` redirects to `<file path="...">` blocks), logs received truncated byte lengths, and escalates automatically if truncated JSON happens consecutively so runs self-correct without manual intervention.
- **Clean Verification Logs (`verification_manager.py`):** Automatically strips terminal ANSI escape/color codes from test runner outputs (such as Vitest/Jest/pytest) for cleaner GUI Activity logs and lower token overhead.
- **Expanded Automated Test Suite:** Added dedicated unit and end-to-end tests in `tests/test_v101_improvements.py` (26 total automated tests).

## [1.0.0] - 2026-11-19
### Added
- Shared cross-platform orchestration engine (`lapo/core/`) powering both Desktop GUI (`Tkinter`/`ttk`) and headless Terminal CLI (`lapo`).
- Dynamic Strata / OpenAI-compatible adapter (`lapo/adapters/strata_adapter.py`) supporting arbitrary host/port configuration, automatic loopback proxy bypass, Strata CLI + `/v1/models` discovery, exact `POST /tokenize` counting, long-timeout UTF-8 SSE streaming (`reasoning_content`, `content`, fragmented `tool_calls`), and verified model unload.
- Explicit multi-phase execution protocol (`phase_execution_plan.json`) with orchestrator-only phase transitions and strict phase isolation handoffs.
- Dual file-writing protocol: native OpenAI tool calls + unescaped inline `<file path="...">...</file>` auto-writer + truncated tool-call JSON recovery.
- KV-cache-preserving in-stream context compaction (retaining tool schemas in API payloads while locking tools Python-side) with schema validation and deterministic machine-state fallback.
- Unattended Smart Auto-Verify mode (Python `py_compile` / `unittest`, JSON validation, TypeScript/Vitest/Go/Rust auto-detection) so multi-hour runs never stall waiting for manual approval popups.
- Application-level SHA-256 file lock manager with dependency synchronization and controlled user-authorized Unlock & repair re-verification.
- Background live user request watcher (`user_request.json` -> `readed_user_request_<timestamp>.json`) with phase routing and pre-closure acknowledgment enforcement.
- Atomic crash recovery (`state.json`, `journal.json`) with `plan_id` verification and archive-on-fresh-start support.
- Non-double-counted token ledger (Read / Think / Write) generating JSON and Markdown final reports.
- Cross-platform launchers (`run_windows.bat`, `run_linux.sh`), `pyproject.toml`, and 22 automated unit & end-to-end integration tests.