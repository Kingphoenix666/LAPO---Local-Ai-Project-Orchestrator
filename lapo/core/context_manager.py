"""
Dynamic context window manager.
Strictly distinguishes:
1. Actual detected model/server context capacity
2. Configured LAPO management ceiling
3. Current active conversation tokens (exact POST /tokenize or estimated)
4. Reserved generation headroom
5. Compaction floor, trigger, and hard-force thresholds
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from lapo.adapters.strata_adapter import ChatCompletionResult, StrataAdapter
from lapo.config import RuntimeConfig, format_token_count


DEFAULT_FALLBACK_CAPACITY = 32768
MIN_SAFE_GEN_TOKENS = 1024


@dataclass
class ContextSnapshot:
    detected_capacity: Optional[int]
    effective_ceiling: int
    active_tokens: int
    is_exact: bool
    min_no_compact_floor: int
    compaction_trigger: int
    hard_force_trigger: int
    compaction_reserve: int
    available_headroom: int
    trigger_progress_pct: float
    ceiling_usage_pct: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "detected_capacity": self.detected_capacity,
            "effective_ceiling": self.effective_ceiling,
            "active_tokens": self.active_tokens,
            "is_exact": self.is_exact,
            "min_no_compact_floor": self.min_no_compact_floor,
            "compaction_trigger": self.compaction_trigger,
            "hard_force_trigger": self.hard_force_trigger,
            "compaction_reserve": self.compaction_reserve,
            "available_headroom": self.available_headroom,
            "trigger_progress_pct": round(self.trigger_progress_pct, 1),
            "ceiling_usage_pct": round(self.ceiling_usage_pct, 1),
        }

    def summary_line(self) -> str:
        mode = "exact" if self.is_exact else "est"
        return (
            f"Active: {format_token_count(self.active_tokens)} ({mode}) / "
            f"Ceiling: {format_token_count(self.effective_ceiling)} | "
            f"Compact Trigger ({format_token_count(self.compaction_trigger)}): {self.trigger_progress_pct:.1f}%"
        )


class ContextManager:
    """Computes safe dynamic token budgets and compaction thresholds for any model size."""

    def __init__(
        self,
        config: RuntimeConfig,
        adapter: StrataAdapter,
        detected_capacity: Optional[int] = None,
    ) -> None:
        self.config = config
        self.adapter = adapter
        self.detected_capacity: Optional[int] = detected_capacity
        self.last_engine_tokens: int = 0

    def update_detected_capacity(self, detected_capacity: Optional[int]) -> None:
        if detected_capacity and detected_capacity > 0:
            self.detected_capacity = int(detected_capacity)

    @property
    def effective_ceiling(self) -> int:
        if self.config.context_setting == "auto":
            if self.detected_capacity and self.detected_capacity >= 2048:
                return self.detected_capacity
            return DEFAULT_FALLBACK_CAPACITY

        manual_ceiling = int(self.config.context_setting)
        if self.detected_capacity and self.detected_capacity >= 2048:
            return min(manual_ceiling, self.detected_capacity)
        return manual_ceiling

    @property
    def compaction_reserve(self) -> int:
        ceiling = self.effective_ceiling
        return min(16384, max(1024, int(ceiling * 0.12)))

    @property
    def min_no_compact_floor(self) -> int:
        return int(self.effective_ceiling * self.config.min_no_compact_pct)

    @property
    def compaction_trigger(self) -> int:
        return int(self.effective_ceiling * self.config.compaction_threshold_pct)

    @property
    def hard_force_trigger(self) -> int:
        return int(self.effective_ceiling * max(self.config.hard_force_pct, self.config.compaction_threshold_pct + 0.05))

    def count_messages_tokens(
        self,
        messages: List[Dict[str, Any]],
        tools_schema: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[int, bool]:
        parts: List[str] = []
        for msg in messages:
            role = msg.get("role") or ""
            content = msg.get("content") or ""
            reasoning = msg.get("reasoning_content") or ""
            tc_str = json.dumps(msg.get("tool_calls", []), ensure_ascii=False) if msg.get("tool_calls") else ""
            parts.append(f"<|{role}|>\n{reasoning}\n{content}\n{tc_str}")

        if tools_schema and self.config.tool_protocol in ("auto", "native"):
            parts.append(json.dumps(tools_schema, ensure_ascii=False))

        full_text = "\n".join(parts)
        return self.adapter.count_tokens(full_text)

    def extract_post_flight_tokens(self, result: ChatCompletionResult, fallback_estimate: int = 0) -> int:
        timings = result.timings
        if isinstance(timings, dict) and ("prompt_n" in timings or "cache_n" in timings):
            total_t = (
                int(timings.get("prompt_n") or 0)
                + int(timings.get("cache_n") or 0)
                + int(timings.get("predicted_n") or 0)
            )
            if total_t > 0:
                self.last_engine_tokens = total_t
                return total_t

        usage = result.usage
        if isinstance(usage, dict):
            total_u = int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
            if total_u > 0:
                self.last_engine_tokens = total_u
                return total_u

        self.last_engine_tokens = fallback_estimate
        return fallback_estimate

    def compute_dynamic_max_tokens(self, active_tokens: int, for_compaction: bool = False) -> int:
        if for_compaction:
            return self.compaction_reserve

        usable_ceiling = self.effective_ceiling - self.compaction_reserve
        available = usable_ceiling - active_tokens
        if available < MIN_SAFE_GEN_TOKENS:
            return MIN_SAFE_GEN_TOKENS
        return min(self.config.max_gen_tokens, available)

    def needs_preflight_compaction(self, active_tokens: int) -> bool:
        """True if active context already reached the compaction trigger or headroom ceiling."""
        if active_tokens >= self.compaction_trigger and active_tokens >= self.min_no_compact_floor:
            return True
        return (active_tokens + MIN_SAFE_GEN_TOKENS + self.compaction_reserve) >= self.effective_ceiling

    def should_trigger_compaction(self, active_tokens: int, wrote_or_tested: bool = True) -> bool:
        """Trigger compaction deterministically as soon as active context reaches the configured trigger."""
        if active_tokens < self.min_no_compact_floor:
            return False
        if active_tokens >= self.hard_force_trigger:
            return True
        if active_tokens >= self.compaction_trigger:
            return True
        return False

    def reset_tracking(self) -> None:
        self.last_engine_tokens = 0

    def snapshot(self, active_tokens: int, is_exact: bool) -> ContextSnapshot:
        ceiling = max(1, self.effective_ceiling)
        trigger = max(1, self.compaction_trigger)
        headroom = max(0, ceiling - self.compaction_reserve - active_tokens)
        return ContextSnapshot(
            detected_capacity=self.detected_capacity,
            effective_ceiling=ceiling,
            active_tokens=active_tokens,
            is_exact=is_exact,
            min_no_compact_floor=self.min_no_compact_floor,
            compaction_trigger=trigger,
            hard_force_trigger=self.hard_force_trigger,
            compaction_reserve=self.compaction_reserve,
            available_headroom=headroom,
            trigger_progress_pct=(active_tokens / float(trigger)) * 100.0,
            ceiling_usage_pct=(active_tokens / float(ceiling)) * 100.0,
        )