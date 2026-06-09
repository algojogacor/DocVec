from __future__ import annotations

import csv
import email
import html
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib
from email import policy
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from posixpath import normpath

from docvec.models import ExtractedRecord, SourceKind


def extract_pdf_file(path: Path, source_kind: SourceKind) -> ExtractedRecord:
    data = path.read_bytes()
    stream_text = "\n".join(_decode_pdf_streams(data))
    text = "\n".join(_extract_pdf_strings(stream_text))
    return _record(path, source_kind, text, "pdf")


def extract_docx_file(path: Path, source_kind: SourceKind) -> ExtractedRecord:
    with zipfile.ZipFile(path) as archive:
        text = _xml_text_from_zip(archive, ["word/document.xml"], {"t"})
    return _record(path, source_kind, text, "docx")


def extract_pptx_file(path: Path, source_kind: SourceKind) -> ExtractedRecord:
    with zipfile.ZipFile(path) as archive:
        slide_names = sorted(
            name
            for name in archive.namelist()
            if name.startswith("ppt/slides/slide") and name.endswith(".xml")
        )
        text = _xml_text_from_zip(archive, slide_names, {"t"})
    return _record(path, source_kind, text, "pptx")


def extract_xlsx_file(path: Path, source_kind: SourceKind) -> ExtractedRecord:
    with zipfile.ZipFile(path) as archive:
        shared_strings = _read_xlsx_shared_strings(archive)
        sheet_names = sorted(
            name
            for name in archive.namelist()
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
        )
        rows: list[str] = []
        for sheet_name in sheet_names:
            rows.extend(_read_xlsx_sheet(archive, sheet_name, shared_strings))
    return _record(path, source_kind, "\n".join(rows), "xlsx")


def extract_csv_file(path: Path, source_kind: SourceKind) -> ExtractedRecord:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    reader = csv.reader(StringIO(raw))
    lines = [" | ".join(cell.strip() for cell in row if cell.strip()) for row in reader]
    text = "\n".join(line for line in lines if line)
    return _record(path, source_kind, text, "csv")


def extract_html_file(path: Path, source_kind: SourceKind) -> ExtractedRecord:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    text = _visible_html_text(raw)
    return _record(path, source_kind, text, "html")


def extract_mhtml_file(path: Path, source_kind: SourceKind) -> ExtractedRecord:
    message = email.message_from_bytes(path.read_bytes(), policy=policy.default)
    parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            if content_type == "text/html":
                parts.append(_visible_html_text(str(part.get_content())))
            elif content_type == "text/plain":
                parts.append(str(part.get_content()).strip())
    else:
        payload = str(message.get_content())
        if message.get_content_type() == "text/html":
            parts.append(_visible_html_text(payload))
        else:
            parts.append(payload.strip())
    text = "\n".join(part for part in parts if part)
    return _record(path, source_kind, text, "mhtml")


def extract_transcript_file(path: Path, source_kind: SourceKind) -> ExtractedRecord:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.upper() == "WEBVTT":
            continue
        if stripped.isdigit():
            continue
        if "-->" in stripped and _looks_like_timecode(stripped):
            continue
        if stripped.startswith(("NOTE", "STYLE", "REGION")):
            continue
        lines.append(stripped)
    return ExtractedRecord(
        source_path=str(path),
        source_kind=source_kind,
        title=path.name,
        text="\n".join(lines),
        metadata={
            "extension": path.suffix.lower(),
            "extractor": "transcript",
            "file_type": "transcript",
        },
    )


def extract_antigravity_pb(path: Path) -> list[ExtractedRecord]:
    data = path.read_bytes()
    strings = _extract_printable_utf8_strings(data)
    text = "\n".join(strings)
    if not text.strip():
        return []
    return [
        ExtractedRecord(
            source_path=str(path),
            source_kind=SourceKind.AI_SESSION,
            title=path.name,
            text=text,
            metadata={"source": "antigravity", "extractor": "protobuf_strings"},
        )
    ]


def extract_zip_archive(path: Path, source_kind: SourceKind) -> list[ExtractedRecord]:
    records: list[ExtractedRecord] = []
    with zipfile.ZipFile(path) as archive:
        for item in sorted(archive.infolist(), key=lambda info: info.filename.lower()):
            entry_name = _safe_zip_entry_name(item.filename)
            if item.is_dir() or entry_name is None:
                continue
            extension = Path(entry_name).suffix.lower()
            if extension not in _ZIP_TEXT_EXTENSIONS:
                continue
            if item.file_size > _MAX_ZIP_TEXT_ENTRY_BYTES:
                continue
            raw = archive.read(item)
            text = _zip_entry_text(raw, extension)
            if not text.strip():
                continue
            records.append(
                ExtractedRecord(
                    source_path=f"{path}#{entry_name}",
                    source_kind=source_kind,
                    title=f"{path.name} :: {entry_name}",
                    text=text,
                    metadata={
                        "extension": extension,
                        "extractor": "zip",
                        "archive_path": str(path),
                        "archive_entry": entry_name,
                    },
                )
            )
    return records


def _record(
    path: Path,
    source_kind: SourceKind,
    text: str,
    extractor: str,
) -> ExtractedRecord:
    return ExtractedRecord(
        source_path=str(path),
        source_kind=source_kind,
        title=path.name,
        text=text,
        metadata={"extension": path.suffix.lower(), "extractor": extractor},
    )


