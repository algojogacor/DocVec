from __future__ import annotations

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
from docvec.models import ExtractedRecord
from docvec.paths import classify_path
from docvec.storage.db import DocVecDB
from docvec.vectors import VectorBackend


class VectorIndexingError(RuntimeError):
    """Raised when chunk text is stored but vector indexing could not complete."""


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

    def index_path(self, path: Path) -> int:
        ensure_storage_budget(self.data_dir, self.storage_hard_stop_bytes)
        classification = classify_path(path)
        if classification.should_skip and not (
            self.ignore_skip_dirs and classification.reason in _IGNORABLE_SKIP_REASONS
            or self._is_included_archive(classification, path)
        ):
            return 0

        records = self._extract(path)

        if not records:
            deactivated_ids = self.db.deactivate_chunks_for_source_prefix(str(path))
            if deactivated_ids:
                self.vectors.remove(deactivated_ids)
                self.vectors.save()
                self.db.purge_inactive_chunks(deactivated_ids)
            ensure_storage_budget(self.data_dir, self.storage_hard_stop_bytes)
            return 0

        chunks = []
        for record in records:
            chunks.extend(chunk_record(record, max_words=900, overlap_words=120))
        if not chunks:
            deactivated_ids = self.db.deactivate_chunks_for_source_prefix(str(path))
            if deactivated_ids:
                self.vectors.remove(deactivated_ids)
                self.vectors.save()
                self.db.purge_inactive_chunks(deactivated_ids)
            ensure_storage_budget(self.data_dir, self.storage_hard_stop_bytes)
            return 0

        chunk_ids = [self.db.stage_chunk(chunk) for chunk in chunks]

        new_items = [
            (index, chunk_id)
            for index, chunk_id in enumerate(chunk_ids)
            if not self.vectors.contains(chunk_id)
        ]
        if new_items:
            new_indexes = [index for index, _chunk_id in new_items]
            new_ids = [chunk_id for _index, chunk_id in new_items]
            new_texts = [chunks[index].text for index in new_indexes]
            embeddings = self.embedder.embed(new_texts)
            try:
                self.vectors.add(new_ids, embeddings)
                self.vectors.save()
            except Exception as error:
                try:
                    self.vectors.remove(new_ids)
                    self.vectors.save()
                except Exception:
                    pass
                raise VectorIndexingError(str(error)) from error

        deactivated_ids = self.db.activate_chunks_for_source_prefix(str(path), chunk_ids)
        if deactivated_ids:
            self.vectors.remove(deactivated_ids)
            self.vectors.save()
            self.db.purge_inactive_chunks(deactivated_ids)
        ensure_storage_budget(self.data_dir, self.storage_hard_stop_bytes)
        return len(chunks)

    def has_vectors_for_source(self, path: Path) -> bool:
        chunk_ids = self.db.list_active_chunk_ids_for_source_prefix(str(path))
        return all(self.vectors.contains(chunk_id) for chunk_id in chunk_ids)

    def delete_source(self, path: Path) -> int:
        deactivated_ids = self.db.deactivate_chunks_for_source_prefix(str(path))
        if deactivated_ids:
            self.vectors.remove(deactivated_ids)
            self.vectors.save()
        return len(deactivated_ids)

    def _is_included_archive(self, classification, path: Path) -> bool:
        return (
            self.include_archives
            and classification.reason == "archive_extension"
            and path.suffix.lower() == ".zip"
        )


_IGNORABLE_SKIP_REASONS = {"skip_dir", "appdata", "game_folder"}
