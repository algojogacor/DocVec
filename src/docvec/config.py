from __future__ import annotations

import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DOCVEC_ROOT = Path(r"D:\DocVec")
DATA_DIR = DOCVEC_ROOT / "data"
SQLITE_PATH = DATA_DIR / "docvec.sqlite"
VECTOR_PATH = DATA_DIR / "vectors.tvim"

STORAGE_WARNING_BYTES = 7 * 1024**3
STORAGE_TARGET_BYTES = 10 * 1024**3
STORAGE_HARD_STOP_BYTES = 12 * 1024**3

DEFAULT_MAX_FILE_BYTES = 20 * 1024**2
OFFICE_DOCUMENT_MAX_FILE_BYTES = 500 * 1024**2
PDF_MAX_FILE_BYTES = 150 * 1024**2
OFFICE_DOCUMENT_EXTENSIONS = {".docx", ".pptx", ".xlsx"}


class StorageBudgetExceeded(RuntimeError):
    """Raised when DocVec local data crosses the configured hard stop."""


_DATA_USAGE_CACHE_TTL_SECONDS = 60.0
_DATA_USAGE_CACHE: dict[Path, tuple[float, int]] = {}


def data_usage_bytes(data_dir: Path = DATA_DIR) -> int:
    if not data_dir.exists():
        return 0
    total = 0
    for path in data_dir.rglob("*"):
        if path.is_file():
            total += path.stat().st_size
    return total


def ensure_storage_budget(
    data_dir: Path = DATA_DIR,
    hard_stop_bytes: int = STORAGE_HARD_STOP_BYTES,
    target_bytes: int = STORAGE_TARGET_BYTES,
) -> None:
    # Cache recursive data-dir walks; indexing calls this often during batch ingestion.
    usage = _cached_data_usage_bytes(data_dir)
    warning_threshold = int(target_bytes * 0.8)
    if usage >= warning_threshold:
        logger.warning(
            "DocVec storage usage is approaching target: usage=%s target=%s hard_stop=%s",
            usage,
            target_bytes,
            hard_stop_bytes,
        )
    if usage >= hard_stop_bytes:
        raise StorageBudgetExceeded(
            f"DocVec data usage is {usage} bytes, above hard stop {hard_stop_bytes} bytes"
        )


def _cached_data_usage_bytes(data_dir: Path) -> int:
    cache_key = data_dir.resolve() if data_dir.exists() else data_dir
    now = time.monotonic()
    cached = _DATA_USAGE_CACHE.get(cache_key)
    if cached is not None:
        checked_at, usage = cached
        if now - checked_at < _DATA_USAGE_CACHE_TTL_SECONDS:
            return usage

    usage = data_usage_bytes(data_dir)
    _DATA_USAGE_CACHE[cache_key] = (now, usage)
    return usage


def max_file_bytes_for_path(path: Path) -> int:
    suffix = path.suffix.lower()
    if suffix in OFFICE_DOCUMENT_EXTENSIONS:
        return OFFICE_DOCUMENT_MAX_FILE_BYTES
    if suffix == ".pdf":
        return PDF_MAX_FILE_BYTES
    return DEFAULT_MAX_FILE_BYTES


SKIP_DIR_NAMES = {
    "$recycle.bin",
    ".android",
    ".cache",
    ".dart_tool",
    ".ollama",
    ".pub-cache",
    "boot",
    "system volume information",
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "recovery",
    "chrome-debug-profile",
    "ffmpeg",
    "inetpub",
    "intel",
    "jaxcorecache",
    "node_modules",
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    "dist",
    "build",
    "buildtools",
    "target",
    "out",
    ".next",
    ".nuxt",
    ".turbo",
    ".angular",
    ".nox",
    ".npm",
    ".parcel-cache",
    ".pip-cache",
    ".pnpm-store",
    ".svelte-kit",
    ".tox",
    ".uv-cache",
    ".vite",
    ".yarn",
    ".gradle",
    ".fvm",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "bower_components",
    "cache",
    "pods",
    "pub-cache",
    "tmp",
    "temp",
    "ollama",
    "hf_cache",
    "image_cache",
    "msdownld.tmp",
    "npm-cache",
    "onedrivetemp",
    "perflogs",
    "pip-cache",
    "pnpm-store",
    "site-packages",
    "temp_pip",
    "uv-cache",
    "ubuntu",
    "windows kits",
    "vs community",
    "wsl",
}

SKIP_DIR_PREFIXES = ("ffmpeg-",)


def is_skip_dir_name(name: str) -> bool:
    normalized = name.lower()
    return normalized in SKIP_DIR_NAMES or any(
        normalized.startswith(prefix) for prefix in SKIP_DIR_PREFIXES
    )

MEDIA_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".wmv",
    ".webm",
    ".mp3",
    ".wav",
    ".flac",
    ".ogg",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".svg",
}

ARCHIVE_EXTENSIONS = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".zst"}
BINARY_EXTENSIONS = {
    ".exe",
    ".dll",
    ".node",
    ".sys",
    ".iso",
    ".bin",
    ".dat",
    ".db",
    ".sqlite",
    ".sqlite-shm",
    ".sqlite-wal",
    ".tv",
    ".tvim",
}
