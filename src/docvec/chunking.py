from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from docvec.models import Chunk, ExtractedRecord, SourceKind


@dataclass(frozen=True)
class _CodeBlock:
    symbol: str
    kind: str
    start_line: int
    end_line: int
    text: str


def _hash_text(text: str) -> str:
    normalized = " ".join(text.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def chunk_record(
    record: ExtractedRecord,
    *,
    max_words: int = 900,
    overlap_words: int = 120,
) -> list[Chunk]:
    if max_words <= 0:
        raise ValueError("max_words must be positive")
    if overlap_words < 0 or overlap_words >= max_words:
        raise ValueError("overlap_words must be >= 0 and < max_words")

    if _is_code_record(record):
        code_chunks = _chunk_code_record(record, max_words=max_words, overlap_words=overlap_words)
        if code_chunks:
            return code_chunks

    return _chunk_text_window(
        record=record,
        title=record.title,
        text=record.text,
        metadata=dict(record.metadata),
        ordinal_start=0,
        max_words=max_words,
        overlap_words=overlap_words,
    )


def _chunk_text_window(
    *,
    record: ExtractedRecord,
    title: str,
    text: str,
    metadata: dict[str, str],
    ordinal_start: int,
    max_words: int,
    overlap_words: int,
) -> list[Chunk]:
    words, line_numbers = _words_with_line_numbers(text, _base_line(metadata))
    if not words:
        return []

    chunks: list[Chunk] = []
    start = 0
    ordinal = ordinal_start
    step = max_words - overlap_words

    while start < len(words):
        end = min(start + max_words, len(words))
        text = " ".join(words[start:end])
        chunk_metadata = dict(metadata)
        chunk_metadata["start_line"] = str(line_numbers[start])
        chunk_metadata["end_line"] = str(line_numbers[end - 1])
        chunks.append(
            Chunk(
                chunk_id=None,
                source_path=record.source_path,
                source_kind=record.source_kind,
                title=title,
                text=text,
                metadata=chunk_metadata,
                ordinal=ordinal,
                content_hash=_hash_text(text),
            )
        )
        if end == len(words):
            break
        start += step
        ordinal += 1

    return chunks


def _chunk_code_record(
    record: ExtractedRecord,
    *,
    max_words: int,
    overlap_words: int,
) -> list[Chunk]:
    blocks = _code_blocks(record)
    if not blocks:
        return []

    chunks: list[Chunk] = []
    ordinal = 0
    for block in blocks:
        metadata = dict(record.metadata)
        metadata["code_symbol"] = block.symbol
        metadata["code_symbol_kind"] = block.kind
        metadata["start_line"] = str(block.start_line)
        metadata["end_line"] = str(block.end_line)
        block_chunks = _chunk_text_window(
            record=record,
            title=f"{record.title} {block.kind} {block.symbol}",
            text=block.text,
            metadata=metadata,
            ordinal_start=ordinal,
            max_words=max_words,
            overlap_words=overlap_words,
        )
        chunks.extend(block_chunks)
        ordinal += len(block_chunks)
    return chunks


def _code_blocks(record: ExtractedRecord) -> list[_CodeBlock]:
    extension = _record_extension(record)
    base_line = _base_line(record.metadata)
    lines = record.text.splitlines()

    if extension == ".py":
        symbols = _python_top_level_symbols(lines, base_line)
    elif extension in _JAVASCRIPT_EXTENSIONS:
        symbols = _javascript_top_level_symbols(lines, base_line)
    elif extension == ".go":
        symbols = _go_top_level_symbols(lines, base_line)
    elif extension == ".rs":
        symbols = _rust_top_level_symbols(lines, base_line)
    elif extension == ".sql":
        symbols = _sql_top_level_symbols(lines, base_line)
    elif extension in _C_LIKE_EXTENSIONS:
        symbols = _c_like_top_level_symbols(lines, base_line)
    else:
        return []

    return _blocks_from_symbols(lines, symbols, base_line)


def _python_top_level_symbols(lines: list[str], base_line: int) -> list[tuple[int, str, str]]:
    symbols: list[tuple[int, str, str]] = []
    pattern = re.compile(r"^(def|class)\s+([A-Za-z_][A-Za-z0-9_]*)\b")
    for index, line in enumerate(lines):
        match = pattern.match(line)
        if not match:
            continue
        raw_kind, symbol = match.groups()
        kind = "function" if raw_kind == "def" else "class"
        symbols.append((base_line + index, kind, symbol))
    return symbols


def _javascript_top_level_symbols(
    lines: list[str],
    base_line: int,
) -> list[tuple[int, str, str]]:
    symbols: list[tuple[int, str, str]] = []
    function_pattern = re.compile(
        r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)\b"
    )
    class_pattern = re.compile(
        r"^(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)\b"
    )
    arrow_pattern = re.compile(
        r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=.*=>"
    )
    for index, line in enumerate(lines):
        function_match = function_pattern.match(line)
        if function_match:
            symbols.append((base_line + index, "function", function_match.group(1)))
            continue

        class_match = class_pattern.match(line)
        if class_match:
            symbols.append((base_line + index, "class", class_match.group(1)))
            continue

        arrow_match = arrow_pattern.match(line)
        if arrow_match:
            symbols.append((base_line + index, "function", arrow_match.group(1)))
    return symbols