def _decode_pdf_streams(data: bytes) -> list[str]:
    streams: list[str] = []
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.DOTALL):
        raw = match.group(1).strip(b"\r\n")
        decoded = _try_flate(raw)
        streams.append(decoded.decode("latin-1", errors="ignore"))
    return streams


def _visible_html_text(raw: str) -> str:
    parser = _VisibleTextHTMLParser()
    parser.feed(raw)
    return "\n".join(parser.parts)


def _zip_entry_text(raw: bytes, extension: str) -> str:
    text = raw.decode("utf-8-sig", errors="replace")
    if extension in {".html", ".htm"}:
        return _visible_html_text(text)
    if extension == ".mhtml":
        message = email.message_from_bytes(raw, policy=policy.default)
        if message.is_multipart():
            parts = []
            for part in message.walk():
                if part.get_content_type() == "text/html":
                    parts.append(_visible_html_text(str(part.get_content())))
                elif part.get_content_type() == "text/plain":
                    parts.append(str(part.get_content()).strip())
            return "\n".join(part for part in parts if part)
    return text


def _safe_zip_entry_name(value: str) -> str | None:
    normalized = normpath(value.replace("\\", "/")).lstrip("/")
    if normalized in {"", "."}:
        return None
    if normalized == ".." or normalized.startswith("../"):
        return None
    return normalized


def _looks_like_timecode(value: str) -> bool:
    return bool(re.search(r"\d{1,2}:\d{2}:\d{2}[,.]\d{3}", value))


def _try_flate(data: bytes) -> bytes:
    try:
        return zlib.decompress(data)
    except zlib.error:
        return data


def _extract_pdf_strings(text: str) -> list[str]:
    strings = [_decode_pdf_literal(match) for match in re.findall(r"\((.*?)\)", text)]
    hex_strings = [
        bytes.fromhex(match).decode("utf-8", errors="replace")
        for match in re.findall(r"<([0-9A-Fa-f]{4,})>", text)
    ]
    return [item for item in strings + hex_strings if item.strip()]


def _decode_pdf_literal(value: str) -> str:
    return (
        value.replace(r"\(", "(")
        .replace(r"\)", ")")
        .replace(r"\\", "\\")
        .replace(r"\n", "\n")
    )


def _xml_text_from_zip(
    archive: zipfile.ZipFile,
    names: list[str],
    local_names: set[str],
) -> str:
    parts: list[str] = []
    for name in names:
        if name not in archive.namelist():
            continue
        root = ET.fromstring(archive.read(name))
        for element in root.iter():
            local_name = element.tag.rsplit("}", 1)[-1]
            if local_name in local_names and element.text:
                parts.append(element.text.strip())
    return "\n".join(part for part in parts if part)


def _read_xlsx_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    strings: list[str] = []
    for item in root.iter():
        if item.tag.rsplit("}", 1)[-1] != "si":
            continue
        parts = [
            child.text.strip()
            for child in item.iter()
            if child.tag.rsplit("}", 1)[-1] == "t" and child.text
        ]
        strings.append("".join(parts))
    return strings


def _read_xlsx_sheet(
    archive: zipfile.ZipFile,
    name: str,
    shared_strings: list[str],
) -> list[str]:
    root = ET.fromstring(archive.read(name))
    rows: list[str] = []
    for row in root.iter():
        if row.tag.rsplit("}", 1)[-1] != "row":
            continue
        values: list[str] = []
        for cell in row:
            if cell.tag.rsplit("}", 1)[-1] != "c":
                continue
            cell_type = cell.attrib.get("t", "")
            raw_value = None
            for child in cell:
                if child.tag.rsplit("}", 1)[-1] == "v":
                    raw_value = child.text
                    break
            if raw_value is None:
                continue
            if cell_type == "s":
                index = int(raw_value)
                if 0 <= index < len(shared_strings):
                    values.append(shared_strings[index])
            else:
                values.append(raw_value)
        if values:
            rows.append(" | ".join(values))
    return rows


def _extract_printable_utf8_strings(data: bytes) -> list[str]:
    decoded = data.decode("utf-8", errors="ignore")
    strings = re.findall(r"[\w\s.,:;!?@#%&/\\\-+()]{4,}", decoded, flags=re.UNICODE)
    return [" ".join(item.split()) for item in strings if item.strip()]


class _VisibleTextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden_depth = 0

    def handle_starttag(self, tag: str, _attrs) -> None:
        if tag.lower() in {"script", "style"}:
            self._hidden_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self._hidden_depth > 0:
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._hidden_depth:
            return
        text = " ".join(html.unescape(data).split())
        if text:
            self.parts.append(text)


_MAX_ZIP_TEXT_ENTRY_BYTES = 2 * 1024 * 1024

_ZIP_TEXT_EXTENSIONS = {
    ".bat",
    ".c",
    ".cc",
    ".cfg",
    ".cmd",
    ".conf",
    ".cpp",
    ".cs",
    ".css",
    ".csv",
    ".dart",
    ".env",
    ".go",
    ".h",
    ".hpp",
    ".htm",
    ".html",
    ".ini",
    ".java",
    ".js",
    ".json",
    ".jsonl",
    ".jsx",
    ".kt",
    ".log",
    ".lua",
    ".markdown",
    ".md",
    ".mhtml",
    ".php",
    ".properties",
    ".ps1",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".vue",
    ".xml",
    ".yaml",
    ".yml",
}
