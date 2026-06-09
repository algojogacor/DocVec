from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from docvec.models import SearchResult, SourceKind
from docvec.runtime import DocVecRuntime, build_runtime

mcp = FastMCP("DocVec")
_runtime: DocVecRuntime | None = None


def _get_runtime() -> DocVecRuntime:
    global _runtime
    if _runtime is None:
        _runtime = build_runtime()
    return _runtime


def search_all_for_runtime(
    runtime: DocVecRuntime,
    *,
    query: str,
    filters: dict[str, Any] | None,
    limit: int = 10,
) -> dict[str, Any]:
    results = runtime.search.search(query, limit=_limit(limit), filters=filters or {})
    return {"ok": True, "results": _compact_results(results)}


def search_files_for_runtime(
    runtime: DocVecRuntime,
    *,
    query: str,
    filters: dict[str, Any] | None,
    limit: int = 10,
) -> dict[str, Any]:
    results = runtime.search.search(query, limit=max(_limit(limit), 30), filters=filters or {})
    file_results = [
        result
        for result in results
        if result.source_kind in {SourceKind.NORMAL_FILE, SourceKind.PROJECT_SOURCE}
    ]
    return {"ok": True, "results": _compact_results(file_results[: _limit(limit)])}


def search_sessions_for_runtime(
    runtime: DocVecRuntime,
    *,
    query: str,
    source: str | None,
    filters: dict[str, Any] | None,
    limit: int = 10,
) -> dict[str, Any]:
    results = runtime.search.search(query, limit=max(_limit(limit), 30), filters=filters or {})
    session_results = [
        result
        for result in results
        if result.source_kind == SourceKind.AI_SESSION
        and (source is None or result.metadata.get("source") == source)
    ]
    return {"ok": True, "results": _compact_results(session_results[: _limit(limit)])}


def find_secret_or_config_for_runtime(
    runtime: DocVecRuntime,
    *,
    query: str,
    limit: int = 10,
) -> dict[str, Any]:
    """Search config-like files for secret or credential related text without redaction."""
    result_limit = _limit(limit)
    results: list[SearchResult] = []
    seen_ids: set[int] = set()
    for candidate_query in _secret_query_variants(query):
        for result in runtime.search.search(
            candidate_query,
            limit=max(result_limit, 30),
            filters={"file_type": "config"},
        ):
            if result.chunk_id in seen_ids:
                continue
            seen_ids.add(result.chunk_id)
            results.append(result)
            if len(results) >= result_limit:
                break
        if len(results) >= result_limit:
            break
    return {"ok": True, "results": _compact_results(results)}


def get_context_for_runtime(runtime: DocVecRuntime, *, result_id: int) -> dict[str, Any]:
    try:
        chunk = runtime.db.get_chunk(result_id)
    except KeyError as error:
        return {"ok": False, "error": str(error)}
    neighbors = runtime.db.get_surrounding_chunks(result_id, radius=1)
    return {
        "ok": True,
        "chunk": _chunk_context_dict(chunk),
        "neighbors": [_chunk_context_dict(neighbor) for neighbor in neighbors],
    }


def open_result_for_runtime(runtime: DocVecRuntime, *, result_id: int) -> dict[str, Any]:
    context = get_context_for_runtime(runtime, result_id=result_id)
    if not context["ok"]:
        return context
    metadata = context["chunk"]["metadata"]
    payload = {
        "ok": True,
        "path": context["chunk"]["path"].split("#", 1)[0],
        "message": "Path resolved. Opening files is handled by the desktop app.",
    }
    line = _metadata_int(metadata, "start_line")
    end_line = _metadata_int(metadata, "end_line")
    if line is not None:
        payload["line"] = line
    if end_line is not None:
        payload["end_line"] = end_line
    return payload


def list_sources_for_runtime(runtime: DocVecRuntime) -> dict[str, Any]:
    with closing(runtime.db.connect()) as connection:
        rows = connection.execute(
            """
            SELECT source_kind, COUNT(*) AS count
            FROM chunks
            WHERE active = 1
            GROUP BY source_kind
            ORDER BY source_kind
            """
        ).fetchall()
    return {"ok": True, "sources": {str(row["source_kind"]): int(row["count"]) for row in rows}}