def _go_top_level_symbols(lines: list[str], base_line: int) -> list[tuple[int, str, str]]:
    symbols: list[tuple[int, str, str]] = []
    function_pattern = re.compile(r"^func\s+(?:\([^)]+\)\s*)?([A-Za-z_][A-Za-z0-9_]*)\b")
    type_pattern = re.compile(
        r"^type\s+([A-Za-z_][A-Za-z0-9_]*)\s+(?:struct|interface)\b"
    )
    for index, line in enumerate(lines):
        function_match = function_pattern.match(line)
        if function_match:
            symbols.append((base_line + index, "function", function_match.group(1)))
            continue

        type_match = type_pattern.match(line)
        if type_match:
            symbols.append((base_line + index, "class", type_match.group(1)))
    return symbols


def _rust_top_level_symbols(lines: list[str], base_line: int) -> list[tuple[int, str, str]]:
    symbols: list[tuple[int, str, str]] = []
    visibility = r"(?:pub(?:\([^)]*\))?\s+)?"
    function_pattern = re.compile(
        rf"^{visibility}(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\b"
    )
    type_pattern = re.compile(
        rf"^{visibility}(?:struct|enum|trait)\s+([A-Za-z_][A-Za-z0-9_]*)\b"
    )
    impl_pattern = re.compile(r"^impl(?:<[^>]+>)?\s+([A-Za-z_][A-Za-z0-9_]*)\b")
    for index, line in enumerate(lines):
        function_match = function_pattern.match(line)
        if function_match:
            symbols.append((base_line + index, "function", function_match.group(1)))
            continue

        type_match = type_pattern.match(line)
        if type_match:
            symbols.append((base_line + index, "class", type_match.group(1)))
            continue

        impl_match = impl_pattern.match(line)
        if impl_match:
            symbols.append((base_line + index, "class", impl_match.group(1)))
    return symbols


def _sql_top_level_symbols(lines: list[str], base_line: int) -> list[tuple[int, str, str]]:
    symbols: list[tuple[int, str, str]] = []
    pattern = re.compile(
        r"^CREATE\s+(?:OR\s+REPLACE\s+)?"
        r"(TABLE|VIEW|FUNCTION|PROCEDURE|TRIGGER)\s+"
        r"(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_.$]*)\b",
        flags=re.IGNORECASE,
    )
    for index, line in enumerate(lines):
        match = pattern.match(line)
        if not match:
            continue
        raw_kind, symbol = match.groups()
        kind = "function" if raw_kind.upper() in {"FUNCTION", "PROCEDURE"} else "class"
        symbols.append((base_line + index, kind, symbol.split(".")[-1]))
    return symbols


def _c_like_top_level_symbols(lines: list[str], base_line: int) -> list[tuple[int, str, str]]:
    symbols: list[tuple[int, str, str]] = []
    class_pattern = re.compile(
        r"^(?:(?:public|private|protected|internal|abstract|sealed|final|static)\s+)*"
        r"(?:class|interface|enum|struct)\s+([A-Za-z_][A-Za-z0-9_]*)\b"
    )
    function_pattern = re.compile(
        r"^(?!(?:if|for|while|switch|catch|return|new)\b)"
        r"(?:[\w:<>,~*&\[\]?]+\s+)+"
        r"([A-Za-z_][A-Za-z0-9_]*)\s*\([^;{}]*\)\s*(?:async\s*)?(?:const\s*)?(?:\{|=>)"
    )
    for index, line in enumerate(lines):
        class_match = class_pattern.match(line)
        if class_match:
            symbols.append((base_line + index, "class", class_match.group(1)))
            continue

        function_match = function_pattern.match(line)
        if function_match:
            symbols.append((base_line + index, "function", function_match.group(1)))
    return symbols


def _blocks_from_symbols(
    lines: list[str],
    symbols: list[tuple[int, str, str]],
    base_line: int,
) -> list[_CodeBlock]:
    if not symbols:
        return []

    blocks: list[_CodeBlock] = []
    for index, (start_line, kind, symbol) in enumerate(symbols):
        next_start = (
            symbols[index + 1][0]
            if index + 1 < len(symbols)
            else base_line + len(lines)
        )
        start_index = start_line - base_line
        end_index = next_start - base_line
        while end_index > start_index and not lines[end_index - 1].strip():
            end_index -= 1
        if end_index <= start_index:
            continue
        block_text = "\n".join(lines[start_index:end_index])
        blocks.append(
            _CodeBlock(
                symbol=symbol,
                kind=kind,
                start_line=start_line,
                end_line=base_line + end_index - 1,
                text=block_text,
            )
        )
    return blocks


def _is_code_record(record: ExtractedRecord) -> bool:
    return (
        record.source_kind == SourceKind.PROJECT_SOURCE
        or _record_extension(record) in _CODE_EXTENSIONS
    )


def _record_extension(record: ExtractedRecord) -> str:
    extension = str(record.metadata.get("extension", "")).lower().strip()
    if extension:
        return extension
    return Path(record.source_path).suffix.lower()


_CODE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".dart",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".py",
    ".rs",
    ".sql",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
}

_JAVASCRIPT_EXTENSIONS = {
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
}

_C_LIKE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".dart",
    ".h",
    ".hpp",
    ".java",
    ".kt",
    ".php",
    ".swift",
}


def _words_with_line_numbers(text: str, base_line: int) -> tuple[list[str], list[int]]:
    words: list[str] = []
    line_numbers: list[int] = []
    for offset, line in enumerate(text.splitlines(), start=0):
        line_words = line.split()
        if not line_words:
            continue
        words.extend(line_words)
        line_numbers.extend([base_line + offset] * len(line_words))
    return words, line_numbers


def _base_line(metadata: dict[str, str]) -> int:
    for key in ("start_line", "line"):
        try:
            value = int(str(metadata.get(key, "")).strip())
        except ValueError:
            continue
        if value > 0:
            return value
    return 1
