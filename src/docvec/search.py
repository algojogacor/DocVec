from __future__ import annotations

from datetime import date, datetime
from dataclasses import replace
from pathlib import Path
from typing import Any

from docvec.embeddings import Embedder
from docvec.models import SearchResult
from docvec.storage.db import DocVecDB
from docvec.vectors import VectorBackend

SEMANTIC_CANDIDATE_OVERFETCH_FACTOR = 8
SEMANTIC_CANDIDATE_MINIMUM = 50


class DocVecSearch:
    def __init__(self, db: DocVecDB, embedder: Embedder, vectors: VectorBackend) -> None:
        self.db = db
        self.embedder = embedder
        self.vectors = vectors

    def search(
        self,
        query: str,
        limit: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        if limit <= 0:
            return []
        query = query.strip()
        if not query:
            return []

        candidate_limit = _candidate_limit(limit, filters or {})
        try:
            semantic_candidates = self._semantic_search(query, candidate_limit)
        except Exception:
            semantic_candidates = []
        semantic_results = self._filter_results(semantic_candidates, filters or {})[:limit]
        fts_results = self._filter_results(
            self.db.search_fts(query, limit=candidate_limit),
            filters or {},
        )[:limit]

        by_id: dict[int, SearchResult] = {}
        fused_scores: dict[int, float] = {}
        for results, weight in ((semantic_results, 1.0), (fts_results, 1.25)):
            for rank, result in enumerate(results, start=1):
                contribution = weight / (rank + 60)
                current = by_id.get(result.chunk_id)
                fused_scores[result.chunk_id] = fused_scores.get(result.chunk_id, 0.0) + contribution
                if current is None:
                    by_id[result.chunk_id] = replace(result, score=fused_scores[result.chunk_id])
                else:
                    by_id[result.chunk_id] = replace(
                        current,
                        score=fused_scores[result.chunk_id],
                        rank_source="hybrid",
                    )

        return sorted(by_id.values(), key=lambda item: item.score, reverse=True)[:limit]

    def _semantic_search(self, query: str, limit: int) -> list[SearchResult]:
        vector = self.embedder.embed([query])[0]
        candidate_limit = max(
            limit * SEMANTIC_CANDIDATE_OVERFETCH_FACTOR,
            SEMANTIC_CANDIDATE_MINIMUM,
        )
        matches = self.vectors.search(vector, k=candidate_limit)
        results: list[SearchResult] = []
        for chunk_id, score in matches:
            try:
                chunk = self.db.get_chunk(chunk_id)
            except KeyError:
                continue
            results.append(
                SearchResult(
                    chunk_id=chunk_id,
                    source_path=chunk.source_path,
                    source_kind=chunk.source_kind,
                    title=chunk.title,
                    snippet=chunk.text[:240],
                    score=float(score),
                    rank_source="vector",
                    metadata=chunk.metadata,
                )
            )
            if len(results) >= limit:
                break
        return results

    def _filter_results(
        self,
        results: list[SearchResult],
        filters: dict[str, Any],
    ) -> list[SearchResult]:
        normalized = _normalize_filters(filters)
        if not normalized:
            return results
        return [result for result in results if _matches_filters(result, normalized)]


def _candidate_limit(limit: int, filters: dict[str, Any]) -> int:
    if not filters:
        return limit
    return max(limit * SEMANTIC_CANDIDATE_OVERFETCH_FACTOR, SEMANTIC_CANDIDATE_MINIMUM)


def _normalize_filters(filters: dict[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key in (
        "path_contains",
        "source_kind",
        "drive",
        "extension",
        "project",
        "file_type",
        "date_from",
        "date_to",
    ):
        value = str(filters.get(key, "")).strip()
        if value:
            normalized[key] = value.lower()
    if "drive" in normalized and not normalized["drive"].endswith(":"):
        normalized["drive"] = normalized["drive"].rstrip("\\/") + ":"
    if "extension" in normalized and not normalized["extension"].startswith("."):
        normalized["extension"] = "." + normalized["extension"]
    return normalized


def _matches_filters(result: SearchResult, filters: dict[str, str]) -> bool:
    source_path = result.source_path.lower()
    if path_contains := filters.get("path_contains"):
        if path_contains not in source_path:
            return False
    if source_kind := filters.get("source_kind"):
        if result.source_kind.value.lower() != source_kind:
            return False
    if drive := filters.get("drive"):
        if not source_path.startswith(drive):
            return False
    if extension := filters.get("extension"):
        if _result_extension(result) != extension:
            return False
    if project := filters.get("project"):
        if not _matches_project(result, project):
            return False
    if file_type := filters.get("file_type"):
        if _result_file_type(result) != _normalize_file_type(file_type):
            return False
    if "date_from" in filters or "date_to" in filters:
        result_date = _result_date(result)
        if result_date is None:
            return False
        date_from = _parse_date(filters.get("date_from", ""))
        date_to = _parse_date(filters.get("date_to", ""))
        if date_from is not None and result_date < date_from:
            return False
        if date_to is not None and result_date > date_to:
            return False
    return True


def _result_extension(result: SearchResult) -> str:
    metadata_extension = result.metadata.get("extension", "").strip().lower()
    if metadata_extension:
        return metadata_extension if metadata_extension.startswith(".") else f".{metadata_extension}"
    return Path(result.source_path.split("#", 1)[0]).suffix.lower()


def _matches_project(result: SearchResult, project: str) -> bool:
    metadata_candidates = (
        result.metadata.get("project", ""),
        result.metadata.get("project_name", ""),
        result.metadata.get("repository", ""),
        result.metadata.get("repo", ""),
    )
    if any(project in candidate.lower() for candidate in metadata_candidates if candidate):
        return True
    return any(project in part for part in _path_parts(result.source_path))


def _result_file_type(result: SearchResult) -> str:
    metadata_file_type = result.metadata.get("file_type", "").strip().lower()
    if metadata_file_type:
        return _normalize_file_type(metadata_file_type)

    if result.source_kind.value == "ai_session":
        return "session"
    if result.source_kind.value == "ai_memory":
        return "memory"

    extension = _result_extension(result)
    if extension in _TRANSCRIPT_EXTENSIONS:
        return "transcript"
    if extension in _CONFIG_EXTENSIONS:
        return "config"
    if extension in _CODE_EXTENSIONS:
        return "code"
    if extension in _DOCUMENT_EXTENSIONS:
        return "document"
    return "file"


def _normalize_file_type(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    aliases = {
        "ai-session": "session",
        "sessions": "session",
        "ai-memory": "memory",
        "memories": "memory",
        "docs": "document",
        "documents": "document",
        "captions": "transcript",
        "subtitle": "transcript",
        "subtitles": "transcript",
        "transcripts": "transcript",
        "configs": "config",
        "settings": "config",
        "source": "code",
        "source-code": "code",
    }
    return aliases.get(normalized, normalized)


def _result_date(result: SearchResult) -> date | None:
    for key in (
        "date",
        "timestamp",
        "created_at",
        "updated_at",
        "modified_at",
        "mtime",
        "file_mtime",
    ):
        parsed = _parse_date(result.metadata.get(key, ""))
        if parsed is not None:
            return parsed
    return None


def _parse_date(value: str) -> date | None:
    value = value.strip()
    if not value:
        return None
    if len(value) >= 10:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _path_parts(source_path: str) -> list[str]:
    normalized = source_path.split("#", 1)[0].replace("/", "\\").lower()
    return [part for part in normalized.split("\\") if part]


_CODE_EXTENSIONS = {
    ".bat",
    ".c",
    ".cmd",
    ".cpp",
    ".cs",
    ".css",
    ".dart",
    ".go",
    ".h",
    ".html",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".lua",
    ".php",
    ".ps1",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".swift",
    ".tsx",
    ".ts",
    ".vue",
}

_CONFIG_EXTENSIONS = {
    ".cfg",
    ".conf",
    ".env",
    ".ini",
    ".json",
    ".lock",
    ".properties",
    ".toml",
    ".xml",
    ".yaml",
    ".yml",
}

_DOCUMENT_EXTENSIONS = {
    ".csv",
    ".doc",
    ".docx",
    ".htm",
    ".md",
    ".pdf",
    ".ppt",
    ".pptx",
    ".rtf",
    ".txt",
    ".xls",
    ".xlsx",
}

_TRANSCRIPT_EXTENSIONS = {".srt", ".vtt", ".ass"}
