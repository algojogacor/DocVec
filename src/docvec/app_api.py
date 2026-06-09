from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from docvec.auto_scan import AutoScanConfig, AutoScanScheduler
from docvec.config import STORAGE_HARD_STOP_BYTES, STORAGE_WARNING_BYTES, data_usage_bytes
from docvec.crawler import CrawlController, CrawlSummary
from docvec.models import SearchResult
from docvec.runtime import DocVecRuntime


class DocVecApiServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        runtime: DocVecRuntime,
        auto_scan_config: AutoScanConfig | None = None,
        auto_scan_enabled: bool = False,
    ) -> None:
        super().__init__(server_address, DocVecApiHandler)
        self.runtime = runtime
        self.controller = CrawlController()
        self._scan_lock = threading.Lock()
        self._scan_thread: threading.Thread | None = None
        self._background_summary = CrawlSummary(
            status="idle",
            discovered_count=0,
            indexed_count=0,
            skipped_count=0,
            error_count=0,
            job_id=None,
        )
        self.auto_scan = AutoScanScheduler(
            start_scan=lambda roots: self.start_background_scan(roots),
            is_scan_running=lambda: self.background_summary().status == "running",
            config=auto_scan_config or AutoScanConfig(),
        )
        if auto_scan_enabled:
            self.auto_scan.start()

    def server_close(self) -> None:
        self.auto_scan.stop()
        super().server_close()

    def start_background_scan(
        self,
        roots: list[Path],
        include_archives: bool = False,
    ) -> CrawlSummary:
        with self._scan_lock:
            if self._scan_thread is not None and self._scan_thread.is_alive():
                return self._background_summary

            self.controller.resume()
            self._background_summary = CrawlSummary(
                status="running",
                discovered_count=0,
                indexed_count=0,
                skipped_count=0,
                error_count=0,
                job_id=None,
            )
            self._scan_thread = threading.Thread(
                target=self._run_background_scan,
                args=(roots, include_archives),
                daemon=True,
                name="docvec-scan",
            )
            self._scan_thread.start()
            return self._background_summary

    def _run_background_scan(self, roots: list[Path], include_archives: bool) -> None:
        try:
            summary = self.crawl(roots, include_archives=include_archives)
        except Exception as error:
            job_id = self._record_background_error(str(error))
            summary = CrawlSummary(
                status="error",
                discovered_count=0,
                indexed_count=0,
                skipped_count=0,
                error_count=1,
                job_id=job_id,
            )
        with self._scan_lock:
            self._background_summary = summary

    def crawl(self, roots: list[Path], include_archives: bool = False) -> CrawlSummary:
        indexer_has_archive_mode = hasattr(self.runtime.indexer, "include_archives")
        crawler_has_archive_mode = hasattr(self.runtime.crawler, "include_archives")
        old_indexer_mode = getattr(self.runtime.indexer, "include_archives", False)
        old_crawler_mode = getattr(self.runtime.crawler, "include_archives", False)
        if include_archives:
            if indexer_has_archive_mode:
                self.runtime.indexer.include_archives = True
            if crawler_has_archive_mode:
                self.runtime.crawler.include_archives = True
        try:
            return self.runtime.crawler.crawl(roots, controller=self.controller)
        finally:
            if indexer_has_archive_mode:
                self.runtime.indexer.include_archives = old_indexer_mode
            if crawler_has_archive_mode:
                self.runtime.crawler.include_archives = old_crawler_mode

    def _record_background_error(self, message: str) -> int:
        with self.runtime.db.connect() as connection:
            with connection:
                job_id = connection.execute(
                    """
                    INSERT INTO index_jobs (root_path, status, error_count, message, finished_at)
                    VALUES ('background', 'error', 1, ?, CURRENT_TIMESTAMP)
                    """,
                    (message,),
                ).lastrowid
        return int(job_id)

    def background_summary(self) -> CrawlSummary:
        with self._scan_lock:
            if self._scan_thread is not None and self._scan_thread.is_alive():
                return CrawlSummary(
                    status="running",
                    discovered_count=self._background_summary.discovered_count,
                    indexed_count=self._background_summary.indexed_count,
                    skipped_count=self._background_summary.skipped_count,
                    error_count=self._background_summary.error_count,
                    job_id=self._background_summary.job_id,
                )
            return self._background_summary


