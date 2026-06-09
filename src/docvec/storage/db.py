from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from docvec.models import Chunk, SearchResult, SourceKind


class DocVecDB:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        schema_path = Path(__file__).with_name("schema.sql")
        with closing(self.connect()) as connection:
            with connection:
                connection.executescript(schema_path.read_text(encoding="utf-8"))
                self._migrate(connection)

    def _migrate(self, connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(chunks)").fetchall()
        }
        if "vector_status" not in columns:
            connection.execute(
                """
                ALTER TABLE chunks
                ADD COLUMN vector_status TEXT NOT NULL DEFAULT 'indexed'
                """
            )

    def upsert_chunk(self, chunk: Chunk) -> int:
        metadata_json = json.dumps(chunk.metadata, ensure_ascii=False, sort_keys=True)
        with closing(self.connect()) as connection:
            with connection:
                return self._upsert_chunk(connection, chunk, metadata_json, activate=True)

    def stage_chunk(self, chunk: Chunk) -> int:
        metadata_json = json.dumps(chunk.metadata, ensure_ascii=False, sort_keys=True)
        with closing(self.connect()) as connection:
            with connection:
                return self._upsert_chunk(connection, chunk, metadata_json, activate=False)

    def _upsert_chunk(
        self,
        connection: sqlite3.Connection,
        chunk: Chunk,
        metadata_json: str,
        *,
        activate: bool,
    ) -> int:
        existing = connection.execute(
            """
            SELECT id
            FROM chunks
            WHERE source_path = ? AND ordinal = ? AND content_hash = ?
            """,
            (chunk.source_path, chunk.ordinal, chunk.content_hash),
        ).fetchone()

        if existing is not None:
            chunk_id = int(existing["id"])
            connection.execute(
                """
                UPDATE chunks
                SET source_kind = ?,
                    title = ?,
                    text = ?,
                    metadata_json = ?,
                    active = CASE WHEN ? THEN 1 ELSE active END,
                    vector_status = CASE
                      WHEN ? THEN 'indexed'
                      WHEN active = 0 THEN 'vector_pending'
                      ELSE vector_status
                    END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    chunk.source_kind.value,
                    chunk.title,
                    chunk.text,
                    metadata_json,
                    int(activate),
                    int(activate),
                    chunk_id,
                ),
            )
            return chunk_id

        cursor = connection.execute(
            """
            INSERT INTO chunks (
              source_path,
              source_kind,
              title,
              text,
              metadata_json,
              ordinal,
              content_hash,
              active,
              vector_status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chunk.source_path,
                chunk.source_kind.value,
                chunk.title,
                chunk.text,
                metadata_json,
                chunk.ordinal,
                chunk.content_hash,
                int(activate),
                "indexed" if activate else "vector_pending",
            ),
        )
        return int(cursor.lastrowid)

    def deactivate_chunks_for_source(self, source_path: str) -> list[int]:
        with closing(self.connect()) as connection:
            with connection:
                rows = connection.execute(
                    """
                    SELECT id
                    FROM chunks
                    WHERE source_path = ?
                      AND active = 1
                    ORDER BY id
                    """,
                    (source_path,),
                ).fetchall()
                deactivated_ids = [int(row["id"]) for row in rows]
                connection.execute(
                    """
                    UPDATE chunks
                    SET active = 0,
                        vector_status = 'inactive',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE source_path = ?
                    """,
                    (source_path,),
                )
        return deactivated_ids

    def deactivate_chunks_for_source_prefix(self, source_prefix: str) -> list[int]:
        source_pattern = _escape_like(source_prefix) + "#%"
        with closing(self.connect()) as connection:
            with connection:
                rows = connection.execute(
                    """
                    SELECT id
                    FROM chunks
                    WHERE (source_path = ? OR source_path LIKE ? ESCAPE '!')
                      AND active = 1
                    ORDER BY id
                    """,
                    (source_prefix, source_pattern),
                ).fetchall()
                deactivated_ids = [int(row["id"]) for row in rows]
                connection.execute(
                    """
                    UPDATE chunks
                    SET active = 0,
                        vector_status = 'inactive',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE (source_path = ? OR source_path LIKE ? ESCAPE '!')
                      AND active = 1
                    """,
                    (source_prefix, source_pattern),
                )
        return deactivated_ids

    def list_active_chunk_ids_for_source_prefix(self, source_prefix: str) -> list[int]:
        source_pattern = _escape_like(source_prefix) + "#%"
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT id
                FROM chunks
                WHERE (source_path = ? OR source_path LIKE ? ESCAPE '!')
                  AND active = 1
                ORDER BY id
                """,
                (source_prefix, source_pattern),
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def activate_chunks_for_source_prefix(
        self, source_prefix: str, chunk_ids: list[int]
    ) -> list[int]:
        source_pattern = _escape_like(source_prefix) + "#%"
        chunk_id_set = set(chunk_ids)
        with closing(self.connect()) as connection:
            with connection:
                rows = connection.execute(
                    """
                    SELECT id
                    FROM chunks
                    WHERE (source_path = ? OR source_path LIKE ? ESCAPE '!')
                      AND active = 1
                    ORDER BY id
                    """,
                    (source_prefix, source_pattern),
                ).fetchall()
                deactivated_ids = [
                    int(row["id"]) for row in rows if int(row["id"]) not in chunk_id_set
                ]
                if deactivated_ids:
                    connection.execute(
                        f"""
                        UPDATE chunks
                        SET active = 0,
                            vector_status = 'inactive',
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id IN ({_placeholders(deactivated_ids)})
                        """,
                        deactivated_ids,
                    )
                if chunk_ids:
                    connection.execute(
                        f"""
                        UPDATE chunks
                        SET active = 1,
                            vector_status = 'indexed',
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id IN ({_placeholders(chunk_ids)})
                        """,
                        chunk_ids,
                    )
        return deactivated_ids

    def purge_inactive_chunks(self, chunk_ids: list[int]) -> int:
        unique_ids = sorted(set(chunk_ids))
        if not unique_ids:
            return 0

        with closing(self.connect()) as connection:
            with connection:
                cursor = connection.execute(
                    f"""
                    DELETE FROM chunks
                    WHERE active = 0
                      AND id IN ({_placeholders(unique_ids)})
                    """,
                    unique_ids,
                )
        return int(cursor.rowcount)

    def upsert_source_file(
        self,
        *,
        path: str,
        source_kind: str,
        fingerprint: str,
        size_bytes: int,
        mtime_ns: int,
        status: str,
        error: str | None,
    ) -> None:
        with closing(self.connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO source_files (
                      path,
                      source_kind,
                      fingerprint,
                      size_bytes,
                      mtime_ns,
                      status,
                      error,
                      last_indexed_at
                    )
                    VALUES (
                      ?, ?, ?, ?, ?, ?, ?,
                      CASE WHEN ? = 'indexed' THEN CURRENT_TIMESTAMP ELSE NULL END
                    )
                    ON CONFLICT(path) DO UPDATE SET
                      source_kind = excluded.source_kind,
                      fingerprint = excluded.fingerprint,
                      size_bytes = excluded.size_bytes,
                      mtime_ns = excluded.mtime_ns,
                      status = excluded.status,
                      error = excluded.error,
                      last_indexed_at = CASE
                        WHEN excluded.status = 'indexed' THEN CURRENT_TIMESTAMP
                        ELSE source_files.last_indexed_at
                      END,
                      updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        path,
                        source_kind,
                        fingerprint,
                        size_bytes,
                        mtime_ns,
                        status,
                        error,
                        status,
                    ),
                )

    def get_source_file(self, path: str) -> sqlite3.Row | None:
        with closing(self.connect()) as connection:
            return connection.execute(
                """
                SELECT
                  path,
                  source_kind,
                  fingerprint,
                  size_bytes,
                  mtime_ns,
                  status,
                  error,
                  last_indexed_at,
                  updated_at
                FROM source_files
                WHERE path = ?
                """,
                (path,),
            ).fetchone()

    def list_source_files_under_root(self, root_path: str) -> list[sqlite3.Row]:
        root = root_path.rstrip("\\/")
        backslash_pattern = _escape_like(root) + "\\%"
        slash_pattern = _escape_like(root) + "/%"
        with closing(self.connect()) as connection:
            return connection.execute(
                """
                SELECT
                  path,
                  source_kind,
                  fingerprint,
                  size_bytes,
                  mtime_ns,
                  status,
                  error,
                  last_indexed_at,
                  updated_at
                FROM source_files
                WHERE path = ?
                   OR path LIKE ? ESCAPE '!'
                   OR path LIKE ? ESCAPE '!'
                ORDER BY path
                """,
                (root_path, backslash_pattern, slash_pattern),
            ).fetchall()

    def update_source_file_status(
        self,
        path: str,
        *,
        status: str,
        error: str | None,
    ) -> None:
        with closing(self.connect()) as connection:
            with connection:
                connection.execute(
                    """
                    UPDATE source_files
                    SET status = ?,
                        error = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE path = ?
                    """,
                    (status, error, path),
                )

    def create_index_job(self, root_path: str) -> int:
        with closing(self.connect()) as connection:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT INTO index_jobs (root_path, status, message)
                    VALUES (?, 'running', 'starting')
                    """,
                    (root_path,),
                )
        return int(cursor.lastrowid)

    def update_index_job(
        self,
        job_id: int,
        *,
        status: str,
        discovered_count: int,
        indexed_count: int,
        skipped_count: int,
        error_count: int,
        message: str,
    ) -> None:
        finished_sql = (
            "CURRENT_TIMESTAMP" if status in {"completed", "paused", "error"} else "NULL"
        )
        with closing(self.connect()) as connection:
            with connection:
                connection.execute(
                    f"""
                    UPDATE index_jobs
                    SET status = ?,
                        discovered_count = ?,
                        indexed_count = ?,
                        skipped_count = ?,
                        error_count = ?,
                        message = ?,
                        updated_at = CURRENT_TIMESTAMP,
                        finished_at = {finished_sql}
                    WHERE id = ?
                    """,
                    (
                        status,
                        discovered_count,
                        indexed_count,
                        skipped_count,
                        error_count,
                        message,
                        job_id,
                    ),
                )

    def get_index_job(self, job_id: int) -> sqlite3.Row | None:
        with closing(self.connect()) as connection:
            return connection.execute(
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
                WHERE id = ?
                """,
                (job_id,),
            ).fetchone()

    def save_search(self, *, name: str, query: str, filters: dict[str, Any]) -> int:
        filters_json = json.dumps(filters, ensure_ascii=False, sort_keys=True)
        with closing(self.connect()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO saved_searches (name, query, filters_json)
                    VALUES (?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                      query = excluded.query,
                      filters_json = excluded.filters_json,
                      updated_at = CURRENT_TIMESTAMP
                    """,
                    (name, query, filters_json),
                )
                row = connection.execute(
                    """
                    SELECT id
                    FROM saved_searches
                    WHERE name = ?
                    """,
                    (name,),
                ).fetchone()
        if row is None:
            raise KeyError(f"Saved search was not persisted: {name}")
        return int(row["id"])

    def list_saved_searches(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, name, query, filters_json
                FROM saved_searches
                ORDER BY updated_at DESC, id DESC
                """
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "name": str(row["name"]),
                "query": str(row["query"]),
                "filters": self._metadata_from_json(str(row["filters_json"])),
            }
            for row in rows
        ]

    def get_saved_search(self, saved_id: int) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT id, name, query, filters_json
                FROM saved_searches
                WHERE id = ?
                """,
                (saved_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "name": str(row["name"]),
            "query": str(row["query"]),
            "filters": self._metadata_from_json(str(row["filters_json"])),
        }

    def search_fts(self, query: str, limit: int = 20) -> list[SearchResult]:
        with closing(self.connect()) as connection:
            try:
                rows = connection.execute(
                    """
                    SELECT
                      c.id,
                      c.source_path,
                      c.source_kind,
                      c.title,
                      c.metadata_json,
                      snippet(chunks_fts, -1, '', '', '...', 8) AS snippet,
                      bm25(chunks_fts) AS score
                    FROM chunks_fts
                    JOIN chunks AS c ON c.id = chunks_fts.rowid
                    WHERE chunks_fts MATCH ?
                      AND c.active = 1
                    ORDER BY score
                    LIMIT ?
                    """,
                    (query, limit),
                ).fetchall()
            except sqlite3.OperationalError as error:
                if self._is_fts_query_error(error):
                    return []
                raise

        return [self._row_to_search_result(row) for row in rows]

    def get_chunk(self, chunk_id: int) -> Chunk:
        with closing(self.connect()) as connection:
            existing = connection.execute(
                """
                SELECT
                  id,
                  source_path,
                  source_kind,
                  title,
                  text,
                  metadata_json,
                  ordinal,
                  content_hash
                FROM chunks
                WHERE id = ?
                  AND active = 1
                """,
                (chunk_id,),
            ).fetchone()

        if existing is None:
            raise KeyError(f"No active chunk found for id {chunk_id}")

        return Chunk(
            chunk_id=int(existing["id"]),
            source_path=str(existing["source_path"]),
            source_kind=SourceKind(str(existing["source_kind"])),
            title=str(existing["title"]),
            text=str(existing["text"]),
            metadata=self._metadata_from_json(str(existing["metadata_json"])),
            ordinal=int(existing["ordinal"]),
            content_hash=str(existing["content_hash"]),
        )

    def get_chunk_text(self, chunk_id: int) -> str:
        return self.get_chunk(chunk_id).text

    def get_surrounding_chunks(self, chunk_id: int, radius: int = 1) -> list[Chunk]:
        radius = max(0, min(radius, 10))
        target = self.get_chunk(chunk_id)
        source_prefix = target.source_path.split("#", 1)[0]
        source_pattern = _escape_like(source_prefix) + "#%"
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT
                  id,
                  source_path,
                  source_kind,
                  title,
                  text,
                  metadata_json,
                  ordinal,
                  content_hash
                FROM chunks
                WHERE (source_path = ? OR source_path LIKE ? ESCAPE '!')
                  AND active = 1
                ORDER BY ordinal, source_path, id
                """,
                (source_prefix, source_pattern),
            ).fetchall()

        chunk_rows = list(rows)
        target_index = next(
            (index for index, row in enumerate(chunk_rows) if int(row["id"]) == chunk_id),
            None,
        )
        if target_index is None:
            return []
        start = max(0, target_index - radius)
        end = min(len(chunk_rows), target_index + radius + 1)
        return [
            self._row_to_chunk(row)
            for index, row in enumerate(chunk_rows[start:end], start=start)
            if index != target_index
        ]

    def _row_to_search_result(self, row: sqlite3.Row) -> SearchResult:
        return SearchResult(
            chunk_id=int(row["id"]),
            source_path=str(row["source_path"]),
            source_kind=SourceKind(str(row["source_kind"])),
            title=str(row["title"]),
            snippet=str(row["snippet"]),
            score=float(row["score"]),
            rank_source="fts",
            metadata=self._metadata_from_json(str(row["metadata_json"])),
        )

    def _row_to_chunk(self, row: sqlite3.Row) -> Chunk:
        return Chunk(
            chunk_id=int(row["id"]),
            source_path=str(row["source_path"]),
            source_kind=SourceKind(str(row["source_kind"])),
            title=str(row["title"]),
            text=str(row["text"]),
            metadata=self._metadata_from_json(str(row["metadata_json"])),
            ordinal=int(row["ordinal"]),
            content_hash=str(row["content_hash"]),
        )

    def _is_fts_query_error(self, error: sqlite3.OperationalError) -> bool:
        message = str(error).lower()
        return any(
            fragment in message
            for fragment in (
                "fts5: syntax error",
                "malformed match expression",
                "no such column:",
                "unterminated string",
            )
        )

    def _metadata_from_json(self, metadata_json: str) -> dict[str, str]:
        metadata: Any = json.loads(metadata_json)
        if not isinstance(metadata, dict):
            return {}
        return {str(key): str(value) for key, value in metadata.items()}


def _escape_like(value: str) -> str:
    return value.replace("!", "!!").replace("%", "!%").replace("_", "!_")


def _placeholders(values: list[int]) -> str:
    return ", ".join("?" for _value in values)
