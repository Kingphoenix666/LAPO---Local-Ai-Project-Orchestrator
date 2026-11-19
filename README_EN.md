<div align="center">



\# Local AI Project Orchestrator (LAPO) — Documentation



\[Main Page](README.md) \&nbsp;•\&nbsp;

\[\*\*نسخه فارسی (README\_FA.md)\*\*](README\_FA.md) \&nbsp;•\&nbsp;

\[Security](SECURITY.md) \&nbsp;•\&nbsp;

\[Changelog](CHANGELOG.md)



</div>



\---



\## 1. Overview



\*\*Local AI Project Orchestrator (LAPO)\*\* is a cross-platform Python (3.10+) supervisory system that transforms locally hosted language models—primarily \*\*Strata\*\* and any server implementing the OpenAI-compatible Chat Completions API—into structured, unattended, multi-phase project agents.



Instead of feeding one massive prompt into a chat window, LAPO manages:

\- Explicit, independently verified project milestones

\- Confined workspace file operations and a separate read-only `Skills` folder

\- Automatic context compaction without breaking the inference server's KV-cache prefix

\- Application-level SHA-256 locks on finalized files

\- Mid-flight user instruction injection (`user\_request.json` → `readed\_user\_request\_<timestamp>.json`)

\- Crash recovery, accurate token accounting, and verified model unload



\---



\## 2. Why LAPO Works Reliably with Local LLMs



Local quantized and MoE models behave differently from cloud APIs. LAPO includes dedicated engineering safeguards for real local hardware:



1\. \*\*Loopback Proxy Bypass:\*\* Requests to `127.0.0.1`, `localhost`, and `::1` explicitly pass `proxies={"http": None, "https": None}` so active OS/VPN proxies never cause `502` or `ProxyError`.

2\. \*\*Explicit UTF-8 SSE Decoding:\*\* Raw SSE bytes are always decoded as UTF-8, preventing garbled Unicode/Persian text when servers omit `charset=utf-8` on `text/event-stream` headers.

3\. \*\*Heavy MoE / SSD Offloading Tolerance:\*\* Configurable long read timeout (default `5400s` / 90 minutes), controlled retries, and server health polling during reconnects.

4\. \*\*Dual File-Writing Protocol (`<file>` Auto-Writer):\*\* Large files (>80 lines) often break single-line JSON string escaping inside `write\_file`. LAPO accepts both standard tool calls and unescaped `<file path="...">...</file>` blocks, and automatically sanitizes truncated tool-call JSON from conversation history so subsequent turns never crash.

5\. \*\*KV-Cache-Preserving In-Stream Compaction:\*\* During compaction, LAPO locks tools on the Python side while keeping `TOOLS\_SCHEMA` unchanged in the API payload. This preserves the inference engine's prompt KV-cache prefix instead of forcing a full-context re-evaluation.



\---



\## 3. Installation



\### Prerequisites

\- \*\*Python 3.10+\*\*

\- A running local LLM server (Strata, `llama-server`, LM Studio, vLLM, etc.)

\- On Debian/Ubuntu Linux (GUI only): `sudo apt install python3-tk`



\### Option A: Automated Launchers (Recommended)

Both launchers verify Python 3.10+, create `.venv`, install `requirements.txt`, validate imports, and launch LAPO:



\- \*\*Windows:\*\*

&#x20; ```powershell

&#x20; .\\run\_windows.bat

&#x20; ```

\- \*\*Linux:\*\*

&#x20; ```bash

&#x20; chmod +x run\_linux.sh

&#x20; ./run\_linux.sh

&#x20; ```



\### Option B: Manual Virtual Environment / Editable Install

```bash

python -m venv .venv

\# Windows:

.venv\\Scripts\\activate

\# Linux:

source .venv/bin/activate



pip install -r requirements.txt

pip install .



\# Launch Desktop GUI:

lapo



\# Or show CLI commands:

lapo --help

```



\---



\## 4. Desktop GUI Workflow



1\. \*\*Strata / Local API:\*\* Enter your server URL and port (e.g., `http://127.0.0.1:47831/v1` or `http://127.0.0.1:5678/v1`), click \*\*Fetch Models\*\*, and pick an `\[ACTIVE]` or `\[API ONLY]` model.

2\. \*\*Workspace \& Skills:\*\* Select your target \*\*Workspace\*\* directory (the only folder the model can modify) and an optional read-only \*\*Skills\*\* directory containing architectural specs or coding rules.

3\. \*\*Context Safety \& Unattended Mode:\*\*

&#x20;  - Leave \*\*Context\*\* on `auto` (reads `/props`, `/slots`, or `/v1/models`) or pick a manual ceiling (`32K`–`1M`).

