from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class SourceKind(StrEnum):
    NORMAL_FILE = "normal_file"
    PROJECT_SOURCE = "project_source"
    AI_SESSION = "ai_session"
    AI_MEMORY = "ai_memory"
    SYSTEM_CACHE = "system_cache"
    ARCHIVE = "archive"
    MEDIA = "media"
    BINARY = "binary"


@dataclass(frozen=True)
class Classification:
    path: Path
    kind: SourceKind
    should_skip: bool
    reason: str


@dataclass(frozen=True)
class ExtractedRecord:
    source_path: str
    source_kind: SourceKind
    title: str
    text: str
    metadata: dict[str, str]


@dataclass(frozen=True)
class Chunk:
    chunk_id: int | None
    source_path: str
    source_kind: SourceKind
    title: str
    text: str
    metadata: dict[str, str]
    ordinal: int
    content_hash: str


@dataclass(frozen=True)
class SearchResult:
    chunk_id: int
    source_path: str
    source_kind: SourceKind
    title: str
    snippet: str
    score: float
    rank_source: str
    metadata: dict[str, str]
