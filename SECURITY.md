\# Security Policy \& Threat Model — LAPO



Because \*\*Local AI Project Orchestrator (LAPO)\*\* grants a local language model controlled access to project files and verification commands, model outputs are always treated as \*\*untrusted data\*\*.



\## Implemented Security Controls



1\. \*\*Strict Workspace Path Confinement\*\*

&#x20;  - Every file path passed to `list\_dir`, `read\_file`, `search\_text`, `write\_file`, `patch\_file`, `lock\_file`, and the inline `<file path="...">` auto-writer is resolved with `Path.resolve()` and verified via `relative\_to(workspace\_root)`.

&#x20;  - Directory traversal (`../`) and symlink escapes pointing outside the workspace root are blocked.

&#x20;  - Internal control directories (`.orchestrator/`, `.git/`) and live request files (`user\_request.json`, `readed\_user\_request\_\*`) are strictly off-limits to model tools.



2\. \*\*Read-Only Skills Directory\*\*

&#x20;  - The optional `Skills` folder is exposed exclusively through `list\_skills` and `read\_skill` with independent path-confinement and symlink checks. Models can never write to Skills files.



3\. \*\*Safe Command Execution (`shell=False`)\*\*

&#x20;  - `run\_terminal` and `run\_checks` never invoke a system shell (`shell=False`).

&#x20;  - Control characters (`\\n`, `\\r`, `\\x00`) are rejected.

&#x20;  - Commands must match either the user-configured allowlist or (when Unattended Auto-Verify is enabled) the strict built-in test/compiler prefix allowlist without parent-directory traversal.



4\. \*\*Application-Level File Locks \& Backups\*\*

&#x20;  - Finalized phase outputs are hashed (SHA-256) in `.orchestrator/locks.json` and rejected for modification by model tools.

&#x20;  - Before overwriting existing non-placeholder files, LAPO stores a timestamped backup in `.orchestrator/backups/`.

&#x20;  - External modifications to locked files are detected via SHA-256 integrity checks.

&#x20;  - \*\*Important Limitation:\*\* File locks in LAPO are \*\*application-level guards\*\* enforced inside LAPO's tool pipeline—they are \*\*not\*\* OS kernel ACLs or filesystem immutable flags. External editors or processes running outside LAPO can still modify workspace files.



5\. \*\*Credential Hygiene \& Loopback Isolation\*\*

&#x20;  - API keys are never written to project configuration exports (`to\_safe\_dict`), `state.json`, `usage.json`, or final reports, and are redacted from error messages.

&#x20;  - Requests to loopback hosts (`127.0.0.1`, `localhost`, `::1`) explicitly bypass OS/VPN HTTP proxies (`proxies={"http": None, "https": None}`).

