from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from docvec.models import ExtractedRecord, SourceKind


def _content_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text") or item.get("input_text") or item.get("output_text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts)


def extract_codex_jsonl(path: Path) -> list[ExtractedRecord]:
    records: list[ExtractedRecord] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue

            payload = event.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            if payload.get("type") != "message":
                continue

            role = payload.get("role")
            if role not in {"user", "assistant"}:
                continue

            text = _content_text(payload.get("content"))
            if not text:
                continue

            records.append(
                ExtractedRecord(
                    source_path=f"{path}#L{line_no}",
                    source_kind=SourceKind.AI_SESSION,
                    title=f"{path.name} {role} message",
                    text=text,
                    metadata={
                        "source": "codex",
                        "role": str(role),
                        "timestamp": str(event.get("timestamp", "")),
                        "line": str(line_no),
                    },
                )
            )
    return records
