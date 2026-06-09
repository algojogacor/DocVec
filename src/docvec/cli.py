from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from rich.console import Console
import typer

from docvec.app_api import serve_app_api
from docvec.config import SQLITE_PATH, VECTOR_PATH, data_usage_bytes
from docvec.runtime import build_runtime
from docvec.storage.db import DocVecDB

app = typer.Typer(help="DocVec debug/admin CLI.")
console = Console(soft_wrap=True)


@app.command()
def status() -> None:
    """Show local DocVec status."""
    console.print(f"DocVec database: {SQLITE_PATH}")
    console.print(f"Database exists: {SQLITE_PATH.exists()}")


@app.command()
def init_db(path: Path = SQLITE_PATH) -> None:
    """Initialize the SQLite database."""
    db = DocVecDB(path)
    db.initialize()
    console.print(f"Initialized {path}")


@app.command()
def scan(
    root: list[Path] = typer.Option(..., "--root", help="Root path to scan."),
    db_path: Path = typer.Option(SQLITE_PATH, help="SQLite database path."),
    vector_path: Path = typer.Option(VECTOR_PATH, help="turbovec index path."),
    fake: bool = typer.Option(False, help="Use deterministic in-memory embeddings."),
    include_archives: bool = typer.Option(
        False,
        help="Index user-approved .zip archives instead of skipping archives.",
    ),
) -> None:
    """Run a debug indexing scan."""
    runtime = build_runtime(
        db_path=db_path,
        vector_path=vector_path,
        fake=fake,
        include_archives=include_archives,
    )
    summary = runtime.crawler.crawl(root)
    console.print(
        f"{summary.status}: discovered={summary.discovered_count} "
        f"indexed={summary.indexed_count} skipped={summary.skipped_count} "
        f"errors={summary.error_count}"
    )


@app.command()
def search(
    query: str,
    limit: int = typer.Option(10, min=1, max=50, help="Maximum result count."),
    db_path: Path = typer.Option(SQLITE_PATH, help="SQLite database path."),
    vector_path: Path = typer.Option(VECTOR_PATH, help="turbovec index path."),
    fake: bool = typer.Option(False, help="Use deterministic in-memory embeddings."),
) -> None:
    """Search the local DocVec index."""
    runtime = build_runtime(db_path=db_path, vector_path=vector_path, fake=fake)
    results = runtime.search.search(query, limit=limit)
    if not results:
        console.print("No results.")
        return

    for result in results:
        console.print(
            _console_safe(f"[{result.rank_source}] {result.title} :: {result.source_path}"),
            markup=False,
        )
        console.print(_console_safe(f"  {result.snippet}"), markup=False)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Local bind host."),
    port: int = typer.Option(8765, min=1, max=65535, help="Local API port."),
    db_path: Path = typer.Option(SQLITE_PATH, help="SQLite database path."),
    vector_path: Path = typer.Option(VECTOR_PATH, help="turbovec index path."),
    fake: bool = typer.Option(False, help="Use deterministic in-memory embeddings."),
) -> None:
    """Serve the local JSON API for the desktop app."""
    runtime = build_runtime(db_path=db_path, vector_path=vector_path, fake=fake)
    console.print(f"Serving DocVec API on http://{host}:{port}")
    serve_app_api(runtime=runtime, host=host, port=port)


@app.command()
def source_stats(
    db_path: Path = typer.Option(SQLITE_PATH, help="SQLite database path."),
) -> None:
    """Inspect indexed source and chunk counts."""
    db = DocVecDB(db_path)
    db.initialize()
    with db.connect() as connection:
        source_rows = connection.execute(
            """
            SELECT source_kind, status, COUNT(*) AS count
            FROM source_files
            GROUP BY source_kind, status
            ORDER BY source_kind, status
            """
        ).fetchall()
        chunk_rows = connection.execute(
            """
            SELECT source_kind, COUNT(*) AS count
            FROM chunks
            WHERE active = 1
            GROUP BY source_kind
            ORDER BY source_kind
            """
        ).fetchall()

    console.print("Source files:")
    if not source_rows:
        console.print("  none")
    for row in source_rows:
        console.print(f"  {row['source_kind']} {row['status']} {row['count']}")

    console.print("Active chunks:")
    if not chunk_rows:
        console.print("  none")
    for row in chunk_rows:
        console.print(f"  {row['source_kind']} {row['count']}")


