from __future__ import annotations

from pathlib import Path

DOCVEC_ROOT = Path(r"D:\DocVec")
DATA_DIR = DOCVEC_ROOT / "data"
SQLITE_PATH = DATA_DIR / "docvec.sqlite"
VECTOR_PATH = DATA_DIR / "vectors.tvim"

STORAGE_WARNING_BYTES = 7 * 1024**3
STORAGE_TARGET_BYTES = 10 * 1024**3
STORAGE_HARD_STOP_BYTES = 12 * 1024**3

DEFAULT_MAX_FILE_BYTES = 20 * 1024**2


class StorageBudgetExceeded(RuntimeError):
    """Raised when DocVec local data crosses the configured hard stop."""


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
) -> None:
    usage = data_usage_bytes(data_dir)
    if usage >= hard_stop_bytes:
        raise StorageBudgetExceeded(
            f"DocVec data usage is {usage} bytes, above hard stop {hard_stop_bytes} bytes"
        )


SKIP_DIR_NAMES = {
    "$recycle.bin",
    ".android",
    ".cache",
    ".ollama",
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
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "bower_components",
    "cache",
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
