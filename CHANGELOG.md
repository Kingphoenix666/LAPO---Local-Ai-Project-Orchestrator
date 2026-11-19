\# Changelog



All notable changes to \*\*Local AI Project Orchestrator (LAPO)\*\* are documented in this file.



\## \[1.0.0] - 2026-11-19

\### Added

\- Shared cross-platform orchestration engine (`lapo/core/`) powering both Desktop GUI (`Tkinter`/`ttk`) and headless Terminal CLI (`lapo`).

\- Dynamic Strata / OpenAI-compatible adapter (`lapo/adapters/strata\_adapter.py`) supporting arbitrary host/port configuration, automatic loopback proxy bypass, Strata CLI + `/v1/models` discovery, exact `POST /tokenize` counting, long-timeout UTF-8 SSE streaming (`reasoning\_content`, `content`, fragmented `tool\_calls`), and verified model unload.

\- Explicit multi-phase execution protocol (`phase\_execution\_plan.json`) with orchestrator-only phase transitions and strict phase isolation handoffs.

\- Dual file-writing protocol: native OpenAI tool calls + unescaped inline `<file path="...">...</file>` auto-writer + truncated tool-call JSON recovery.

\- KV-cache-preserving in-stream context compaction (retaining tool schemas in API payloads while locking tools Python-side) with schema validation and deterministic machine-state fallback.

\- Unattended Smart Auto-Verify mode (Python `py\_compile` / `unittest`, JSON validation, TypeScript/Vitest/Go/Rust auto-detection) so multi-hour runs never stall waiting for manual approval popups.

\- Application-level SHA-256 file lock manager with dependency synchronization and controlled user-authorized Unlock \& repair re-verification.

\- Background live user request watcher (`user\_request.json` -> `readed\_user\_request\_<timestamp>.json`) with phase routing and pre-closure acknowledgment enforcement.

\- Atomic crash recovery (`state.json`, `journal.json`) with `plan\_id` verification and archive-on-fresh-start support.

\- Non-double-counted token ledger (Read / Think / Write) generating JSON and Markdown final reports.

\- Cross-platform launchers (`run\_windows.bat`, `run\_linux.sh`), `pyproject.toml`, and 22 automated unit \& end-to-end integration tests.