@app.command()
def rebuild_vectors(
    db_path: Path = typer.Option(SQLITE_PATH, help="SQLite database path."),
    vector_path: Path = typer.Option(VECTOR_PATH, help="turbovec index path."),
    fake: bool = typer.Option(False, help="Use deterministic in-memory embeddings."),
    batch_size: int = typer.Option(32, min=1, max=256, help="Embedding batch size."),
) -> None:
    """Rebuild the vector index from active SQLite chunks."""
    if not fake and vector_path.exists():
        vector_path.unlink()
    runtime = build_runtime(db_path=db_path, vector_path=vector_path, fake=fake)
    with runtime.db.connect() as connection:
        rows = connection.execute(
            """
            SELECT id
            FROM chunks
            WHERE active = 1
            ORDER BY id
            """
        ).fetchall()

    rebuilt = 0
    chunk_ids = [int(row["id"]) for row in rows]
    for offset in range(0, len(chunk_ids), batch_size):
        batch_ids = chunk_ids[offset : offset + batch_size]
        chunks = [runtime.db.get_chunk(chunk_id) for chunk_id in batch_ids]
        missing = [
            (chunk.chunk_id, chunk.text)
            for chunk in chunks
            if chunk.chunk_id is not None and not runtime.vectors.contains(chunk.chunk_id)
        ]
        if not missing:
            continue
        ids = [int(chunk_id) for chunk_id, _text in missing]
        texts = [text for _chunk_id, text in missing]
        runtime.vectors.add(ids, runtime.search.embedder.embed(texts))
        rebuilt += len(ids)

    runtime.vectors.save()
    console.print(f"rebuilt={rebuilt}")


@app.command()
def retry_errors(
    db_path: Path = typer.Option(SQLITE_PATH, help="SQLite database path."),
    vector_path: Path = typer.Option(VECTOR_PATH, help="turbovec index path."),
    fake: bool = typer.Option(False, help="Use deterministic in-memory embeddings."),
) -> None:
    """Retry sources that previously failed during indexing."""
    runtime = build_runtime(db_path=db_path, vector_path=vector_path, fake=fake)
    with runtime.db.connect() as connection:
        rows = connection.execute(
            """
            SELECT path
            FROM source_files
            WHERE status IN ('error', 'vector_pending')
            ORDER BY path
            """
        ).fetchall()

    retried = 0
    indexed = 0
    skipped = 0
    errors = 0
    missing = 0
    for row in rows:
        path = Path(str(row["path"]))
        if not path.exists():
            missing += 1
        summary = runtime.crawler.crawl([path])
        retried += 1
        indexed += summary.indexed_count
        skipped += summary.skipped_count
        errors += summary.error_count

    console.print(
        f"retried={retried} indexed={indexed} skipped={skipped} "
        f"errors={errors} missing={missing}"
    )


@app.command()
def vacuum(
    db_path: Path = typer.Option(SQLITE_PATH, help="SQLite database path."),
) -> None:
    """Vacuum/compact the SQLite database."""
    db = DocVecDB(db_path)
    db.initialize()
    with db.connect() as connection:
        connection.execute("VACUUM")
    console.print(f"Vacuumed {db_path}")


