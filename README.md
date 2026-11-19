\# Local AI Project Orchestrator (LAPO)



<p align="center">

&#x20; <b>Turn local LLMs (Strata, llama.cpp, LM Studio, vLLM) into reliable, unattended, multi-phase engineering agents.</b><br>

&#x20; Windows + Linux \&nbsp;|\&nbsp; Desktop GUI + Headless CLI \&nbsp;|\&nbsp; Shared Core Engine

</p>



<p align="center">

&#x20; <a href="README\_EN.md"><b>English Documentation (README\_EN.md)</b></a>

&#x20; \&nbsp;\&nbsp;|\&nbsp;\&nbsp;

&#x20; <a href="README\_FA.md"><b>مستندات کامل فارسی (README\_FA.md)</b></a>

&#x20; \&nbsp;\&nbsp;|\&nbsp;\&nbsp;

&#x20; <a href="SECURITY.md">Security</a>

&#x20; \&nbsp;\&nbsp;|\&nbsp;\&nbsp;

&#x20; <a href="CHANGELOG.md">Changelog</a>

</p>



\---



\## Quick Start



\### Windows

Double-click `run\_windows.bat` or run in PowerShell:

```powershell

.\\run\_windows.bat



Linux



chmod +x run\_linux.sh

./run\_linux.sh



Run Automated Tests



python -m unittest discover -s tests -v



CLI Usage (Windows \& Headless Linux)



python lapo\_app.py --help

python lapo\_app.py models --url \[http://127.0.0.1:47831/v1](http://127.0.0.1:47831/v1)

python lapo\_app.py run --workspace ./my\_workspace --url \[http://127.0.0.1:47831/v1](http://127.0.0.1:47831/v1) --interactive-phases



For full architecture details, configuration options, live mid-flight requests, context compaction, and troubleshooting, read README\_EN.md or README\_FA.md.