class DocVecApiHandler(BaseHTTPRequestHandler):
    server: DocVecApiServer

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT.value)
        self._send_cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/status":
            self._write_json(self._status_payload())
            return
        if parsed.path == "/search":
            params = parse_qs(parsed.query)
            query = params.get("q", [""])[0]
            limit = _parse_limit(params.get("limit", ["10"])[0])
            results = self.server.runtime.search.search(
                query,
                limit=limit,
                filters=_parse_search_filters(params),
            )
            self._write_json(
                {
                    "ok": True,
                    "results": [_search_result_to_dict(result) for result in results],
                }
            )
            return
        if parsed.path == "/context":
            params = parse_qs(parsed.query)
            chunk_id = _parse_chunk_id(params.get("id", [""])[0])
            if chunk_id is None:
                self._write_json(
                    {"ok": False, "error": "invalid chunk id"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            try:
                chunk = self.server.runtime.db.get_chunk(chunk_id)
            except KeyError:
                self._write_json(
                    {"ok": False, "error": "chunk not found"},
                    status=HTTPStatus.NOT_FOUND,
                )
                return
            neighbors = self.server.runtime.db.get_surrounding_chunks(chunk_id, radius=1)
            self._write_json(
                {
                    "ok": True,
                    "chunk": _chunk_to_dict(chunk),
                    "neighbors": [_chunk_to_dict(neighbor) for neighbor in neighbors],
                }
            )
            return
        if parsed.path == "/saved-searches":
            self._write_json(
                {
                    "ok": True,
                    "saved_searches": self.server.runtime.db.list_saved_searches(),
                }
            )
            return
        if parsed.path == "/auto-scan":
            self._write_json({"ok": True, "auto_scan": self.server.auto_scan.status()})
            return
        self._write_json({"ok": False, "error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        payload = self._read_json()
        if parsed.path == "/scan":
            roots = [Path(root) for root in payload.get("roots", [])]
            include_archives = bool(payload.get("include_archives", False))
            if bool(payload.get("background", False)):
                summary = self.server.start_background_scan(
                    roots,
                    include_archives=include_archives,
                )
                self._write_json({"ok": True, "summary": asdict(summary), "background": True})
                return
            self.server.controller.resume()
            summary = self.server.crawl(roots, include_archives=include_archives)
            self._write_json({"ok": True, "summary": asdict(summary)})
            return
        if parsed.path == "/pause":
            self.server.controller.request_pause()
            self._write_json({"ok": True, "paused": True})
            return
        if parsed.path == "/resume":
            self.server.controller.resume()
            self._write_json({"ok": True, "paused": False})
            return
        if parsed.path == "/auto-scan":
            current = self.server.auto_scan.config()
            roots = payload.get("roots", [str(root) for root in current.roots])
            if not isinstance(roots, list) or not all(isinstance(root, str) for root in roots):
                self._write_json(
                    {"ok": False, "error": "roots must be a list of strings"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            next_config = AutoScanConfig(
                enabled=bool(payload.get("enabled", current.enabled)),
                interval_seconds=max(
                    60,
                    _parse_int(payload.get("interval_seconds"), current.interval_seconds),
                ),
                idle_seconds=max(
                    0,
                    _parse_int(payload.get("idle_seconds"), current.idle_seconds),
                ),
                require_charging=bool(
                    payload.get("require_charging", current.require_charging)
                ),
                roots=tuple(Path(root) for root in roots),
                check_interval_seconds=max(
                    1,
                    _parse_int(
                        payload.get("check_interval_seconds"),
                        current.check_interval_seconds,
                    ),
                ),
            )
            self.server.auto_scan.configure(next_config)
            self._write_json({"ok": True, "auto_scan": self.server.auto_scan.status()})
            return
        if parsed.path == "/saved-searches":
            name = str(payload.get("name", "")).strip()
            query = str(payload.get("query", "")).strip()
            raw_filters = payload.get("filters", {})
            if not name or not query or not isinstance(raw_filters, dict):
                self._write_json(
                    {
                        "ok": False,
                        "error": "name, query, and filters object are required",
                    },
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            saved_id = self.server.runtime.db.save_search(
                name=name,
                query=query,
                filters={str(key): str(value) for key, value in raw_filters.items()},
            )
            self._write_json(
                {
                    "ok": True,
                    "saved_search": self.server.runtime.db.get_saved_search(saved_id),
                }
            )
            return
        if parsed.path == "/open":
            target = self._resolve_open_target(payload)
            if target is None:
                self._write_json(
                    {"ok": False, "error": "path or result_id is required"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            mode = str(payload.get("mode", "file"))
            if mode not in {"file", "folder"}:
                self._write_json(
                    {"ok": False, "error": "mode must be file or folder"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            local_path = _strip_virtual_suffix(target)
            if not local_path.exists():
                self._write_json(
                    {"ok": False, "error": "path not found", "path": str(local_path)},
                    status=HTTPStatus.NOT_FOUND,
                )
                return
            open_local_path(local_path, reveal_parent=(mode == "folder"))
            self._write_json({"ok": True, "path": str(local_path), "mode": mode})
            return
        self._write_json({"ok": False, "error": "not found"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def _status_payload(self) -> dict[str, Any]:
        db_path = self.server.runtime.db.path
        storage_usage = data_usage_bytes(self.server.runtime.data_dir)
        return {
            "ok": True,
            "database_path": str(db_path),
            "database_exists": db_path.exists(),
            "storage_usage_bytes": storage_usage,
            "storage_breakdown": _storage_breakdown(
                data_dir=self.server.runtime.data_dir,
                db_path=db_path,
            ),
            "storage_warning_bytes": STORAGE_WARNING_BYTES,
            "storage_hard_stop_bytes": STORAGE_HARD_STOP_BYTES,
            "storage_state": _storage_state(storage_usage),
            "latest_job": _latest_job_to_dict(self.server.runtime.db),
            "background_scan": asdict(self.server.background_summary()),
            "auto_scan": self.server.auto_scan.status(),
        }

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _resolve_open_target(self, payload: dict[str, Any]) -> str | None:
        if "path" in payload:
            return str(payload["path"])
        if "result_id" not in payload:
            return None
        chunk_id = _parse_chunk_id(str(payload["result_id"]))
        if chunk_id is None:
            return None
        try:
            return self.server.runtime.db.get_chunk(chunk_id).source_path
        except KeyError:
            return None

    def _write_json(
        self,
        payload: dict[str, Any],
        *,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_cors_headers(self) -> None:
        origin = _allowed_cors_origin(self.headers.get("Origin", ""))
        if origin is None:
            return
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")


def create_app_server(
    *,
    runtime: DocVecRuntime,
    host: str = "127.0.0.1",
    port: int = 8765,
    auto_scan_config: AutoScanConfig | None = None,
    auto_scan_enabled: bool = False,
) -> DocVecApiServer:
    if host != "127.0.0.1":
        raise ValueError("DocVec API only binds to 127.0.0.1")
    return DocVecApiServer(
        (host, port),
        runtime=runtime,
        auto_scan_config=auto_scan_config,
        auto_scan_enabled=auto_scan_enabled,
    )


def serve_app_api(runtime: DocVecRuntime, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = create_app_server(
        runtime=runtime,
        host=host,
        port=port,
        auto_scan_enabled=True,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


def open_local_path(path: Path, reveal_parent: bool = False) -> None:
    if sys.platform == "win32":
        if reveal_parent:
            subprocess.Popen(["explorer.exe", f"/select,{path}"])
            return
        os.startfile(path)  # type: ignore[attr-defined]
        return

    target = path.parent if reveal_parent else path
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen([opener, str(target)])


def _search_result_to_dict(result: SearchResult) -> dict[str, Any]:
    return {
        "chunk_id": result.chunk_id,
        "source_path": result.source_path,
        "source_kind": result.source_kind.value,
        "title": result.title,
        "snippet": result.snippet,
        "score": result.score,
        "rank_source": result.rank_source,
        "metadata": result.metadata,
    }


def _chunk_to_dict(chunk: Any) -> dict[str, Any]:
    return {
        "chunk_id": chunk.chunk_id,
        "source_path": chunk.source_path,
        "source_kind": chunk.source_kind.value,
        "title": chunk.title,
        "text": chunk.text,
        "metadata": chunk.metadata,
        "ordinal": chunk.ordinal,
        "content_hash": chunk.content_hash,
    }


def _latest_job_to_dict(db: Any) -> dict[str, Any] | None:
    with db.connect() as connection:
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
    if row is None:
        return None
    return {
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
    }


def _storage_breakdown(*, data_dir: Path, db_path: Path) -> dict[str, int]:
    sqlite_bytes = db_path.stat().st_size if db_path.exists() else 0
    vector_bytes = sum(path.stat().st_size for path in data_dir.glob("vectors*") if path.is_file())
    log_bytes = _path_usage_bytes(data_dir / "logs")
    total_bytes = data_usage_bytes(data_dir)
    known_bytes = sqlite_bytes + vector_bytes + log_bytes
    return {
        "sqlite_bytes": sqlite_bytes,
        "vector_bytes": vector_bytes,
        "log_bytes": log_bytes,
        "other_bytes": max(0, total_bytes - known_bytes),
    }


def _path_usage_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(child.stat().st_size for child in path.rglob("*") if child.is_file())


def _storage_state(storage_usage: int) -> str:
    if storage_usage >= STORAGE_HARD_STOP_BYTES:
        return "hard_stop"
    if storage_usage >= STORAGE_WARNING_BYTES:
        return "warning"
    return "ok"


def _parse_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError:
        return 10
    return max(1, min(limit, 50))


def _parse_search_filters(params: dict[str, list[str]]) -> dict[str, str]:
    filters: dict[str, str] = {}
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
        value = params.get(key, [""])[0].strip()
        if value:
            filters[key] = value
    return filters


def _parse_chunk_id(value: str) -> int | None:
    try:
        chunk_id = int(value)
    except (TypeError, ValueError):
        return None
    if chunk_id <= 0:
        return None
    return chunk_id


def _parse_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _allowed_cors_origin(origin: str) -> str | None:
    if not origin:
        return None
    parsed = urlparse(origin)
    if parsed.scheme not in {"http", "https", "tauri"}:
        return None
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return None
    return origin


def _strip_virtual_suffix(value: str) -> Path:
    return Path(value.split("#", 1)[0])