def saved_searches_for_runtime(runtime: DocVecRuntime) -> dict[str, Any]:
    return {"ok": True, "saved_searches": runtime.db.list_saved_searches()}


def summarize_project_for_runtime(
    runtime: DocVecRuntime,
    *,
    path_or_source_id: str,
    limit: int = 8,
) -> dict[str, Any]:
    project_path = _resolve_project_path(runtime, path_or_source_id)
    if project_path is None:
        return {"ok": False, "error": "project path or source id not found"}

    rows = _project_chunk_rows(runtime, project_path)
    source_kinds: dict[str, int] = {}
    source_paths: set[str] = set()
    highlights: list[dict[str, Any]] = []
    for row in rows:
        source_kind = str(row["source_kind"])
        source_kinds[source_kind] = source_kinds.get(source_kind, 0) + 1
        source_path = str(row["source_path"]).split("#", 1)[0]
        source_paths.add(source_path)
        if len(highlights) < _limit(limit):
            metadata = _json_dict(str(row["metadata_json"]))
            highlights.append(
                {
                    "id": int(row["id"]),
                    "title": str(row["title"]),
                    "path": str(row["source_path"]),
                    "snippet": str(row["text"])[:240],
                    "source_kind": source_kind,
                    "metadata": metadata,
                }
            )

    return {
        "ok": True,
        "project_path": project_path,
        "chunk_count": len(rows),
        "file_count": len(source_paths),
        "source_kinds": source_kinds,
        "summary": _project_summary(project_path, rows, highlights),
        "highlights": highlights,
    }


