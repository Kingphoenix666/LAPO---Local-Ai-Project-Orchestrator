<div align="center">

# Local AI Project Orchestrator (LAPO)

**Turn local LLMs (Strata, llama.cpp, LM Studio, vLLM) into reliable, unattended, multi-phase engineering agents.**

[![Version](https://img.shields.io/badge/Version-1.1.0-2f81f7?style=flat-square)](CHANGELOG.md)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab?style=flat-square&logo=python&logoColor=white)](pyproject.toml)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux-2ea043?style=flat-square)](#quick-start)
[![Interfaces](https://img.shields.io/badge/Interface-Desktop%20GUI%20%2B%20CLI-d29922?style=flat-square)](#quick-start)
[![Tests](https://img.shields.io/badge/Automated%20Tests-39%20Passed-238636?style=flat-square)](tests/)

<br>

[**English Documentation (README_EN.md)**](README_EN.md) &nbsp;•&nbsp;
[**مستندات کامل فارسی (README_FA.md)**](README_FA.md) &nbsp;•&nbsp;
[**Security Model**](SECURITY.md) &nbsp;•&nbsp;
[**Changelog**](CHANGELOG.md)

</div>


<img width="2560" height="1392" alt="LAPO_V1 1 0" src="https://github.com/user-attachments/assets/6a296a54-0eca-4d6f-b6eb-4afa1fed70c0" />



---

## What is LAPO?

**LAPO** is not a chat window—it is a **supervisory execution engine** that runs local language models across long, multi-phase projects without losing context, corrupting finished files, or stalling when you step away from your desk.

Both the **Desktop GUI (`Tkinter`/`ttk`)** and the **Headless Terminal CLI (`lapo`)** run on one shared core engine (`lapo/core/`).

### Core Capabilities

| Capability | How LAPO Solves It |
| :--- | :--- |
| **Dynamic Server & Model Discovery** | Connect to any host/port (no hardcoded ports). Combines Strata CLI inventory with `GET /v1/models` to tag `[ACTIVE]`, `[INSTALLED - NOT LOADED]`, and `[API ONLY]` models. |
| **Strict Multi-Phase Isolation** | Locks phases in `.orchestrator/phase_execution_plan.json`. Only the Orchestrator can advance phases—models cannot skip milestones. |
| **Unattended Smart Auto-Verify** | Runs custom verification commands (`shell=False`) or auto-verifies syntax & detected test suites (`unittest`, `tsc`, `vitest`, `go test`, `cargo check`) with zero manual blocking. |
| **KV-Cache-Preserving Compaction** | Counts tokens via `POST /tokenize`, keeps tool schemas intact during in-stream compaction so server KV-cache stays warm, resets history cleanly, and stays on the exact active phase. |
| **Dual File-Writing Protocol** | Supports native tool calls (`write_file`, `patch_file`) **and** unescaped `<file path="...">...</file>` inline blocks to eliminate JSON-escaping truncation on large files. |
| **SHA-256 File Locks & Repair** | Finalized phase outputs are locked and read-only to model tools. Controlled repair requires explicit user `Unlock` and re-verification of the owning phase. |
| **Live Mid-Flight Instructions** | Independent watcher thread ingests `Workspace/user_request.json` once, renames it to `readed_user_request_<timestamp>.json`, and enforces resolution before phase closure. |
| **Crash Recovery & Token Ledger** | Atomic state/journal persistence with `plan_id` checks, non-double-counted Read/Think/Write token reports (`JSON` + `Markdown`), and verified model unload. |

---

## Architecture Overview

```text
                    +-------------------------------------------+
                    |   Desktop GUI (Tkinter)  |  Headless CLI  |
                    +--------------------+-----+----------------+
                                         |  Thread-Safe EventBus
                    +--------------------v----------------------+
                    |     Shared Core Engine (lapo/core/)       |
                    |  PhaseManager • Context/Compaction • Locks|
                    |  Auto-Verify • Live Requests • Recovery   |
                    +--------+------------------------+---------+
                             |                        |
            +----------------v---------+    +---------v------------------+
            |  Strata / OpenAI Adapter |    |  Confined Workspace Tools  |
            |  UTF-8 SSE • Proxy Bypass|    |  <file> Writer • Read-Only |
            |  POST /tokenize • Unload |    |  Skills • shell=False      |
            +--------------------------+    +----------------------------+
```

---

## What's New in v1.1.0 (HotRod & Strata 0.1.67)
- **Strata Engine `0.1.67` Native Telemetry & Lifecycle Guard:** Auto-detects engine version and `cache_max_tokens` via `/v1/status`, captures `/metrics` deltas (`prompt_tokens_read` vs `prompt_tokens_reused`, `DFlash2`/`MTP` acceptance rate), and handles `ENGINE_TERMINATED` cleanly.
- **Cache-Safe Append-Only History Hygiene:** Strips reasoning blocks and compacts large file writes into one-line receipts once per turn without mutating earlier turns—preserving Strata `0.1.67` Conversation Cache prefix hits.
- **Live Thought Watchdog + Anti-Drafting Policy:** Interrupts runaway reasoning streams that emit no output/tool calls and redirects the model cleanly.
- **Bounded Compaction JSON + Python AST Symbol Map:** Caps continuation JSON size (`4,000` chars default) and attaches a compact `python_ast_map` after context resets to prevent redundant full-file re-reads.
- **Lock-Aware `delete_file` Tool & HotRod Profiles:** Safe scratch-file cleanup with automatic backup, plus ready-to-use `--profile` presets (`gyro_m_256k_native`, `gyro_m_512k`, `coder_iq1_m_128k`, `coder_iq1_m_64k`) in both CLI (`lapo profiles`) and Desktop GUI.
### Verified Strata Engine Benchmarks (RTX 4070 12GB · Windows 11)
| Run / Profile | Engine | Phases & Outcome | Prompt Reuse | Draft Accept | Compactions | Unit Tests |
| :--- | :--- | :--- | :---: | :---: | :---: | :---: |
| **LAPO v1.1.0 Live Check (`gyro_m_256k_native`)** | `0.1.67` | 2 phases · `exit 0` (23 reqs, 0 err, 19.9K chars compacted) | **88.83%** | **83.4%** (`DFlash2`) | **0** | **100% OK** |
| **Baseline Reference (`Gyro-M 512K`, `mini_kv`)** | `0.1.64` | 3 phases · `exit 0` (110 reqs, 156 min) | **98.83%** | **84.43%** (`DFlash2`) | **0** | **199 / 199 OK** |
| **Ablation Study (`Coder IQ1_M 64K`, `mini_kv`)** | `0.1.66` | 2 phases · `exit 0` (86 reqs, 133.6 min) | **85.28%** | **75.19%** (`MTP`) | 6 | **108 / 108 OK** |
## Quick Start

### Windows
Double-click **`run_windows.bat`** or run in PowerShell:
```powershell
.\run_windows.bat
```

### Linux (Desktop & Headless Server)
```bash
chmod +x run_linux.sh
./run_linux.sh
```

### CLI Examples
```bash
# List installed Strata models & active API models
python lapo_app.py models --url [http://127.0.0.1:47831/v1](http://127.0.0.1:47831/v1)

# Run an interactive multi-phase project
python lapo_app.py run --workspace ./my_workspace --url [http://127.0.0.1:47831/v1](http://127.0.0.1:47831/v1) --interactive-phases

# Send a live instruction to Phase 2 while the model is running
python lapo_app.py request --workspace ./my_workspace --phase 2 --prompt "Add boundary validation"
```

### Run Automated Test Suite
```bash
python -m unittest discover -s tests -v
```

---

<div align="center">

Need full setup, configuration, and troubleshooting guides?  
Read [**README_EN.md**](README_EN.md) (English) or [**README_FA.md**](README_FA.md) (فارسی).

</div>
