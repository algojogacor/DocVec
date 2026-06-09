from __future__ import annotations

import sqlite3
from pathlib import Path

from docvec.models import ExtractedRecord, SourceKind


def extract_hermes_state_db(path: Path) -> list[ExtractedRecord]:
    db_uri = path.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(db_uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT
              m.id AS message_id,
              m.session_id,
              m.role,
              m.content,
              m.timestamp,
              s.model
            FROM messages m
            LEFT JOIN sessions s ON s.id = m.session_id
            WHERE m.content IS NOT NULL
            ORDER BY m.session_id, m.id
            """
        ).fetchall()
    finally:
        con.close()

    records: list[ExtractedRecord] = []
    for row in rows:
        content = str(row["content"])
        if not content.strip():
            continue

        role = str(row["role"])
        records.append(
            ExtractedRecord(
                source_path=f"{path}#message:{row['message_id']}",
                source_kind=SourceKind.AI_SESSION,
                title=f"Hermes {role} message",
                text=content,
                metadata={
                    "source": "hermes",
                    "session_id": str(row["session_id"]),
                    "message_id": str(row["message_id"]),
                    "role": role,
                    "timestamp": str(row["timestamp"] or ""),
                    "model": str(row["model"] or ""),
                },
            )
        )
    return records


def extract_hermes_memory_store_db(path: Path) -> list[ExtractedRecord]:
    db_uri = path.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(db_uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """
            SELECT
              fact_id,
              content,
              category,
              tags,
              trust_score,
              created_at,
              updated_at
            FROM facts
            WHERE content IS NOT NULL
            ORDER BY fact_id
            """
        ).fetchall()
    finally:
        con.close()

    records: list[ExtractedRecord] = []
    for row in rows:
        content = str(row["content"])
        if not content.strip():
            continue
        records.append(
            ExtractedRecord(
                source_path=f"{path}#fact:{row['fact_id']}",
                source_kind=SourceKind.AI_MEMORY,
                title="Hermes memory fact",
                text=content,
                metadata={
                    "source": "hermes",
                    "fact_id": str(row["fact_id"]),
                    "category": str(row["category"] or ""),
                    "tags": str(row["tags"] or ""),
                    "trust_score": str(row["trust_score"] or ""),
                    "created_at": str(row["created_at"] or ""),
                    "updated_at": str(row["updated_at"] or ""),
                },
            )
        )
    return records
