# Changelog

All notable changes to **Local AI Project Orchestrator (LAPO)** are documented in this file.

## [1.1.0] - 2026-11-25
### Added
- **Strata Engine 0.1.67 Native Integration & Telemetry (`strata_adapter.py`, `usage_ledger.py`, `orchestrator.py`):**
  - Auto-inspects `/v1/status` to detect `engine_version` (e.g., `0.1.67`) and `cache_max_tokens` (e.g., `262144`).
  - Polls Strata `/metrics` at session start/end to record exact delta telemetry in `final_report.json` and `final_report.md`: `prompt_tokens_read`, `prompt_tokens_reused`, `prompt_reuse_pct`, `weighted_decode_tok_s`, and `drafts_accepted / drafts_offered` (`DFlash2` / `MTP`).
  - Adds an explicit engine lifecycle state machine (`ENGINE_STARTING`, `ENGINE_READY`, `ENGINE_REQUEST_FAILED`, `ENGINE_RESTARTING`, `ENGINE_TERMINATED`) with `EngineTerminatedError` so LAPO halts cleanly when Strata 0.1.67 hits its 3-crash restart limit.
- **Append-Only History Hygiene (`orchestrator.py`, `config.py`):** Strips `reasoning_content` and compacts large `write_file` / inline `<file>` bodies into one-line receipts **once upon turn insertion** (leaving earlier turns byte-identical to preserve Strata 0.1.67 Conversation Cache prefix reuse), and adjusts post-flight context accounting to prevent false compactions.
- **Live Thought Watchdog (`strata_adapter.py`, `orchestrator.py`):** Monitors SSE streams live and cleanly interrupts runaway reasoning (default `32,000` chars with 0 output and 0 tool calls) to redirect the model toward direct file/tool output without polluting history.
- **Bounded Compaction JSON & Python AST Symbol Map (`compaction_manager.py`, `phase_manager.py`):** Strictly caps continuation JSON size (`max_compaction_summary_chars=4000`) and injects an automated `python_ast_map` (classes, methods, function signatures) into the Machine-Verified Snapshot to eliminate post-reset re-read loops.
- **Safe, Lock-Aware `delete_file` Tool (`workspace_tools.py`, `file_lock_manager.py`):** Allows deleting unlocked scratch/temporary workspace files with automatic `.orchestrator/backups` backup while strictly rejecting locked files, repair-queue files, directories, and out-of-workspace paths.
- **Verified Strata-HotRod Profiles in CLI & Desktop GUI (`config.py`, `cli.py`, `main_window.py`):** Added `lapo profiles`, `--profile` (`gyro_m_256k_native`, `gyro_m_512k`, `coder_iq1_m_128k`, `coder_iq1_m_64k`), granular watchdog/hygiene CLI flags, and a HotRod Profile & Controls bar in the Tkinter GUI.
- **Expanded Automated Test Suite (`tests/test_v110_hotrod.py`):** Added 8 new tests covering all v1.1.0 features (39 total automated tests passing).
### Verified Live Benchmarks (Windows 11 · i5-13600K · RTX 4070 12GB · Strata Engine 0.1.67)
- **LAPO v1.1.0 Live 2-Phase Integration (`gyro_m_256k_native`, `0.1.67` without `STRATA_HC_SPLIT=0`):** `exit_code=0`, 23 requests, 32 tool calls, 0 errors/retries/compactions, **88.83%** prompt cache reuse (`140,848` reused / `17,719` newly read), **83.4%** DFlash2 draft acceptance, **19,917** history chars compacted, `delete_file` scratch cleanup & 5 locked outputs verified.
## [1.0.3] - 2026-11-23
### Fixed
- **Universal Thinking Stream Parsing (`lapo/adapters/strata_adapter.py`):** Added support for the `thinking` delta/message field alongside `reasoning_content` and `reasoning`, plus an incremental `_InlineThinkSplitter` to separate inline `<think>...</think>` and `<thought>...</thought>` tags (including tags split across SSE chunks and non-streaming responses) into the live Thinking Stream tab.

## [1.0.2] - 2026-11-19
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