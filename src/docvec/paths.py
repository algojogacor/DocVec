from __future__ import annotations

from pathlib import Path

from docvec.config import (
    ARCHIVE_EXTENSIONS,
    BINARY_EXTENSIONS,
    MEDIA_EXTENSIONS,
    is_skip_dir_name,
)
from docvec.models import Classification, SourceKind

_ROOT_DRIVE_NOISE_FILE_NAMES = {"dumpstack.log", "dumpstack.log.tmp", "finish.log"}

_C_DRIVE_ALLOWED_SCOPES = (
    ("c:", "users", "arya rizky", ".codex", "sessions"),
    ("c:", "users", "arya rizky", ".codex", "memories"),
    ("c:", "users", "arya rizky", ".codex", "session_index.jsonl"),
    ("c:", "users", "arya rizky", ".gemini", "history"),
    ("c:", "users", "arya rizky", ".gemini", "memory"),
    ("c:", "users", "arya rizky", ".gemini", "antigravity", "brain"),
    ("c:", "users", "arya rizky", ".gemini", "antigravity", "conversations"),
    ("c:", "users", "arya rizky", ".gemini", "antigravity-ide", "brain"),
    ("c:", "users", "arya rizky", ".gemini", "antigravity-ide", "conversations"),
    ("c:", "users", "arya rizky", ".antigravity", "brain"),
    ("c:", "users", "arya rizky", ".antigravity", "conversations"),
    ("c:", "users", "arya rizky", ".antigravity-ide", "brain"),
    ("c:", "users", "arya rizky", ".antigravity-ide", "conversations"),
)


def _parts_lower(path: Path) -> list[str]:
    return [part for part in _path_lower(path).split("\\") if part]


def _path_lower(path: Path) -> str:
    return str(path).replace("/", "\\").lower()


def _has_prefix(parts: list[str], prefix: tuple[str, ...]) -> bool:
    return parts[: len(prefix)] == list(prefix)


def _is_prefix(parts: list[str], candidate: tuple[str, ...]) -> bool:
    return list(candidate[: len(parts)]) == parts


def _contains_sequence(parts: list[str], sequence: tuple[str, ...]) -> bool:
    sequence_length = len(sequence)
    return any(
        tuple(parts[index : index + sequence_length]) == sequence
        for index in range(len(parts) - sequence_length + 1)
    )


def _contains_after(parts: list[str], anchor: str, target: str) -> bool:
    try:
        anchor_index = parts.index(anchor)
    except ValueError:
        return False
    return target in parts[anchor_index + 1 :]


def _is_antigravity_root(parts: list[str]) -> bool:
    return (
        ".antigravity" in parts
        or ".antigravity-ide" in parts
        or _contains_sequence(parts, (".gemini", "antigravity"))
        or _contains_sequence(parts, (".gemini", "antigravity-ide"))
    )


def _is_appdata_path(parts: list[str]) -> bool:
    return _contains_sequence(parts, ("users", "arya rizky", "appdata")) or "appdata" in parts