&#x20;  - Keep \*\*Auto-Verify \& Continue Between Phases (Unattended Mode)\*\* checked if you want LAPO to run all phases continuously without manual approval popups.

4\. \*\*Phase Builder:\*\* Create independent phases using \*\*+ Add Phase\*\*, enter each phase's prompt and optional verification commands, then click \*\*Finish Phases Input\*\* to lock the execution plan.

5\. \*\*Start \& Monitor:\*\* Click \*\*Start\*\*. Watch live reasoning in \*\*Thinking Stream\*\*, model text in \*\*Generated Output\*\*, and tool/compaction/verification events in \*\*Activity\*\*.

6\. \*\*Live Mid-Flight Request:\*\* While running, choose a target phase (`active` or a specific phase number) and click \*\*Send Live Request\*\* to inject instructions without stopping execution.

7\. \*\*Final Review \& Unload:\*\* When all phases complete, LAPO generates `.orchestrator/reports/final\_report.json` and `final\_report.md`, displays token \& timing metrics, and asks whether to send a verified \*\*Unload\*\* command to your model server.



\---



\## 5. CLI Reference (Windows \& Headless Linux)



| Command | Description |

| :--- | :--- |

| `lapo models --url <URL>` | Discover installed Strata models and active API models |

| `lapo init-config --directory <DIR>` | Create a starter `project.json` template |

| `lapo run --config <FILE>` | Execute a multi-phase run defined in a JSON config |

| `lapo run --workspace <DIR> --interactive-phases` | Launch the interactive terminal phase wizard |

| `lapo resume --workspace <DIR> --url <URL> --model <ID>` | Resume an interrupted session with plan-identity validation |

| `lapo request --workspace <DIR> --phase <ID> --prompt "..."` | Publish a live `user\_request.json` instruction |

| `lapo status --workspace <DIR>` | Inspect active phase, journal state, and SHA-256 lock integrity |

| `lapo unlock --workspace <DIR> --file <REL\_PATH>` | Authorize controlled repair on a finalized file |

| `lapo run --workspace <DIR> --url <URL> --smoke-test` | Run a live connectivity, tokenization, and streaming smoke test |



> Use `--manual-approval` to pause for confirmation between phases that have no verification commands, or `--non-interactive` for automated pipelines.



\---



\## 6. Workspace State Layout



All orchestration metadata is isolated inside `Workspace/.orchestrator/` (strictly blocked from model tool edits):



```text

Workspace/

├── user\_request.json                 # Consumed once -> renamed to readed\_user\_request\_<ts>.json

└── .orchestrator/

&#x20;   ├── phase\_execution\_plan.json     # Authoritative phase manifest \& plan\_id

&#x20;   ├── state.json                    # Machine-verified snapshot + continuation report

&#x20;   ├── journal.json                  # Atomic conversation history for crash recovery

&#x20;   ├── locks.json                    # SHA-256 locked files \& dependency graph

&#x20;   ├── pending\_requests.json         # Persistent routed live user requests

&#x20;   ├── repair\_queue.json             # User-authorized unlock/repair obligations

&#x20;   ├── usage.json                    # Non-double-counted Read/Think/Write ledger

&#x20;   ├── backups/                      # Automatic backups before overwriting existing files

&#x20;   ├── requests/                     # Audit archive of consumed live requests

&#x20;   └── reports/                      # final\_report.json \& final\_report.md

```



\---



\## 7. Automated Testing



Run the full 22-test unit \& deterministic end-to-end integration suite (powered by `tests/mock\_strata\_server.py`):



```bash

python -m unittest discover -s tests -v

```



\---



\## 8. Troubleshooting



| Issue | Resolution |

| :--- | :--- |

| \*\*Cannot reach `127.0.0.1:<port>` while VPN is ON\*\* | LAPO bypasses proxies automatically for loopback addresses. Double-check that your server is running and listening on the exact port in \*\*API URL\*\*. |

| \*\*Model shows `\[INSTALLED - NOT LOADED]`\*\* | Load the model in Strata first, then click \*\*Fetch Models\*\* so its status becomes `\[ACTIVE]`. |

| \*\*Headless Linux server fails to open GUI\*\* | Pass CLI subcommands (e.g., `./run\_linux.sh run --config project.json`). GUI requires an X11/Wayland display and `python3-tk`. |

| \*\*Automatic Unload could not be confirmed\*\* | Not all OpenAI-compatible servers expose an HTTP unload endpoint. LAPO never fakes confirmation; unload the model directly in your server UI/CLI if HTTP 404/501 is returned. |