def index_status_for_runtime(runtime: DocVecRuntime) -> dict[str, Any]:
    with closing(runtime.db.connect()) as connection:
        row = connection.execute(
            """
            SELECT
              id,
              root_path,
              status,
              discovered_count,
              indexed_count,
              skipped_count,
              error_count,
              message,
              started_at,
              updated_at,
              finished_at
            FROM index_jobs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    return {
        "ok": True,
        "latest_job": None
        if row is None
        else {
            "id": int(row["id"]),
            "root_path": str(row["root_path"]),
            "status": str(row["status"]),
            "discovered_count": int(row["discovered_count"]),
            "indexed_count": int(row["indexed_count"]),
            "skipped_count": int(row["skipped_count"]),
            "error_count": int(row["error_count"]),
            "message": str(row["message"]),
            "started_at": str(row["started_at"]),
            "updated_at": str(row["updated_at"]),
            "finished_at": None if row["finished_at"] is None else str(row["finished_at"]),
        },
    }


@mcp.tool()
def search_all(query: str, filters: dict[str, Any] | None = None, limit: int = 10) -> dict[str, Any]:
    """Search all indexed DocVec files, sessions, and memories."""
    return search_all_for_runtime(_get_runtime(), query=query, filters=filters, limit=limit)


@mcp.tool()
def search_files(query: str, filters: dict[str, Any] | None = None, limit: int = 10) -> dict[str, Any]:
    """Search indexed normal/project files."""
    return search_files_for_runtime(_get_runtime(), query=query, filters=filters, limit=limit)


@mcp.tool()
def search_sessions(
    query: str,
    source: str | None = None,
    filters: dict[str, Any] | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Search indexed AI sessions, optionally filtered by source such as codex or hermes."""
    return search_sessions_for_runtime(
        _get_runtime(),
        query=query,
        source=source,
        filters=filters,
        limit=limit,
    )


@mcp.tool()
def get_context(result_id: int) -> dict[str, Any]:
    """Return the full chunk context for a DocVec result id."""
    return get_context_for_runtime(_get_runtime(), result_id=result_id)


@mcp.tool()
def open_result(result_id: int) -> dict[str, Any]:
    """Resolve the file path for a DocVec result id."""
    return open_result_for_runtime(_get_runtime(), result_id=result_id)


@mcp.tool()
def find_secret_or_config(query: str, limit: int = 10) -> dict[str, Any]:
    """Search config-like files for secret or credential related text."""
    return find_secret_or_config_for_runtime(_get_runtime(), query=query, limit=limit)


@mcp.tool()
def list_sources() -> dict[str, Any]:
    """List active indexed source kinds and counts."""
    return list_sources_for_runtime(_get_runtime())


@mcp.tool()
def saved_searches() -> dict[str, Any]:
    """List persisted DocVec saved searches and filters."""
    return saved_searches_for_runtime(_get_runtime())


@mcp.tool()
def summarize_project(path_or_source_id: str, limit: int = 8) -> dict[str, Any]:
    """Return an extractive overview for an indexed project path or source id."""
    return summarize_project_for_runtime(
        _get_runtime(),
        path_or_source_id=path_or_source_id,
        limit=limit,
    )


@mcp.tool()
def index_status() -> dict[str, Any]:
    """Return the latest DocVec indexing job status."""
    return index_status_for_runtime(_get_runtime())


def main() -> None:
    mcp.run()


def _compact_results(results: list[SearchResult]) -> list[dict[str, Any]]:
    return [
        {
            "id": result.chunk_id,
            "title": result.title,
            "path": result.source_path,
            "source_kind": result.source_kind.value,
            "snippet": result.snippet,
            "rank_source": result.rank_source,
            "score": result.score,
            "metadata": result.metadata,
        }
        for result in results
    ]


def _chunk_context_dict(chunk: Any) -> dict[str, Any]:
    return {
        "id": chunk.chunk_id,
        "path": chunk.source_path,
        "source_kind": chunk.source_kind.value,
        "title": chunk.title,
        "text": chunk.text,
        "metadata": chunk.metadata,
        "ordinal": chunk.ordinal,
        "content_hash": chunk.content_hash,
    }


def _limit(limit: int) -> int:
    return max(1, min(int(limit), 50))


def _secret_query_variants(query: str) -> list[str]:
    normalized = " ".join(query.split())
    terms = [term for term in normalized.split() if term]
    variants = [normalized] if normalized else []
    variants.extend(term for term in terms if term not in variants)
    for fallback in ("api key", "token", "secret", "password", "credential"):
        if fallback not in variants:
            variants.append(fallback)
    return variants


def _metadata_int(metadata: dict[str, Any], key: str) -> int | None:
    try:
        value = int(str(metadata.get(key, "")).strip())
    except ValueError:
        return None
    return value if value > 0 else None


def _resolve_project_path(runtime: DocVecRuntime, value: str) -> str | None:
    value = str(value).strip()
    if not value:
        return None
    try:
        chunk_id = int(value)
    except ValueError:
        source_path = value
    else:
        try:
            source_path = runtime.db.get_chunk(chunk_id).source_path
        except KeyError:
            return None

    physical_path = source_path.split("#", 1)[0].rstrip("\\/")
    path = Path(physical_path)
    if path.suffix:
        return str(path.parent)
    return physical_path


def _project_chunk_rows(runtime: DocVecRuntime, project_path: str) -> list[Any]:
    root = project_path.rstrip("\\/")
    backslash_pattern = _escape_like(root) + "\\%"
    slash_pattern = _escape_like(root) + "/%"
    with closing(runtime.db.connect()) as connection:
        return connection.execute(
            """
            SELECT id, source_path, source_kind, title, text, metadata_json
            FROM chunks
            WHERE active = 1
              AND (
                source_path = ?
                OR source_path LIKE ? ESCAPE '!'
                OR source_path LIKE ? ESCAPE '!'
              )
            ORDER BY source_path, ordinal, id
            """,
            (project_path, backslash_pattern, slash_pattern),
        ).fetchall()


def _project_summary(
    project_path: str,
    rows: list[Any],
    highlights: list[dict[str, Any]],
) -> str:
    if not rows:
        return f"No indexed chunks found for {project_path}."
    titles = ", ".join(highlight["title"] for highlight in highlights[:3])
    snippets = " ".join(highlight["snippet"] for highlight in highlights[:2])
    return (
        f"Indexed project {project_path} has {len(rows)} chunks. "
        f"Representative files: {titles}. {snippets}"
    )


def _json_dict(value: str) -> dict[str, str]:
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(item) for key, item in data.items()}


def _escape_like(value: str) -> str:
    return value.replace("!", "!!").replace("%", "!%").replace("_", "!_")


if __name__ == "__main__":
    main()
