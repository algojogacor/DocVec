from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol

from docvec.config import StorageBudgetExceeded, is_skip_dir_name, max_file_bytes_for_path
from docvec.fingerprints import FileFingerprint, fingerprint_file
from docvec.indexer import VectorIndexingError
from docvec.models import Classification, SourceKind
from docvec.paths import classify_path, is_c_drive_allowed_scope_or_ancestor
from docvec.storage.db import DocVecDB

logger = logging.getLogger(__name__)


DEFAULT_SAVE_EVERY = 100
DEFAULT_EXTRACT_WORKERS = 4
DEFAULT_INDEX_BATCH_SIZE = 32
DEFAULT_MAX_IN_FLIGHT = 128


class IndexerLike(Protocol):
    def index_path(self, path: Path) -> int:
        ...


@dataclass(frozen=True)
class CrawlSummary:
    status: str
    discovered_count: int
    indexed_count: int
    skipped_count: int
    error_count: int
    job_id: int | None


class CrawlController:
    def __init__(self) -> None:
        self._pause_requested = False

    def request_pause(self) -> None:
        self._pause_requested = True

    def resume(self) -> None:
        self._pause_requested = False

    def should_pause(self) -> bool:
        return self._pause_requested


class DocVecCrawler:
    def __init__(
        self,
        db: DocVecDB,
        indexer: IndexerLike,
        include_archives: bool = False,
        save_every: int = DEFAULT_SAVE_EVERY,
        extract_workers: int = DEFAULT_EXTRACT_WORKERS,
        index_batch_size: int = DEFAULT_INDEX_BATCH_SIZE,
        max_in_flight: int = DEFAULT_MAX_IN_FLIGHT,
    ) -> None:
        self.db = db
        self.indexer = indexer
        self.include_archives = include_archives
        self.save_every = max(1, save_every)
        self.extract_workers = max(1, extract_workers)
        self.index_batch_size = max(1, index_batch_size)
        self.max_in_flight = max(1, max_in_flight)
        self._pending_flush_sources: list[_IndexedSource] = []
        self._crawl_lock = threading.Lock()

    def discover(self, roots: list[Path]) -> Iterator[Path]:
        for root in roots:
            yield from self._discover_root(root)

    def crawl(
        self,
        roots: list[Path],
        controller: CrawlController | None = None,
    ) -> CrawlSummary:
        with self._crawl_lock:
            self._pending_flush_sources.clear()
            logger.info(
                "Crawl start roots=%s pipeline=%s",
                [str(root) for root in roots],
                controller is None and self._can_use_pipeline(),
            )
            if controller is None and self._can_use_pipeline():
                return self._crawl_pipelined(roots)
            return self._crawl_sequential(roots, controller=controller)

    def _crawl_sequential(
        self,
        roots: list[Path],
        controller: CrawlController | None = None,
    ) -> CrawlSummary:
        self.db.initialize()
        job_id = self.db.create_index_job(";".join(str(root) for root in roots))
        counts = _CrawlCounts()
        seen_paths: set[str] = set()
        previous_defer = self._set_deferred_vector_save(True)

        try:
            for root in roots:
                for path in self._discover_root(root):
                    if controller is not None and controller.should_pause():
                        flush_error = self._flush_indexer(counts)
                        if flush_error is not None:
                            return self._finish_job(
                                job_id,
                                counts,
                                status="error",
                                message=flush_error,
                            )
                        return self._finish_job(
                            job_id,
                            counts,
                            status="paused",
                            message="paused",
                        )
                    counts.discovered_count += 1
                    seen_paths.add(str(path))
                    try:
                        indexed_source = self._crawl_file(path, root, counts)
                    except StorageBudgetExceeded as error:
                        logger.exception("Storage budget exceeded while crawling path=%s", path)
                        return self._finish_job(
                            job_id,
                            counts,
                            status="error",
                            message=f"storage budget exceeded: {error}",
                        )
                    if indexed_source is not None:
                        self._pending_flush_sources.append(indexed_source)
                    flush_error = self._maybe_flush_indexer(counts)
                    if flush_error is not None:
                        return self._finish_job(
                            job_id,
                            counts,
                            status="error",
                            message=flush_error,
                        )
                    self._update_job(job_id, counts, status="running", message=str(path))
                self._mark_deleted_sources(root, seen_paths)

            flush_error = self._flush_indexer(counts)
            if flush_error is not None:
                return self._finish_job(job_id, counts, status="error", message=flush_error)

            return self._finish_job(job_id, counts, status="completed", message="completed")
        finally:
            self._restore_deferred_vector_save(previous_defer)

    def _crawl_pipelined(self, roots: list[Path]) -> CrawlSummary:
        self.db.initialize()
        job_id = self.db.create_index_job(";".join(str(root) for root in roots))
        counts = _CrawlCounts()
        seen_paths: set[str] = set()
        previous_defer = self._set_deferred_vector_save(True)

        try:
            for root in roots:
                prepared_batch: list[_PreparedCandidate] = []
                pending: dict[Future, _IndexCandidate] = {}
                with ThreadPoolExecutor(max_workers=self.extract_workers) as executor:
                    for path in self._discover_root(root):
                        counts.discovered_count += 1
                        seen_paths.add(str(path))
                        candidate = self._index_candidate(path, root, counts)
                        if candidate is None:
                            self._update_job(
                                job_id,
                                counts,
                                status="running",
                                message=str(path),
                            )
                            continue

                        pending[executor.submit(self._prepare_candidate, candidate)] = candidate
                        while len(pending) >= self.max_in_flight:
                            flush_error = self._drain_ready_prepared(
                                pending,
                                prepared_batch,
                                counts,
                                job_id,
                                wait_for_one=True,
                            )
                            if flush_error is not None:
                                return self._finish_job(
                                    job_id,
                                    counts,
                                    status="error",
                                    message=flush_error,
                                )

                    while pending:
                        flush_error = self._drain_ready_prepared(
                            pending,
                            prepared_batch,
                            counts,
                            job_id,
                            wait_for_one=True,
                        )
                        if flush_error is not None:
                            return self._finish_job(
                                job_id,
                                counts,
                                status="error",
                                message=flush_error,
                            )

                flush_error = self._flush_prepared_batch(prepared_batch, counts, job_id)
                if flush_error is not None:
                    return self._finish_job(job_id, counts, status="error", message=flush_error)
                self._mark_deleted_sources(root, seen_paths)

            flush_error = self._flush_indexer(counts)
            if flush_error is not None:
                return self._finish_job(job_id, counts, status="error", message=flush_error)

            return self._finish_job(job_id, counts, status="completed", message="completed")
        finally:
            self._restore_deferred_vector_save(previous_defer)

    def _discover_root(self, root: Path) -> Iterator[Path]:
        if not root.exists():
            return

        if root.is_file():
            classification = self._classify_for_root(root, root.parent)
            if not self._should_skip_file(root, classification):
                yield root
            return

        for path in self._iter_files(root):
            classification = self._classify_for_root(path, root)
            if not self._should_skip_file(path, classification):
                yield path

    def _iter_files(self, directory: Path) -> Iterator[Path]:
        try:
            # DirEntry caches type checks, avoiding repeated pathlib stat calls per child.
            with os.scandir(directory) as entries:
                children = sorted(entries, key=lambda item: item.name.lower())
        except OSError:
            logger.exception("Unable to enumerate directory path=%s", directory)
            return

        for entry in children:
            child = Path(entry.path)
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if self._should_skip_directory(child, directory):
                        continue
                    yield from self._iter_files(child)
                elif entry.is_file(follow_symlinks=False):
                    yield child
            except OSError:
                logger.exception("Unable to inspect directory entry path=%s", child)
                continue

    def _fingerprint_or_record_error(
        self,
        path: Path,
        classification: Classification,
        counts: "_CrawlCounts",
    ) -> FileFingerprint | None:
        try:
            return fingerprint_file(path)
        except OSError:
            logger.exception("Unable to fingerprint file path=%s", path)
            self._record_source_file(
                path=path,
                classification=classification,
                fingerprint=FileFingerprint(
                    path=str(path),
                    size_bytes=0,
                    mtime_ns=0,
                    fingerprint="fingerprint_unavailable",
                ),
                status="error",
                error="fingerprint unavailable",
            )
            counts.error_count += 1
            return None

    def _crawl_file(
        self,
        path: Path,
        root: Path,
        counts: _CrawlCounts,
    ) -> "_IndexedSource | None":
        classification = self._classify_for_root(path, root)
        file_fingerprint = self._fingerprint_or_record_error(path, classification, counts)
        if file_fingerprint is None:
            return None

        stored = self.db.get_source_file(str(path))
        if (
            stored is not None
            and stored["fingerprint"] == file_fingerprint.fingerprint
            and stored["status"] == "indexed"
            and self._has_index_artifacts(path)
        ):
            counts.skipped_count += 1
            return None

        try:
            logger.debug("Indexing file path=%s", path)
            indexed_chunks = self.indexer.index_path(path)
        except StorageBudgetExceeded:
            raise
        except VectorIndexingError as error:
            logger.exception("Vector indexing failed for file path=%s", path)
            self._record_source_file(
                path=path,
                classification=classification,
                fingerprint=file_fingerprint,
                status="vector_pending",
                error=str(error),
            )
            counts.error_count += 1
            return None
        except Exception as error:
            logger.exception("Indexing failed for file path=%s", path)
            self._record_source_file(
                path=path,
                classification=classification,
                fingerprint=file_fingerprint,
                status="error",
                error=str(error),
            )
            counts.error_count += 1
            return None

        if indexed_chunks <= 0:
            self._record_source_file(
                path=path,
                classification=classification,
                fingerprint=file_fingerprint,
                status="skipped",
                error=None,
            )
            counts.skipped_count += 1
            return None

        self._record_source_file(
            path=path,
            classification=classification,
            fingerprint=file_fingerprint,
            status="indexed",
            error=None,
        )
        counts.indexed_count += 1
        return _IndexedSource(path, classification, file_fingerprint)

    def _has_index_artifacts(self, path: Path) -> bool:
        checker = getattr(self.indexer, "has_vectors_for_source", None)
        if not callable(checker):
            return True
        return bool(checker(path))

    def _mark_deleted_sources(self, root: Path, seen_paths: set[str]) -> None:
        for row in self.db.list_source_files_under_root(str(root)):
            source_path = str(row["path"])
            if source_path in seen_paths or Path(source_path).exists():
                continue
            deleter = getattr(self.indexer, "delete_source", None)
            if callable(deleter):
                deleter(Path(source_path))
            else:
                self.db.deactivate_chunks_for_source_prefix(source_path)
            self.db.update_source_file_status(source_path, status="deleted", error=None)

    def _record_source_file(
        self,
        *,
        path: Path,
        classification: Classification,
        fingerprint: FileFingerprint,
        status: str,
        error: str | None,
    ) -> None:
        self.db.upsert_source_file(
            path=str(path),
            source_kind=classification.kind.value,
            fingerprint=fingerprint.fingerprint,
            size_bytes=fingerprint.size_bytes,
            mtime_ns=fingerprint.mtime_ns,
            status=status,
            error=error,
        )

    def _index_candidate(
        self,
        path: Path,
        root: Path,
        counts: _CrawlCounts,
    ) -> "_IndexCandidate | None":
        classification = self._classify_for_root(path, root)
        file_fingerprint = self._fingerprint_or_record_error(path, classification, counts)
        if file_fingerprint is None:
            return None
        stored = self.db.get_source_file(str(path))
        if (
            stored is not None
            and stored["fingerprint"] == file_fingerprint.fingerprint
            and stored["status"] == "indexed"
            and self._has_index_artifacts(path)
        ):
            counts.skipped_count += 1
            return None
        return _IndexCandidate(path, root, classification, file_fingerprint)

    def _prepare_candidate(self, candidate: "_IndexCandidate") -> "_PreparedCandidate":
        preparer = getattr(self.indexer, "prepare_path")
        return _PreparedCandidate(candidate, preparer(candidate.path))

    def _drain_ready_prepared(
        self,
        pending: dict[Future, "_IndexCandidate"],
        prepared_batch: list["_PreparedCandidate"],
        counts: _CrawlCounts,
        job_id: int,
        *,
        wait_for_one: bool,
    ) -> str | None:
        if not pending:
            return None

        if wait_for_one:
            done, _not_done = wait(set(pending.keys()), return_when=FIRST_COMPLETED)
        else:
            done = {future for future in pending if future.done()}
        if not done:
            return None

        for future in done:
            candidate = pending.pop(future)
            try:
                prepared_batch.append(future.result())
            except Exception as error:
                logger.exception("Preparing file failed path=%s", candidate.path)
                self._record_source_file(
                    path=candidate.path,
                    classification=candidate.classification,
                    fingerprint=candidate.fingerprint,
                    status="error",
                    error=str(error),
                )
                counts.error_count += 1
                self._update_job(
                    job_id,
                    counts,
                    status="running",
                    message=str(candidate.path),
                )
                continue

            if len(prepared_batch) >= self.index_batch_size:
                flush_error = self._flush_prepared_batch(prepared_batch, counts, job_id)
                if flush_error is not None:
                    return flush_error
        return None

    def _flush_prepared_batch(
        self,
        prepared_batch: list["_PreparedCandidate"],
        counts: _CrawlCounts,
        job_id: int,
    ) -> str | None:
        if not prepared_batch:
            return None

        batch = list(prepared_batch)
        prepared_batch.clear()
        batch_indexer = getattr(self.indexer, "index_prepared_batch")
        try:
            results = batch_indexer([item.prepared for item in batch])
        except StorageBudgetExceeded as error:
            logger.exception("Storage budget exceeded for prepared batch size=%s", len(batch))
            return f"storage budget exceeded: {error}"
        except VectorIndexingError as error:
            logger.exception("Vector indexing failed for prepared batch size=%s", len(batch))
            for item in batch:
                self._record_source_file(
                    path=item.candidate.path,
                    classification=item.candidate.classification,
                    fingerprint=item.candidate.fingerprint,
                    status="vector_pending",
                    error=str(error),
                )
                counts.error_count += 1
            return f"vector indexing failed: {error}"
        except Exception as error:
            logger.exception("Indexing failed for prepared batch size=%s", len(batch))
            for item in batch:
                self._record_source_file(
                    path=item.candidate.path,
                    classification=item.candidate.classification,
                    fingerprint=item.candidate.fingerprint,
                    status="error",
                    error=str(error),
                )
                counts.error_count += 1
            return f"indexing failed: {error}"

        for item in batch:
            indexed_chunks = int(results.get(str(item.candidate.path), 0))
            if indexed_chunks <= 0:
                self._record_source_file(
                    path=item.candidate.path,
                    classification=item.candidate.classification,
                    fingerprint=item.candidate.fingerprint,
                    status="skipped",
                    error=None,
                )
                counts.skipped_count += 1
            else:
                self._record_source_file(
                    path=item.candidate.path,
                    classification=item.candidate.classification,
                    fingerprint=item.candidate.fingerprint,
                    status="indexed",
                    error=None,
                )
                counts.indexed_count += 1
                self._pending_flush_sources.append(
                    _IndexedSource(
                        item.candidate.path,
                        item.candidate.classification,
                        item.candidate.fingerprint,
                    )
                )
            self._update_job(
                job_id,
                counts,
                status="running",
                message=str(item.candidate.path),
            )

        return self._maybe_flush_indexer(counts)

    def _can_use_pipeline(self) -> bool:
        return (
            self.extract_workers > 1
            and callable(getattr(self.indexer, "prepare_path", None))
            and callable(getattr(self.indexer, "index_prepared_batch", None))
        )

    def _maybe_flush_indexer(self, counts: _CrawlCounts) -> str | None:
        if (
            counts.indexed_count > 0
            and self._pending_flush_sources
            and counts.indexed_count % self.save_every == 0
        ):
            return self._flush_indexer(counts)
        return None

    def _flush_indexer(self, counts: _CrawlCounts) -> str | None:
        flusher = getattr(self.indexer, "flush", None)
        if not callable(flusher):
            self._pending_flush_sources.clear()
            return None
        try:
            flusher()
        except Exception as error:
            logger.exception("Vector flush failed")
            message = str(error)
            pending = list(self._pending_flush_sources)
            self._pending_flush_sources.clear()
            for source in pending:
                self.db.mark_chunks_vector_pending_for_source_prefix(str(source.path))
                self._record_source_file(
                    path=source.path,
                    classification=source.classification,
                    fingerprint=source.fingerprint,
                    status="vector_pending",
                    error=message,
                )
                counts.error_count += 1
            return f"vector flush failed: {message}"
        flushed_sources = len(self._pending_flush_sources)
        self._pending_flush_sources.clear()
        logger.info(
            "Flush completed indexed_count=%s pending_sources=%s",
            counts.indexed_count,
            flushed_sources,
        )
        return None

    def _set_deferred_vector_save(self, enabled: bool) -> bool | None:
        if not hasattr(self.indexer, "defer_vector_save"):
            return None
        previous = bool(getattr(self.indexer, "defer_vector_save"))
        setattr(self.indexer, "defer_vector_save", enabled)
        return previous

    def _restore_deferred_vector_save(self, previous: bool | None) -> None:
        if previous is None:
            return
        setattr(self.indexer, "defer_vector_save", previous)

    def _classify_for_root(self, path: Path, root: Path) -> Classification:
        classification = classify_path(path)
        if classification.reason == "c_drive_unscoped" and not _is_c_drive_scan_root(root):
            return Classification(path, SourceKind.NORMAL_FILE, False, "normal_file")
        if classification.reason not in {"skip_dir", "appdata", "game_folder"}:
            return classification
        if self._relative_has_ignored_dir(path, root):
            return classification
        return Classification(path, SourceKind.NORMAL_FILE, False, "normal_file")

    def _should_skip_file(self, path: Path, classification: Classification) -> bool:
        if self._is_included_archive(classification, path):
            return False
        if classification.should_skip:
            return True
        try:
            size_bytes = path.stat().st_size
        except OSError:
            logger.exception("Unable to stat file for skip check path=%s", path)
            return True
        if classification.kind == SourceKind.AI_SESSION:
            return False
        max_file_bytes = max_file_bytes_for_path(path)
        if size_bytes > max_file_bytes:
            logger.warning(
                "Skipping file due to size path=%s size_bytes=%s max_file_bytes=%s",
                path,
                size_bytes,
                max_file_bytes,
            )
            return True
        return False

    def _is_included_archive(self, classification: Classification, path: Path) -> bool:
        return (
            self.include_archives
            and classification.reason == "archive_extension"
            and path.suffix.lower() == ".zip"
        )

    def _should_skip_directory(self, path: Path, root: Path) -> bool:
        classification = classify_path(path)
        if classification.reason == "c_drive_unscoped":
            if not _is_c_drive_scan_root(root):
                return False
            return not is_c_drive_allowed_scope_or_ancestor(path)
        if classification.should_skip and classification.reason in _NON_RELAXABLE_DIRECTORY_SKIP_REASONS:
            return True
        return self._relative_has_ignored_dir(path, root)

    def _relative_has_skip_dir(self, path: Path, root: Path) -> bool:
        return any(is_skip_dir_name(part) for part in self._relative_parts(path, root))

    def _relative_has_ignored_dir(self, path: Path, root: Path) -> bool:
        parts = self._relative_parts(path, root)
        return (
            _relative_has_docvec_runtime_data(parts)
            or any(is_skip_dir_name(part) for part in parts)
            or "appdata" in parts
            or any(part in _GAME_DIR_NAMES for part in parts)
        )

    def _relative_parts(self, path: Path, root: Path) -> list[str]:
        try:
            relative = path.relative_to(root)
        except ValueError:
            relative = path
        return [part.lower() for part in relative.parts]

    def _update_job(
        self,
        job_id: int,
        counts: _CrawlCounts,
        *,
        status: str,
        message: str,
    ) -> None:
        self.db.update_index_job(
            job_id,
            status=status,
            discovered_count=counts.discovered_count,
            indexed_count=counts.indexed_count,
            skipped_count=counts.skipped_count,
            error_count=counts.error_count,
            message=message,
        )

    def _finish_job(
        self,
        job_id: int,
        counts: _CrawlCounts,
        *,
        status: str,
        message: str,
    ) -> CrawlSummary:
        self._update_job(job_id, counts, status=status, message=message)
        logger.info(
            "Crawl end status=%s discovered=%s indexed=%s skipped=%s errors=%s job_id=%s",
            status,
            counts.discovered_count,
            counts.indexed_count,
            counts.skipped_count,
            counts.error_count,
            job_id,
        )
        return CrawlSummary(
            status=status,
            discovered_count=counts.discovered_count,
            indexed_count=counts.indexed_count,
            skipped_count=counts.skipped_count,
            error_count=counts.error_count,
            job_id=job_id,
        )


