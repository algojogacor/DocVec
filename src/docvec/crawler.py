from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol

from docvec.config import DEFAULT_MAX_FILE_BYTES, is_skip_dir_name
from docvec.fingerprints import FileFingerprint, fingerprint_file
from docvec.indexer import VectorIndexingError
from docvec.models import Classification, SourceKind
from docvec.paths import classify_path, is_c_drive_allowed_scope_or_ancestor
from docvec.storage.db import DocVecDB


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
    ) -> None:
        self.db = db
        self.indexer = indexer
        self.include_archives = include_archives

    def discover(self, roots: list[Path]) -> Iterator[Path]:
        for root in roots:
            yield from self._discover_root(root)

    def crawl(
        self,
        roots: list[Path],
        controller: CrawlController | None = None,
    ) -> CrawlSummary:
        self.db.initialize()
        job_id = self.db.create_index_job(";".join(str(root) for root in roots))
        counts = _CrawlCounts()
        seen_paths: set[str] = set()

        for root in roots:
            for path in self._discover_root(root):
                if controller is not None and controller.should_pause():
                    return self._finish_job(job_id, counts, status="paused", message="paused")
                counts.discovered_count += 1
                seen_paths.add(str(path))
                self._crawl_file(path, root, counts)
                self._update_job(job_id, counts, status="running", message=str(path))
            self._mark_deleted_sources(root, seen_paths)

        return self._finish_job(job_id, counts, status="completed", message="completed")

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
            children = sorted(directory.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            return

        for child in children:
            if child.is_symlink():
                continue
            if child.is_dir():
                if self._should_skip_directory(child, directory):
                    continue
                yield from self._iter_files(child)
            elif child.is_file():
                yield child

    def _crawl_file(self, path: Path, root: Path, counts: _CrawlCounts) -> None:
        classification = self._classify_for_root(path, root)
        file_fingerprint = fingerprint_file(path)
        stored = self.db.get_source_file(str(path))
        if (
            stored is not None
            and stored["fingerprint"] == file_fingerprint.fingerprint
            and stored["status"] == "indexed"
            and self._has_index_artifacts(path)
        ):
            counts.skipped_count += 1
            return

        try:
            indexed_chunks = self.indexer.index_path(path)
        except VectorIndexingError as error:
            self._record_source_file(
                path=path,
                classification=classification,
                fingerprint=file_fingerprint,
                status="vector_pending",
                error=str(error),
            )
            counts.error_count += 1
            return
        except Exception as error:
            self._record_source_file(
                path=path,
                classification=classification,
                fingerprint=file_fingerprint,
                status="error",
                error=str(error),
            )
            counts.error_count += 1
            return

        if indexed_chunks <= 0:
            self._record_source_file(
                path=path,
                classification=classification,
                fingerprint=file_fingerprint,
                status="skipped",
                error=None,
            )
            counts.skipped_count += 1
            return

        self._record_source_file(
            path=path,
            classification=classification,
            fingerprint=file_fingerprint,
            status="indexed",
            error=None,
        )
        counts.indexed_count += 1

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
            return True
        if classification.kind == SourceKind.AI_SESSION:
            return False
        return size_bytes > DEFAULT_MAX_FILE_BYTES

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
