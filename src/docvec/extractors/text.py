from __future__ import annotations

from pathlib import Path

from docvec.models import ExtractedRecord, SourceKind


def extract_text_file(path: Path, source_kind: SourceKind) -> ExtractedRecord:
    text = path.read_text(encoding="utf-8", errors="replace")
    return ExtractedRecord(
        source_path=str(path),
        source_kind=source_kind,
        title=path.name,
        text=text,
        metadata={"extension": path.suffix.lower()},
    )