def _is_game_install_path(parts: list[str]) -> bool:
    game_markers = {
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
    return any(part in game_markers for part in parts)


def _is_root_drive_noise_file(parts: list[str]) -> bool:
    return (
        len(parts) == 2
        and parts[0].endswith(":")
        and parts[1] in _ROOT_DRIVE_NOISE_FILE_NAMES
    )


def _is_docvec_runtime_data(parts: list[str]) -> bool:
    return _contains_sequence(parts, ("docvec", "data"))


def is_c_drive_allowed_scope_or_ancestor(path: Path) -> bool:
    parts = _parts_lower(path)
    return _is_c_drive_path(parts) and any(
        _has_prefix(parts, scope) or _is_prefix(parts, scope)
        for scope in _C_DRIVE_ALLOWED_SCOPES
    )


def _is_c_drive_path(parts: list[str]) -> bool:
    return bool(parts) and parts[0] == "c:"


def _classify_excluded_extension(path: Path, suffix: str) -> Classification | None:
    if suffix in MEDIA_EXTENSIONS:
        return Classification(path, SourceKind.MEDIA, True, "media_extension")

    if suffix in ARCHIVE_EXTENSIONS:
        return Classification(path, SourceKind.ARCHIVE, True, "archive_extension")

    if suffix in BINARY_EXTENSIONS:
        return Classification(path, SourceKind.BINARY, True, "binary_extension")

    return None


def should_skip_path(path: Path) -> bool:
    classification = classify_path(path)
    return classification.should_skip


def classify_path(path: Path) -> Classification:
    parts = _parts_lower(path)
    suffix = path.suffix.lower()

    if _is_root_drive_noise_file(parts):
        return Classification(path, SourceKind.SYSTEM_CACHE, True, "root_drive_noise_file")

    if _is_docvec_runtime_data(parts):
        return Classification(path, SourceKind.SYSTEM_CACHE, True, "docvec_runtime_data")

    if _contains_sequence(parts, (".gemini", "antigravity-backup")):
        return Classification(path, SourceKind.SYSTEM_CACHE, True, "antigravity_backup")

    if _contains_sequence(parts, (".gemini", "antigravity-browser-profile")):
        return Classification(
            path, SourceKind.SYSTEM_CACHE, True, "antigravity_browser_profile"
        )

    if _contains_after(parts, ".gemini", "skills"):
        return Classification(path, SourceKind.SYSTEM_CACHE, True, "gemini_skills")

    if _contains_after(parts, ".gemini", "extensions"):
        return Classification(path, SourceKind.SYSTEM_CACHE, True, "gemini_extensions")

    if _contains_after(parts, ".antigravity", "extensions") or _contains_after(
        parts, ".antigravity-ide", "extensions"
    ):
        return Classification(path, SourceKind.SYSTEM_CACHE, True, "antigravity_extensions")

    if _contains_sequence(parts, (".codex", "memories", ".git")):
        return Classification(path, SourceKind.SYSTEM_CACHE, True, "codex_memory_git")

    if _contains_sequence(parts, ("hermes", ".hermes", "state.db")):
        return Classification(path, SourceKind.AI_SESSION, False, "hermes_state_db")

    if _contains_sequence(parts, ("hermes", ".hermes", "memory_store.db")):
        return Classification(path, SourceKind.AI_MEMORY, False, "hermes_memory_store_db")

    if _is_antigravity_root(parts) and "conversations" in parts and suffix == ".pb":
        return Classification(path, SourceKind.AI_SESSION, False, "antigravity_pb")

    excluded_extension = _classify_excluded_extension(path, suffix)
    if excluded_extension is not None:
        return excluded_extension

    if _has_prefix(parts, ("d:", "hermes", "brain")):
        return Classification(path, SourceKind.AI_MEMORY, False, "hermes_brain")

    if _has_prefix(parts, ("d:", "hermes", ".hermes", "memories")):
        return Classification(path, SourceKind.AI_MEMORY, False, "hermes_memories")

    if _has_prefix(parts, ("d:", "hermes", "gbrain")):
        return Classification(path, SourceKind.AI_MEMORY, False, "hermes_gbrain")

    if _has_prefix(parts, ("d:", "hermes", "projects")):
        return Classification(path, SourceKind.PROJECT_SOURCE, False, "hermes_projects")

    if _contains_sequence(parts, (".codex", "sessions")):
        return Classification(path, SourceKind.AI_SESSION, False, "codex_session")

    if _contains_sequence(parts, (".codex", "memories")):
        return Classification(path, SourceKind.AI_MEMORY, False, "codex_memory")

    if _contains_sequence(parts, (".codex", "session_index.jsonl")):
        return Classification(path, SourceKind.AI_SESSION, False, "codex_session_index")

    if _is_antigravity_root(parts) and "brain" in parts:
        return Classification(path, SourceKind.AI_MEMORY, False, "antigravity_brain")

    if _contains_sequence(parts, (".gemini", "history")) or _contains_sequence(
        parts, (".gemini", "memory")
    ):
        return Classification(path, SourceKind.AI_MEMORY, False, "gemini_history_memory")

    if _is_antigravity_root(parts) and "conversations" in parts:
        return Classification(path, SourceKind.AI_SESSION, False, "antigravity_conversation")

    if _is_appdata_path(parts):
        return Classification(path, SourceKind.SYSTEM_CACHE, True, "appdata")

    if _is_game_install_path(parts):
        return Classification(path, SourceKind.SYSTEM_CACHE, True, "game_folder")

    if any(is_skip_dir_name(part) for part in parts):
        return Classification(path, SourceKind.SYSTEM_CACHE, True, "skip_dir")

    if _is_c_drive_path(parts):
        return Classification(path, SourceKind.SYSTEM_CACHE, True, "c_drive_unscoped")

    if _has_prefix(parts, ("d:",)):
        return Classification(path, SourceKind.PROJECT_SOURCE, False, "d_drive_project")

    return Classification(path, SourceKind.NORMAL_FILE, False, "normal_file")
