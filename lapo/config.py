"""Dynamic runtime configuration for LAPO (zero hardcoded project/port assumptions)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


ContextSetting = Union[str, int]  # "auto" or integer token ceiling

DEFAULT_API_BASE_URL = "http://127.0.0.1:47831/v1"
DEFAULT_API_KEY = "strata-local"
NO_PROXY_HOSTS = "127.0.0.1,localhost,::1"
LOCAL_BYPASS_PROXIES: Dict[str, Optional[str]] = {"http": None, "https": None}

SUPPORTED_TOOL_PROTOCOLS = ("auto", "native", "text")


def parse_context_size(raw: Union[str, int, None]) -> ContextSetting:
    if raw is None:
        return "auto"
    if isinstance(raw, int):
        if raw < 2048:
            raise ValueError(f"Context ceiling too small ({raw}); minimum is 2048 tokens.")
        return raw

    text = str(raw).strip().lower()
    if not text or text == "auto":
        return "auto"

    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([km]?)", text)
    if not match:
        raise ValueError(
            f"Invalid context value '{raw}'. Use 'auto', an integer (e.g. 32768), or K/M suffix (e.g. 64K, 1M)."
        )

    num = float(match.group(1))
    suffix = match.group(2)
    multiplier = 1
    if suffix == "k":
        multiplier = 1024
    elif suffix == "m":
        multiplier = 1024 * 1024

    tokens = int(num * multiplier)
    if tokens < 2048:
        raise ValueError(f"Context ceiling too small ({tokens}); minimum is 2048 tokens.")
    return tokens


def format_token_count(tokens: int) -> str:
    if tokens >= 1024 * 1024:
        return f"{tokens / (1024 * 1024):.2f}M"
    if tokens >= 1024:
        return f"{tokens / 1024:.1f}K"
    return str(tokens)


def normalize_api_base_url(url: str) -> str:
    cleaned = (url or "").strip().rstrip("/")
    if not cleaned:
        raise ValueError("API Base URL cannot be empty.")
    if not (cleaned.startswith("http://") or cleaned.startswith("https://")):
        cleaned = f"http://{cleaned}"
    return cleaned


def derive_server_root_url(base_url: str) -> str:
    cleaned = normalize_api_base_url(base_url)
    if cleaned.endswith("/v1"):
        return cleaned[:-3].rstrip("/")
    return cleaned


@dataclass
class RuntimeConfig:
    """Complete dynamic configuration shared by CLI and Desktop GUI."""

    workspace_dir: Path
    api_base_url: str = DEFAULT_API_BASE_URL
    api_key: str = DEFAULT_API_KEY
    model_name: str = ""
    skills_dir: Optional[Path] = None

    # Context & compaction parameters
    context_setting: ContextSetting = "auto"
    compaction_threshold_pct: float = 0.75
    min_no_compact_pct: float = 0.50
    hard_force_pct: float = 0.85
    max_gen_tokens: int = 32768
    max_tool_output_chars: int = 32000

    # Autonomous verification (default True: never block unattended runs on human clicks)
    unattended_auto_verify: bool = True

    # Inference & resilience parameters
    temperature: float = 0.15
    tool_protocol: str = "auto"  # "auto" | "native" | "text"
    connect_timeout_s: int = 30
    read_timeout_s: int = 5400  # 90 minutes for heavy MoE SSD expert loading
    max_api_retries: int = 5
    retry_backoff_s: int = 20

    # Optional external proxies for verification subprocesses (loopback always bypassed)
    primary_proxy: Optional[str] = None
    fallback_proxy: Optional[str] = None

    # Execution safety & verification allowlist
    allowed_commands: List[str] = field(default_factory=list)
    max_file_size_bytes: int = 5 * 1024 * 1024  # 5 MB
    live_request_poll_s: float = 1.0

    def __post_init__(self) -> None:
        self.workspace_dir = Path(self.workspace_dir).expanduser().resolve()
        if self.skills_dir is not None and str(self.skills_dir).strip():
            self.skills_dir = Path(self.skills_dir).expanduser().resolve()
        else:
            self.skills_dir = None

        self.api_base_url = normalize_api_base_url(self.api_base_url)
        self.context_setting = parse_context_size(self.context_setting)

        if self.tool_protocol not in SUPPORTED_TOOL_PROTOCOLS:
            raise ValueError(
                f"Invalid tool_protocol '{self.tool_protocol}'. Must be one of {SUPPORTED_TOOL_PROTOCOLS}."
            )
        if not (0.25 <= self.compaction_threshold_pct <= 0.95):
            raise ValueError("compaction_threshold_pct must be between 0.25 and 0.95.")
        if self.max_gen_tokens < 512:
            raise ValueError("max_gen_tokens must be at least 512.")
        if self.read_timeout_s < 10:
            raise ValueError("read_timeout_s must be at least 10 seconds.")

    @property
    def server_url(self) -> str:
        return derive_server_root_url(self.api_base_url)

    @property
    def orchestrator_dir(self) -> Path:
        return self.workspace_dir / ".orchestrator"

    @property
    def state_file(self) -> Path:
        return self.orchestrator_dir / "state.json"

    @property
    def journal_file(self) -> Path:
        return self.orchestrator_dir / "journal.json"

    @property
    def plan_file(self) -> Path:
        return self.orchestrator_dir / "phase_execution_plan.json"

    @property
    def locks_file(self) -> Path:
        return self.orchestrator_dir / "locks.json"

    @property
    def pending_requests_file(self) -> Path:
        return self.orchestrator_dir / "pending_requests.json"

    @property
    def repair_queue_file(self) -> Path:
        return self.orchestrator_dir / "repair_queue.json"

    @property
    def usage_file(self) -> Path:
        return self.orchestrator_dir / "usage.json"

    @property
    def backups_dir(self) -> Path:
        return self.orchestrator_dir / "backups"

    @property
    def requests_audit_dir(self) -> Path:
        return self.orchestrator_dir / "requests"

    @property
    def reports_dir(self) -> Path:
        return self.orchestrator_dir / "reports"

    @property
    def logs_dir(self) -> Path:
        return self.orchestrator_dir / "logs"

    def ensure_orchestrator_dirs(self) -> None:
        for d in (
            self.workspace_dir,
            self.orchestrator_dir,
            self.backups_dir,
            self.requests_audit_dir,
            self.reports_dir,
            self.logs_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    def to_safe_dict(self) -> Dict[str, Any]:
        return {
            "workspace_dir": str(self.workspace_dir),
            "skills_dir": str(self.skills_dir) if self.skills_dir else None,
            "api_base_url": self.api_base_url,
            "model_name": self.model_name,
            "context_setting": self.context_setting,
            "compaction_threshold_pct": self.compaction_threshold_pct,
            "min_no_compact_pct": self.min_no_compact_pct,
            "hard_force_pct": self.hard_force_pct,
            "max_gen_tokens": self.max_gen_tokens,
            "unattended_auto_verify": self.unattended_auto_verify,
            "temperature": self.temperature,
            "tool_protocol": self.tool_protocol,
            "connect_timeout_s": self.connect_timeout_s,
            "read_timeout_s": self.read_timeout_s,
            "max_api_retries": self.max_api_retries,
            "retry_backoff_s": self.retry_backoff_s,
            "allowed_commands": list(self.allowed_commands),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], api_key_override: Optional[str] = None) -> "RuntimeConfig":
        ws = data.get("workspace_dir") or data.get("workspace")
        if not ws:
            raise ValueError("Configuration missing required field 'workspace_dir'.")

        skills = data.get("skills_dir") or data.get("skills")
        api_key = (
            api_key_override
            or data.get("api_key")
            or os.environ.get("LAPO_API_KEY")
            or os.environ.get("STRATA_API_KEY")
            or DEFAULT_API_KEY
        )

        return cls(
            workspace_dir=Path(ws),
            skills_dir=Path(skills) if skills else None,
            api_base_url=data.get("api_base_url")
            or os.environ.get("STRATA_BASE_URL")
            or DEFAULT_API_BASE_URL,
            api_key=api_key,
            model_name=data.get("model_name") or data.get("model") or "",
            context_setting=data.get("context_setting", "auto"),
            compaction_threshold_pct=float(data.get("compaction_threshold_pct", 0.75)),
            min_no_compact_pct=float(data.get("min_no_compact_pct", 0.50)),
            hard_force_pct=float(data.get("hard_force_pct", 0.85)),
            max_gen_tokens=int(data.get("max_gen_tokens", 32768)),
            max_tool_output_chars=int(data.get("max_tool_output_chars", 32000)),
            unattended_auto_verify=bool(data.get("unattended_auto_verify", True)),
            temperature=float(data.get("temperature", 0.15)),
            tool_protocol=str(data.get("tool_protocol", "auto")),
            connect_timeout_s=int(data.get("connect_timeout_s", 30)),
            read_timeout_s=int(data.get("read_timeout_s", 5400)),
            max_api_retries=int(data.get("max_api_retries", 5)),
            retry_backoff_s=int(data.get("retry_backoff_s", 20)),
            primary_proxy=data.get("primary_proxy"),
            fallback_proxy=data.get("fallback_proxy"),
            allowed_commands=list(data.get("allowed_commands", [])),
            max_file_size_bytes=int(data.get("max_file_size_bytes", 5 * 1024 * 1024)),
            live_request_poll_s=float(data.get("live_request_poll_s", 1.0)),
        )