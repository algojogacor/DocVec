from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from docvec.chunking import chunk_record
from docvec.config import DATA_DIR, STORAGE_HARD_STOP_BYTES, ensure_storage_budget
from docvec.embeddings import Embedder
from docvec.extractors.codex import extract_codex_jsonl
from docvec.extractors.documents import (
    extract_antigravity_pb,
    extract_csv_file,
    extract_docx_file,
    extract_html_file,
    extract_mhtml_file,
    extract_pdf_file,
    extract_pptx_file,
    extract_transcript_file,
    extract_xlsx_file,
    extract_zip_archive,
)
from docvec.extractors.hermes import extract_hermes_memory_store_db, extract_hermes_state_db
from docvec.extractors.text import extract_text_file
from docvec.models import Chunk, ExtractedRecord
from docvec.paths import classify_path
from docvec.storage.db import DocVecDB
from docvec.vectors import VectorBackend


class VectorIndexingError(RuntimeError):
    """Raised when chunk text is stored but vector indexing could not complete."""


@dataclass(frozen=True)
class PreparedIndex:
    path: Path
    chunks: list[Chunk]


class DocVecIndexer:
    def __init__(
        self,
        db: DocVecDB,
        embedder: Embedder,
        vectors: VectorBackend,
        data_dir: Path = DATA_DIR,
        storage_hard_stop_bytes: int = STORAGE_HARD_STOP_BYTES,
        ignore_skip_dirs: bool = False,
        include_archives: bool = False,
    ) -> None:
        self.db = db
        self.embedder = embedder
        self.vectors = vectors
        self.data_dir = data_dir
        self.storage_hard_stop_bytes = storage_hard_stop_bytes
        self.ignore_skip_dirs = ignore_skip_dirs
        self.include_archives = include_archives
        self.defer_vector_save = False
        self._vectors_dirty = False

    def _extract(self, path: Path) -> list[ExtractedRecord]:
        classification = classify_path(path)
        if classification.should_skip and not (
            self.ignore_skip_dirs and classification.reason in _IGNORABLE_SKIP_REASONS
            or self._is_included_archive(classification, path)
        ):
            return []
        if self._is_included_archive(classification, path):
            return extract_zip_archive(path, classification.kind)
        if classification.reason == "hermes_state_db":
            return extract_hermes_state_db(path)
        if classification.reason == "hermes_memory_store_db":
            return extract_hermes_memory_store_db(path)
        if classification.reason == "antigravity_pb":
            return extract_antigravity_pb(path)
        if classification.reason == "codex_session" and path.suffix.lower() == ".jsonl":
            return extract_codex_jsonl(path)
        if path.suffix.lower() == ".pdf":
            return [extract_pdf_file(path, classification.kind)]
        if path.suffix.lower() == ".docx":
            return [extract_docx_file(path, classification.kind)]
        if path.suffix.lower() == ".pptx":
            return [extract_pptx_file(path, classification.kind)]
        if path.suffix.lower() == ".xlsx":
            return [extract_xlsx_file(path, classification.kind)]
        if path.suffix.lower() == ".csv":
            return [extract_csv_file(path, classification.kind)]
        if path.suffix.lower() in {".html", ".htm"}:
            return [extract_html_file(path, classification.kind)]
        if path.suffix.lower() == ".mhtml":
            return [extract_mhtml_file(path, classification.kind)]
        if path.suffix.lower() in {".srt", ".vtt", ".ass"}:
            return [extract_transcript_file(path, classification.kind)]
        return [extract_text_file(path, classification.kind)]

    def prepare_path(self, path: Path) -> PreparedIndex:
        classification = classify_path(path)
        if classification.should_skip and not (
            self.ignore_skip_dirs and classification.reason in _IGNORABLE_SKIP_REASONS
            or self._is_included_archive(classification, path)
        ):
            return PreparedIndex(path=path, chunks=[])

        records = self._extract(path)
        chunks = []
        for record in records:
            chunks.extend(chunk_record(record, max_words=900, overlap_words=120))
        return PreparedIndex(path=path, chunks=chunks)

    def index_path(self, path: Path) -> int:
        return self.index_prepared(self.prepare_path(path))

    def index_prepared(self, prepared: PreparedIndex) -> int:
        return self.index_prepared_batch([prepared]).get(str(prepared.path), 0)

    def index_prepared_batch(
        self,
        prepared_items: list[PreparedIndex],
    ) -> dict[str, int]:
        ensure_storage_budget(self.data_dir, self.storage_hard_stop_bytes)
        results: dict[str, int] = {}
        activation_plan: list[tuple[PreparedIndex, list[int]]] = []
        missing_ids: list[int] = []
        missing_texts: list[str] = []

        for prepared in prepared_items:
            path_key = str(prepared.path)
            chunks = prepared.chunks
            if not chunks:
                deactivated_ids = self.db.deactivate_chunks_for_source_prefix(path_key)
                if deactivated_ids:
                    self.vectors.remove(deactivated_ids)
                    self._save_vectors_if_needed()
                    self.db.purge_inactive_chunks(deactivated_ids)
                results[path_key] = 0
                continue

            chunk_ids = self.db.stage_chunks(chunks)
            activation_plan.append((prepared, chunk_ids))
            results[path_key] = len(chunks)

            for index, chunk_id in enumerate(chunk_ids):
                if not self.vectors.contains(chunk_id):
                    missing_ids.append(chunk_id)
                    missing_texts.append(chunks[index].text)

        if missing_ids:
            try:
                embeddings = self.embedder.embed(missing_texts)
                self.vectors.add(missing_ids, embeddings)
                self._save_vectors_if_needed()
            except Exception as error:
                try:
                    self.vectors.remove(missing_ids)
                    self._save_vectors_if_needed()
                except Exception:
                    pass
                raise VectorIndexingError(str(error)) from error

        for prepared, chunk_ids in activation_plan:
            deactivated_ids = self.db.activate_chunks_for_source_prefix(
                str(prepared.path),
                chunk_ids,
            )
            if deactivated_ids:
                self.vectors.remove(deactivated_ids)
                self._save_vectors_if_needed()
                self.db.purge_inactive_chunks(deactivated_ids)

        ensure_storage_budget(self.data_dir, self.storage_hard_stop_bytes)
        return results

    def has_vectors_for_source(self, path: Path) -> bool:
        chunk_ids = self.db.list_active_chunk_ids_for_source_prefix(str(path))
        return all(self.vectors.contains(chunk_id) for chunk_id in chunk_ids)

    def delete_source(self, path: Path) -> int:
        deactivated_ids = self.db.deactivate_chunks_for_source_prefix(str(path))
        if deactivated_ids:
            self.vectors.remove(deactivated_ids)
            self.vectors.save()
        return len(deactivated_ids)

    def flush(self) -> None:
        if not self._vectors_dirty:
            return
        self.vectors.save()
        self._vectors_dirty = False

    def _is_included_archive(self, classification, path: Path) -> bool:
        return (
            self.include_archives
            and classification.reason == "archive_extension"
            and path.suffix.lower() == ".zip"
        )

    def _save_vectors_if_needed(self) -> None:
        self._vectors_dirty = True
        if not self.defer_vector_save:
            self.flush()


_IGNORABLE_SKIP_REASONS = {"skip_dir", "appdata", "game_folder"}