@dataclass
class _CrawlCounts:
    discovered_count: int = 0
    indexed_count: int = 0
    skipped_count: int = 0
    error_count: int = 0


@dataclass(frozen=True)
class _IndexedSource:
    path: Path
    classification: Classification
    fingerprint: FileFingerprint


@dataclass(frozen=True)
class _IndexCandidate:
    path: Path
    root: Path
    classification: Classification
    fingerprint: FileFingerprint


@dataclass(frozen=True)
class _PreparedCandidate:
    candidate: _IndexCandidate
    prepared: object


_GAME_DIR_NAMES = {
    "steam",
    "steamlibrary",
    "steamapps",
    "epic games",
    "xboxgames",
    "gog galaxy",
    "battle.net",
    "riot games",
    "ubisoft",
    "ea games",
    "electronic arts",
}


_NON_RELAXABLE_DIRECTORY_SKIP_REASONS = {
    "antigravity_backup",
    "antigravity_browser_profile",
    "antigravity_extensions",
    "docvec_runtime_data",
    "gemini_extensions",
    "gemini_skills",
}


def _relative_has_docvec_runtime_data(parts: list[str]) -> bool:
    return any(
        tuple(parts[index : index + 2]) == ("docvec", "data")
        for index in range(len(parts) - 1)
    )


def _is_c_drive_scan_root(root: Path) -> bool:
    return str(root).replace("/", "\\").lower().rstrip("\\") == "c:"
