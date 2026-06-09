from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileFingerprint:
    path: str
    size_bytes: int
    mtime_ns: int
    fingerprint: str


def fingerprint_file(path: Path) -> FileFingerprint:
    stat = path.stat()
    identity = f"{path}|{stat.st_size}|{stat.st_mtime_ns}"
    return FileFingerprint(
        path=str(path),
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        fingerprint=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
    )
