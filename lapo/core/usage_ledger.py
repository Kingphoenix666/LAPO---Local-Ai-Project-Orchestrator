"""
Lifetime session telemetry, non-double-counted token accounting (Read / Write / Think),
and final JSON + Markdown report generation.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from lapo.config import RuntimeConfig, format_token_count
from lapo.models import (
    PhasePlan,
    atomic_write_json,
    atomic_write_text,
    read_json_safe,
    utc_now_iso,
)


class UsageLedger:
    """
    Tracks wall-clock execution duration, request/tool/compaction/retry counters,
    and separates prompt (read), completion (write), and reasoning (think) tokens
    without double-counting reasoning when already included in completion_tokens.
    """

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.data: Dict[str, Any] = {
            "started_at": None,
            "ended_at": None,
            "start_epoch": None,
            "elapsed_seconds": 0.0,
            "active_model": config.model_name,
            "api_base_url": config.api_base_url,
            "context_setting": config.context_setting,
            "detected_context_capacity": None,
            "engine_version": None,
            "prompt_tokens": 0,
            "prompt_tokens_read": 0,
            "prompt_tokens_reused": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
            "reasoning_tokens_estimated": False,
            "reasoning_included_in_completion": True,
            "total_tokens": 0,
            "request_count": 0,
            "tool_call_count": 0,
            "inline_files_written": 0,
            "compaction_count": 0,
            "retry_count": 0,
            "error_count": 0,
            "thought_watchdog_triggers": 0,
            "history_chars_compacted": 0,
            "drafts_offered": 0,
            "drafts_accepted": 0,
            "strata_metrics_start": None,
            "strata_metrics_end": None,
            "strata_telemetry_delta": None,
            "known_blockers": [],
            "unverified_items": [],
        }
        self.load()

    def load(self) -> None:
        saved = read_json_safe(self.config.usage_file)
        if isinstance(saved, dict):
            self.data.update(saved)

    def save(self) -> None:
        atomic_write_json(self.config.usage_file, self.data)

    def mark_started(
        self,
        detected_ctx: Optional[int] = None,
        engine_version: Optional[str] = None,
        strata_metrics_snapshot: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.data.get("started_at"):
            self.data["started_at"] = utc_now_iso()
            self.data["start_epoch"] = time.time()
        self.data["active_model"] = self.config.model_name
        self.data["api_base_url"] = self.config.api_base_url
        self.data["context_setting"] = self.config.context_setting
        if detected_ctx:
            self.data["detected_context_capacity"] = detected_ctx
        if engine_version:
            self.data["engine_version"] = engine_version
        if isinstance(strata_metrics_snapshot, dict) and self.data.get("strata_metrics_start") is None:
            totals = strata_metrics_snapshot.get("totals")
            if isinstance(totals, dict):
                self.data["strata_metrics_start"] = dict(totals)
        self.save()

    def mark_finished(self) -> None:
        self.data["ended_at"] = utc_now_iso()
        if self.data.get("start_epoch"):
            self.data["elapsed_seconds"] = round(time.time() - float(self.data["start_epoch"]), 2)
        self.save()

    def record_turn(
        self,
        usage: Dict[str, Any],
        timings: Dict[str, Any],
        reasoning_text: str,
        content_text: str,
        retries_used: int = 0,
    ) -> None:
        """
        Accumulate per-turn token counts accurately without double-counting reasoning tokens.
        """
        self.data["request_count"] += 1
        self.data["retry_count"] += max(0, int(retries_used))

        # 1. Prompt tokens (Read vs Reused)
        prompt_tok = int(usage.get("prompt_tokens") or 0)
        reused_tok = 0
        read_tok = 0
        prompt_details = usage.get("prompt_tokens_details") or {}
        if isinstance(prompt_details, dict) and prompt_details.get("cached_tokens") is not None:
            reused_tok = max(0, int(prompt_details.get("cached_tokens") or 0))
            read_tok = max(0, prompt_tok - reused_tok)
        elif isinstance(timings, dict) and ("prompt_n" in timings or "cache_n" in timings):
            reused_tok = max(0, int(timings.get("cache_n") or 0))
            read_tok = max(0, int(timings.get("prompt_n") or 0))
            if prompt_tok == 0:
                prompt_tok = read_tok + reused_tok
        else:
            read_tok = prompt_tok

        # 2. Completion tokens (Write + possibly Think)
        comp_tok = int(usage.get("completion_tokens") or 0)
        if comp_tok == 0 and isinstance(timings, dict):
            comp_tok = int(timings.get("predicted_n") or 0)
        if comp_tok == 0 and (reasoning_text or content_text):
            comp_tok = max(1, int((len(reasoning_text) + len(content_text)) / 3.5))

        # 3. Reasoning tokens (Think) — detect if reported inside completion_tokens_details or top-level
        comp_details = usage.get("completion_tokens_details") or {}
        if isinstance(comp_details, dict) and comp_details.get("reasoning_tokens") is not None:
            r_tok = int(comp_details["reasoning_tokens"])
            included_in_comp = True
        elif usage.get("reasoning_tokens") is not None:
            r_tok = int(usage["reasoning_tokens"])
            # Check total_tokens to determine whether backend already included reasoning in completion_tokens
            reported_total = int(usage.get("total_tokens") or 0)
            if reported_total >= prompt_tok + comp_tok + r_tok and r_tok > 0:
                included_in_comp = False
            else:
                included_in_comp = True
        elif reasoning_text:
            r_tok = max(1, int(len(reasoning_text) / 3.5))
            # Cap estimate so it cannot exceed comp_tok when included
            r_tok = min(r_tok, comp_tok) if comp_tok > 0 else r_tok
            included_in_comp = True
            self.data["reasoning_tokens_estimated"] = True
        else:
            r_tok = 0
            included_in_comp = True

        self.data["prompt_tokens"] += prompt_tok
        self.data["prompt_tokens_read"] = int(self.data.get("prompt_tokens_read") or 0) + read_tok
        self.data["prompt_tokens_reused"] = int(self.data.get("prompt_tokens_reused") or 0) + reused_tok
        self.data["completion_tokens"] += comp_tok
        self.data["reasoning_tokens"] += r_tok
        self.data["reasoning_included_in_completion"] = included_in_comp

        if included_in_comp:
            self.data["total_tokens"] += prompt_tok + comp_tok
        else:
            self.data["total_tokens"] += prompt_tok + comp_tok + r_tok

        if self.data.get("start_epoch"):
            self.data["elapsed_seconds"] = round(time.time() - float(self.data["start_epoch"]), 2)
        self.save()

    def record_tool_calls(self, count: int = 1) -> None:
        self.data["tool_call_count"] += max(0, count)
        self.save()

    def record_inline_files(self, count: int = 1) -> None:
        self.data["inline_files_written"] += max(0, count)
        self.save()

    def record_compaction(self) -> None:
        self.data["compaction_count"] += 1
        self.save()

    def record_watchdog_trigger(self) -> None:
        self.data["thought_watchdog_triggers"] = int(self.data.get("thought_watchdog_triggers") or 0) + 1
        self.save()

    def record_history_compaction_savings(self, chars_saved: int) -> None:
        if chars_saved > 0:
            self.data["history_chars_compacted"] = int(self.data.get("history_chars_compacted") or 0) + int(chars_saved)
            self.save()

    def record_strata_metrics_end(self, strata_metrics_snapshot: Optional[Dict[str, Any]]) -> None:
        if not isinstance(strata_metrics_snapshot, dict):
            return
        end_t = strata_metrics_snapshot.get("totals")
        if not isinstance(end_t, dict):
            return
        self.data["strata_metrics_end"] = dict(end_t)
        start_t = self.data.get("strata_metrics_start") or {}
        if not isinstance(start_t, dict):
            start_t = {}
        same_epoch = (start_t.get("since") == end_t.get("since")) and (end_t.get("requests", 0) >= start_t.get("requests", 0))
        base = start_t if same_epoch else {}
        d_prompt = max(0, int(end_t.get("prompt_tokens") or 0) - int(base.get("prompt_tokens") or 0))
        d_reused = max(0, int(end_t.get("reused") or 0) - int(base.get("reused") or 0))
        d_read = max(0, d_prompt - d_reused)
        d_out = max(0, int(end_t.get("output_tokens") or 0) - int(base.get("output_tokens") or 0))
        d_dec_ms = max(0.0, float(end_t.get("decode_ms") or 0.0) - float(base.get("decode_ms") or 0.0))
        d_off = max(0, int(end_t.get("drafts_offered") or 0) - int(base.get("drafts_offered") or 0))
        d_acc = max(0, int(end_t.get("drafts_accepted") or 0) - int(base.get("drafts_accepted") or 0))
        self.data["drafts_offered"] = d_off
        self.data["drafts_accepted"] = d_acc
        self.data["strata_telemetry_delta"] = {
            "prompt_tokens": d_prompt,
            "prompt_tokens_read": d_read,
            "prompt_tokens_reused": d_reused,
            "prompt_reuse_pct": round((d_reused / float(d_prompt)) * 100.0, 2) if d_prompt > 0 else 0.0,
            "output_tokens": d_out,
            "weighted_decode_tok_s": round(d_out / (d_dec_ms / 1000.0), 2) if d_dec_ms > 0 else 0.0,
            "drafts_offered": d_off,
            "drafts_accepted": d_acc,
            "draft_accept_pct": round((d_acc / float(d_off)) * 100.0, 2) if d_off > 0 else 0.0,
        }
        if d_prompt > 0:
            self.data["prompt_tokens_read"] = d_read
            self.data["prompt_tokens_reused"] = d_reused
        self.save()

    def record_error(self, message: str) -> None:
        self.data["error_count"] += 1
        blockers = self.data.setdefault("known_blockers", [])
        if message and message not in blockers:
            blockers.append(message[:300])
        self.save()

    def record_unverified_item(self, item: str) -> None:
        items = self.data.setdefault("unverified_items", [])
        if item and item not in items:
            items.append(item)
        self.save()

    # ------------------------------------------------------------------
    # Final JSON & Markdown Report Generation
    # ------------------------------------------------------------------
    def generate_final_reports(
        self,
        plan: PhasePlan,
        locked_files: List[Dict[str, Any]],
        live_requests: List[Dict[str, Any]],
    ) -> Tuple[Path, Path, Dict[str, Any], str]:
        self.mark_finished()

        duration_s = float(self.data.get("elapsed_seconds") or 0.0)
        duration_min = round(duration_s / 60.0, 2)

        prompt_tok = int(self.data["prompt_tokens"])
        comp_tok = int(self.data["completion_tokens"])
        reason_tok = int(self.data["reasoning_tokens"])
        included = bool(self.data.get("reasoning_included_in_completion", True))
        output_non_reasoning = max(0, comp_tok - reason_tok) if included else comp_tok
        total_tok = int(self.data["total_tokens"])

        all_modified: List[str] = []
        phase_reports: List[Dict[str, Any]] = []
        for p in plan.phases:
            for f in p.created_or_modified_files:
                if f not in all_modified:
                    all_modified.append(f)
            phase_reports.append(
                {
                    "id": p.id,
                    "name": p.name,
                    "status": p.status,
                    "summary": p.summary,
                    "completed_at": p.completed_at,
                    "created_or_modified_files": list(p.created_or_modified_files),
                    "locked_outputs": list(p.locked_outputs),
                    "verification_history": list(p.verification_history),
                }
            )

        report_obj: Dict[str, Any] = {
            "project_workspace": str(self.config.workspace_dir),
            "plan_id": plan.plan_id,
            "started_at": self.data["started_at"],
            "ended_at": self.data["ended_at"],
            "duration_seconds": duration_s,
            "duration_minutes": duration_min,
            "active_model": self.data["active_model"],
            "engine_version": self.data.get("engine_version"),
            "api_base_url": self.data["api_base_url"],
            "context_setting": self.data["context_setting"],
            "detected_context_capacity": self.data["detected_context_capacity"],
            "tokens": {
                "input_read_tokens": prompt_tok,
                "prompt_tokens_read": int(self.data.get("prompt_tokens_read") or 0),
                "prompt_tokens_reused": int(self.data.get("prompt_tokens_reused") or 0),
                "completion_total_tokens": comp_tok,
                "reasoning_think_tokens": reason_tok,
                "output_write_tokens_excluding_reasoning": output_non_reasoning,
                "reasoning_included_in_completion": included,
                "reasoning_tokens_estimated": bool(self.data.get("reasoning_tokens_estimated", False)),
                "total_processed_tokens": total_tok,
            },
            "counters": {
                "request_count": self.data["request_count"],
                "tool_call_count": self.data["tool_call_count"],
                "inline_files_written": self.data["inline_files_written"],
                "compaction_count": self.data["compaction_count"],
                "retry_count": self.data["retry_count"],
                "error_count": self.data["error_count"],
                "thought_watchdog_triggers": int(self.data.get("thought_watchdog_triggers") or 0),
                "history_chars_compacted": int(self.data.get("history_chars_compacted") or 0),
            },
            "strata_telemetry": self.data.get("strata_telemetry_delta"),
            "phases": phase_reports,
            "files_created_or_modified": sorted(all_modified),
            "locked_files": locked_files,
            "live_requests": live_requests,
            "known_blockers": list(self.data.get("known_blockers", [])),
            "unverified_items": list(self.data.get("unverified_items", [])),
        }

        reason_label = " (estimated)" if report_obj["tokens"]["reasoning_tokens_estimated"] else ""
        md_lines = [
            "# LAPO Final Execution Report",
            "",
            "## Summary",
            f"- **Workspace:** `{report_obj['project_workspace']}`",
            f"- **Model:** `{report_obj['active_model']}`",
            f"- **Started:** `{report_obj['started_at']}`",
            f"- **Ended:** `{report_obj['ended_at']}`",
            f"- **Duration:** {duration_min} min ({duration_s} s)",
            f"- **Context Config:** `{report_obj['context_setting']}` (Detected: `{report_obj['detected_context_capacity']}`)",
            "",
            "## Token Consumption (No Double-Counting)",
            f"- **Read (Input/Prompt Total):** {prompt_tok:,} ({format_token_count(prompt_tok)}) — "
            f"Newly Read: {int(self.data.get('prompt_tokens_read') or 0):,} | "
            f"Cache Reused: {int(self.data.get('prompt_tokens_reused') or 0):,}",
            f"- **Think (Reasoning{reason_label}):** {reason_tok:,} ({format_token_count(reason_tok)})",
            f"- **Write (Output excl. Reasoning):** {output_non_reasoning:,} ({format_token_count(output_non_reasoning)})",
            f"- **Completion Total:** {comp_tok:,} ({format_token_count(comp_tok)})",
            f"- **Grand Total Processed:** {total_tok:,} ({format_token_count(total_tok)})",
            "",
            "## Execution Counters",
            f"- **Model Requests:** {self.data['request_count']}",
            f"- **Tool Calls:** {self.data['tool_call_count']}",
            f"- **Inline `<file>` Writes:** {self.data['inline_files_written']}",
            f"- **Context Compactions:** {self.data['compaction_count']}",
            f"- **Retries / Errors:** {self.data['retry_count']} / {self.data['error_count']}",
            f"- **Thought Watchdog Triggers:** {int(self.data.get('thought_watchdog_triggers') or 0)}",
            f"- **History Chars Compacted (Append-Only Hygiene):** {int(self.data.get('history_chars_compacted') or 0):,}",
            "",
            "## Phase-by-Phase Results",
        ]

        for pr in phase_reports:
            md_lines.append(f"### Phase {pr['id']}: {pr['name']} — `{pr['status']}`")
            if pr["summary"]:
                md_lines.append(f"- **Summary:** {pr['summary']}")
            md_lines.append(f"- **Files Modified:** {', '.join(pr['created_or_modified_files']) or 'None'}")
            md_lines.append(f"- **Locked Outputs:** {', '.join(pr['locked_outputs']) or 'None'}")
            md_lines.append("")

        md_lines.extend(
            [
                "## Locked Files",
                *([f"- `{lf['path']}` (Phase {lf['owner_phase_id']}, sha256:`{lf['sha256'][:12]}`)" for lf in locked_files] or ["- None"]),
                "",
                "## Known Blockers",
                *([f"- {b}" for b in report_obj["known_blockers"]] or ["- None"]),
                "",
                "## Unverified Items",
                *([f"- {u}" for u in report_obj["unverified_items"]] or ["- None"]),
            ]
        )
        md_text = "\n".join(md_lines) + "\n"

        self.config.reports_dir.mkdir(parents=True, exist_ok=True)
        json_path = self.config.reports_dir / "final_report.json"
        md_path = self.config.reports_dir / "final_report.md"
        atomic_write_json(json_path, report_obj)
        atomic_write_text(md_path, md_text)
        return json_path, md_path, report_obj, md_text