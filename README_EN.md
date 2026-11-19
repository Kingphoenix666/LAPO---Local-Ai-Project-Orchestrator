\# Local AI Project Orchestrator (LAPO)



\*\*Local AI Project Orchestrator (LAPO)\*\* is a cross-platform supervisory execution system (Python 3.10+) that transforms local language models—primarily \*\*Strata\*\* and any OpenAI-compatible Chat Completions backend—into structured, unattended, multi-phase project agents.



Unlike a chatbot, LAPO controls workspace file access, enforces milestone-by-milestone execution, runs verification gates, protects finalized files with SHA-256 locks, compacts context without invalidating the server's KV-cache prefix, ingests live mid-flight user instructions, and recovers cleanly from interruptions.



\---



\## Key Features



\- \*\*Single Shared Engine (GUI + CLI):\*\* Desktop GUI (`Tkinter`/`ttk`) and headless Terminal CLI (`lapo`) share 100% of `lapo/core/`.

\- \*\*Zero Hardcoded Ports or Domains:\*\* Enter any API URL/port (e.g., `http://127.0.0.1:5678/v1`), discover `\[ACTIVE]`, `\[INSTALLED - NOT LOADED]`, and `\[API ONLY]` models in a dropdown, and run any language or workflow.

\- \*\*Built for Real Local LLM Constraints:\*\*

&#x20; - Strict loopback proxy bypass (`127.0.0.1` / `localhost` / `::1`) so VPNs/system proxies never break local inference.

&#x20; - Explicit UTF-8 SSE stream decoding for reasoning deltas (`reasoning\_content`), content deltas, and fragmented tool calls.

&#x20; - Configurable long read timeout (default 90 minutes) for heavy MoE SSD expert offloading, plus automatic retries and server reconnect polling.

&#x20; - \*\*Dual File-Writing Protocol:\*\* Supports standard tool calls (`write\_file`, `patch\_file`) AND direct unescaped `<file path="...">...</file>` inline blocks to avoid JSON escaping truncation on large files.

\- \*\*Explicit Phase Execution Protocol:\*\*

&#x20; - Define independent ordered phases with per-phase prompts and optional verification commands.

&#x20; - The Orchestrator (`phase\_execution\_plan.json`) is the sole authority for phase transitions—models cannot skip phases or advance prematurely.

\- \*\*Unattended Smart Auto-Verify (Default On):\*\*

&#x20; - If a phase has custom verification commands, LAPO executes them with `shell=False`.

&#x20; - If no custom command is supplied, LAPO automatically verifies syntax (`py\_compile`, JSON validation) and auto-detected test suites (`unittest`, `tsc`, `vitest`, `go test`, `cargo check`) so multi-hour runs never stall waiting for manual clicks.

&#x20; - Optional manual approval mode can be toggled in the GUI or via `--manual-approval` in CLI.

\- \*\*KV-Cache-Preserving In-Stream Context Compaction:\*\*

&#x20; - Supports `Auto` context detection (`/props`, `/slots`, `/v1/models`) or manual ceilings (`32K`, `64K`, `128K`, `256K`, `512K`, `1M`, or exact integers) with exact `POST /tokenize` accounting.

&#x20; - Retains `TOOLS\_SCHEMA` in the compaction turn while locking tools Python-side, preventing catastrophic KV-cache invalidation.

&#x20; - Validates the model's continuation JSON with a deterministic machine-state fallback, resets client message history, and resumes the \*\*exact same active phase\*\*.

\- \*\*SHA-256 File Locks \& Controlled Repair:\*\*

&#x20; - Finalized outputs are locked in `.orchestrator/locks.json` and read-protected against unintended edits.

&#x20; - Users can explicitly authorize `Unlock for Repair`; the model must reread the file, apply minimal fixes, and pass the owning phase's verification again before re-locking.

\- \*\*Live Mid-Flight User Requests (`user\_request.json` → `readed\_`):\*\*

&#x20; - A background watcher thread monitors `Workspace/user\_request.json` independently of model streaming.

&#x20; - Each request is consumed once, queued to its target phase, renamed immediately to `readed\_user\_request\_<timestamp>.json`, and enforced before phase closure (`resolved\_request\_ids`).

\- \*\*Accurate Usage Ledger \& Verified Unload:\*\*

&#x20; - Separates Read (`prompt\_tokens`), Think (`reasoning\_tokens`), and Write (`completion\_tokens` excluding reasoning) without double-counting.

&#x20; - Generates `.orchestrator/reports/final\_report.json` and `final\_report.md`, and optionally sends a verified model unload request after user approval.



\---



\## Installation



\### Requirements

\- Python \*\*3.10 or newer\*\*

\- A running local inference server (Strata, llama.cpp server, LM Studio, vLLM, etc.)



\### Option 1: One-Click Launchers (Recommended)

\- \*\*Windows:\*\* Run `run\_windows.bat`

\- \*\*Linux:\*\* Run `chmod +x run\_linux.sh \&\& ./run\_linux.sh`



\### Option 2: Manual Virtual Environment / Wheel Install


python -m venv .venv

\# Windows:

.venv\\Scripts\\activate

\# Linux:

source .venv/bin/activate



pip install -r requirements.txt

python lapo\_app.py



To install the lapo command globally inside your environment:



pip install .

lapo --help



CLI Reference



\# Discover installed Strata \& active API models

lapo models --url \[http://127.0.0.1:47831/v1](http://127.0.0.1:47831/v1)



\# Generate a starter project.json config

lapo init-config --directory ./my\_project



\# Start an interactive multi-phase run

lapo run --workspace ./my\_workspace --url \[http://127.0.0.1:47831/v1](http://127.0.0.1:47831/v1) --model my-model --interactive-phases



\# Start from a config file

lapo run --config ./examples/project.example.json



\# Resume an interrupted session cleanly

lapo resume --workspace ./my\_workspace --url \[http://127.0.0.1:47831/v1](http://127.0.0.1:47831/v1) --model my-model



\# Inject a mid-flight user instruction into Phase 2

lapo request --workspace ./my\_workspace --phase 2 --prompt "Add edge-case error handling"



\# Inspect workspace status \& lock integrity

lapo status --workspace ./my\_workspace



\# Authorize repair on a locked file

lapo unlock --workspace ./my\_workspace --file src/core.py --reason "Fix boundary check"



\# Verify live Strata connectivity with a smoke test

lapo run --workspace ./my\_workspace --url \[http://127.0.0.1:47831/v1](http://127.0.0.1:47831/v1) --smoke-test



Troubleshooting



&#x20;   Cannot reach 127.0.0.1:<port> while VPN/proxy is active: LAPO automatically bypasses proxies for loopback addresses. Verify your inference server is listening on the exact host and port entered in API URL.



&#x20;   Model shows \[INSTALLED - NOT LOADED]: Load the model inside Strata first, then click 🔄 Fetch Models so it appears as \[ACTIVE].



&#x20;   Headless Linux server (no DISPLAY): Use CLI subcommands (./run\_linux.sh run --config ...); GUI requires Tk (python3-tk on Debian/Ubuntu).



&#x20;   Unload could not be verified: Not all OpenAI-compatible servers expose a remote unload API. LAPO never fakes unload success—if your server returns 404/501, unload the model directly in your server UI/CLI.

