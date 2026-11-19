#!/usr/bin/env python3
"""
Unified entry point for Local AI Project Orchestrator (LAPO).
- Run with no arguments (or `--gui`) to launch the Desktop GUI.
- Pass CLI subcommands (`models`, `run`, `resume`, `status`, `request`, `unlock`, `init-config`, `--help`)
  to operate headlessly from Windows PowerShell/CMD or Linux terminals.
"""

from __future__ import annotations

import sys
from typing import List, Optional


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if not args or args == ["--gui"]:
        try:
            from lapo.ui.main_window import launch_gui
        except Exception as err:
            print(
                f"[LAPO] Could not start Desktop GUI ({err}).\n"
                "On headless Linux servers, use the CLI instead, e.g.: `python lapo_app.py --help`",
                file=sys.stderr,
            )
            return 1
        return launch_gui()

    from lapo.cli import main as cli_main

    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())