@app.command()
def export_diagnostics(
    output: Path = typer.Option(..., "--output", help="Diagnostics JSON output path."),
    db_path: Path = typer.Option(SQLITE_PATH, help="SQLite database path."),
    vector_path: Path = typer.Option(VECTOR_PATH, help="turbovec index path."),
    include_samples: bool = typer.Option(
        False,
        help="Include active chunk text samples for deeper diagnostics.",
    ),
    sample_limit: int = typer.Option(5, min=1, max=50, help="Maximum sample chunks."),
    redact_secrets: bool = typer.Option(
        False,
        help="Redact token-like values from exported diagnostic text.",
    ),
) -> None:
    """Export a compact diagnostics JSON file."""
    db = DocVecDB(db_path)
    db.initialize()
    with db.connect() as connection:
        latest_job = connection.execute(
            """
            SELECT *
            FROM index_jobs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        source_status_rows = connection.execute(
            """
            SELECT source_kind, status, COUNT(*) AS count
            FROM source_files
            GROUP BY source_kind, status
            ORDER BY source_kind, status
            """
        ).fetchall()
        chunk_rows = connection.execute(
            """
            SELECT source_kind, COUNT(*) AS count
            FROM chunks
            WHERE active = 1
            GROUP BY source_kind
            ORDER BY source_kind
            """
        ).fetchall()
        sample_rows = []
        if include_samples:
            sample_rows = connection.execute(
                """
                SELECT
                  id,
                  source_path,
                  source_kind,
                  title,
                  text,
                  metadata_json,
                  ordinal
                FROM chunks
                WHERE active = 1
                ORDER BY id
                LIMIT ?
                """,
                (sample_limit,),
            ).fetchall()

    payload = {
        "database_path": str(db_path),
        "database_exists": db_path.exists(),
        "vector_path": str(vector_path),
        "vector_exists": vector_path.exists(),
        "data_usage_bytes": data_usage_bytes(db_path.parent),
        "latest_job": _row_to_dict(latest_job),
        "source_status_counts": [_row_to_dict(row) for row in source_status_rows],
        "active_chunk_counts": [_row_to_dict(row) for row in chunk_rows],
    }
    if include_samples:
        payload["chunk_samples"] = [_chunk_sample_to_dict(row) for row in sample_rows]
    if redact_secrets:
        payload = _redact_payload(payload)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"Exported diagnostics to {output}")


def _console_safe(text: str, encoding: str | None = None) -> str:
    text = text.replace("\ufeff", "")
    target_encoding = encoding or getattr(console.file, "encoding", None) or "utf-8"
    return text.encode(target_encoding, errors="replace").decode(
        target_encoding,
        errors="replace",
    )


def _row_to_dict(row) -> dict:
    if row is None:
        return {}
    return {key: row[key] for key in row.keys()}


def _chunk_sample_to_dict(row) -> dict[str, Any]:
    return {
        "chunk_id": int(row["id"]),
        "source_path": str(row["source_path"]),
        "source_kind": str(row["source_kind"]),
        "title": str(row["title"]),
        "text": str(row["text"]),
        "metadata": json.loads(str(row["metadata_json"])),
        "ordinal": int(row["ordinal"]),
    }


def _redact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _redact_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    if isinstance(value, str):
        return _redact_secrets(value)
    return value


def _redact_secrets(value: str) -> str:
    value = _SECRET_ASSIGNMENT_RE.sub(_redact_assignment, value)
    return _SECRET_TOKEN_RE.sub("[REDACTED_SECRET]", value)


def _redact_assignment(match: re.Match[str]) -> str:
    return (
        f"{match.group('key')}{match.group('sep')}"
        f"{match.group('quote')}[REDACTED_SECRET]{match.group('quote')}"
    )


_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?P<key>[A-Za-z0-9_.-]*(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password|credential)[A-Za-z0-9_.-]*)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<quote>[\"']?)"
    r"(?P<value>[^\s\"';&]+)"
    r"(?P=quote)",
    flags=re.IGNORECASE,
)

_SECRET_TOKEN_RE = re.compile(
    r"\b(?:sk|ghp|gho|ghu|ghs|glpat|xox[baprs])[-_][A-Za-z0-9][A-Za-z0-9._-]{6,}\b"
)
