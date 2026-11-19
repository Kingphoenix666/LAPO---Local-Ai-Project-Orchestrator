"""
Dual-protocol extractors for local LLMs:
1. `<file path="...">...</file>` and fenced ` ```lang file="..." ` inline file blocks
   to prevent JSON string-escaping truncation on long source files.
2. Strictly delimited `<tool_call>{...}</tool_call>` fallback for models without native tool calls.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass
class ExtractedFileBlock:
    path: str
    content: str
    format_kind: str  # "xml_file" | "fenced_file"


@dataclass
class ExtractedToolCall:
    name: str
    arguments: Dict[str, Any]
    raw_json: str


_XML_FILE_PATTERN = re.compile(
    r"<file\s+path=[\"']([^\"']+)[\"']\s*>\s*\n?(.*?)\n?</file>",
    re.DOTALL,
)

_FENCE_FILE_PATTERN = re.compile(
    r"```[A-Za-z0-9_+\-\.]*\s+(?:file|path)=[\"']([^\"']+)[\"'][^\n]*\n(.*?)```",
    re.DOTALL,
)

_TEXT_TOOL_CALL_PATTERN = re.compile(
    r"<tool_call>\s*(\{.*?\})\s*</tool_call>",
    re.DOTALL,
)


def extract_inline_file_blocks(text: str) -> List[ExtractedFileBlock]:
    """
    Extract inline file blocks without double-matching code fences nested inside `<file>` tags.
    """
    if not text:
        return []

    blocks: List[ExtractedFileBlock] = []
    consumed_spans: List[tuple[int, int]] = []

    for match in _XML_FILE_PATTERN.finditer(text):
        rel_path = match.group(1).strip()
        body = match.group(2)
        if rel_path:
            blocks.append(ExtractedFileBlock(path=rel_path, content=body, format_kind="xml_file"))
            consumed_spans.append(match.span())

    def _overlaps(start: int, end: int) -> bool:
        return any(not (end <= s or start >= e) for s, e in consumed_spans)

    for match in _FENCE_FILE_PATTERN.finditer(text):
        if _overlaps(match.start(), match.end()):
            continue
        rel_path = match.group(1).strip()
        body = match.group(2)
        if rel_path:
            blocks.append(ExtractedFileBlock(path=rel_path, content=body, format_kind="fenced_file"))

    return blocks


def extract_text_tool_calls(text: str) -> List[ExtractedToolCall]:
    """
    Strictly parse `<tool_call>{"name": "...", "arguments": {...}}</tool_call>` blocks.
    Arbitrary prose or malformed JSON is NEVER executed.
    """
    if not text or "<tool_call>" not in text:
        return []

    parsed_calls: List[ExtractedToolCall] = []
    for match in _TEXT_TOOL_CALL_PATTERN.finditer(text):
        raw = match.group(1).strip()
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue

        if not isinstance(obj, dict):
            continue
        name = obj.get("name")
        args = obj.get("arguments", {})
        if isinstance(name, str) and name.strip() and isinstance(args, dict):
            parsed_calls.append(
                ExtractedToolCall(
                    name=name.strip(),
                    arguments=args,
                    raw_json=raw,
                )
            )

    return parsed_calls