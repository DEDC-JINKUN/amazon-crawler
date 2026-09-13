"""Fail-closed XLSX reader for the ordinary crawler Operator."""
from __future__ import annotations

import io
import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET


ASIN_RE = re.compile(r"[A-Z0-9]{10}")
CELL_RE = re.compile(r"([A-Z]{1,3})([1-9][0-9]{0,6})")
HEADER_NAMES = {"asin", "amazon asin", "amazon_asin", "asin号", "产品asin", "商品asin"}
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


class XlsxRejected(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class XlsxLimits:
    max_file_bytes: int = 5 * 1024 * 1024
    max_uncompressed_bytes: int = 50 * 1024 * 1024
    max_entries: int = 1000
    max_compression_ratio: int = 200
    max_sheets: int = 20
    max_rows: int = 10_000
    max_unique_asins: int = 5_000
    max_cells: int = 200_000


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _column_number(letters: str) -> int:
    if re.fullmatch(r"[A-Z]{1,3}", letters) is None:
        raise XlsxRejected("invalid_cell_coordinate")
    value = 0
    for letter in letters:
        value = value * 26 + ord(letter) - 64
    if value > 16_384:
        raise XlsxRejected("invalid_cell_coordinate")
    return value


def _column_letter(number: int) -> str:
    parts = []
    while number:
        number, remainder = divmod(number - 1, 26)
        parts.append(chr(65 + remainder))
    return "".join(reversed(parts))


def _safe_name(name: str) -> str:
    if not name or "\\" in name or name.startswith("/"):
        raise XlsxRejected("unsafe_archive_path")
    path = PurePosixPath(name)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise XlsxRejected("unsafe_archive_path")
    return path.as_posix()


def _read_xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        payload = archive.read(name)
    except (KeyError, RuntimeError, zipfile.BadZipFile, OSError) as exc:
        raise XlsxRejected("malformed_xlsx") from exc
    lowered = payload.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise XlsxRejected("unsafe_xml")
    try:
        return ET.fromstring(payload)
    except ET.ParseError as exc:
        raise XlsxRejected("malformed_xlsx") from exc


def _validate_archive(data: bytes, limits: XlsxLimits) -> zipfile.ZipFile:
    if len(data) > limits.max_file_bytes:
        raise XlsxRejected("file_too_large")
    if not data.startswith(b"PK"):
        raise XlsxRejected("malformed_xlsx")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        entries = archive.infolist()
    except (OSError, zipfile.BadZipFile) as exc:
        raise XlsxRejected("malformed_xlsx") from exc
    if len(entries) > limits.max_entries:
        archive.close()
        raise XlsxRejected("archive_entry_limit")
    total = 0
    for item in entries:
        name = _safe_name(item.filename)
        if item.flag_bits & 0x1:
            archive.close()
            raise XlsxRejected("encrypted_xlsx_not_allowed")
        total += int(item.file_size)
        if total > limits.max_uncompressed_bytes:
            archive.close()
            raise XlsxRejected("archive_too_large")
        compressed = max(1, int(item.compress_size))
        if item.file_size > 1024 * 1024 and item.file_size / compressed > limits.max_compression_ratio:
            archive.close()
            raise XlsxRejected("suspicious_compression_ratio")
        lowered = name.lower()
        if lowered.endswith("vbaproject.bin") or "macroenabled" in lowered:
            archive.close()
            raise XlsxRejected("macro_not_allowed")
        if lowered.startswith("xl/externallinks/"):
            archive.close()
            raise XlsxRejected("external_link_not_allowed")
    if len({item.filename for item in entries}) != len(entries):
        archive.close()
        raise XlsxRejected("duplicate_archive_entry")
    names = {item.filename for item in entries}
    if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names or "xl/_rels/workbook.xml.rels" not in names:
        archive.close()
        raise XlsxRejected("malformed_xlsx")
    try:
        content_types = archive.read("[Content_Types].xml").lower()
    except (KeyError, RuntimeError) as exc:
        archive.close()
        raise XlsxRejected("malformed_xlsx") from exc
    if b"macroenabled" in content_types or b"vba" in content_types:
        archive.close()
        raise XlsxRejected("macro_not_allowed")
    for item in entries:
        if not item.filename.lower().endswith(".rels"):
            continue
        root = _read_xml(archive, item.filename)
        for relationship in root.iter():
            if _local(relationship.tag) != "Relationship":
                continue
            if str(relationship.attrib.get("TargetMode", "")).lower() == "external":
                archive.close()
                raise XlsxRejected("external_link_not_allowed")
    return archive


def _shared_strings(archive: zipfile.ZipFile, limits: XlsxLimits) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _read_xml(archive, "xl/sharedStrings.xml")
    values = []
    for item in root:
        if _local(item.tag) != "si":
            continue
        value = "".join(node.text or "" for node in item.iter() if _local(node.tag) == "t")
        if len(value) > 32_767 or len(values) >= limits.max_cells:
            raise XlsxRejected("cell_limit")
        values.append(value)
    return values


def _sheet_paths(archive: zipfile.ZipFile, limits: XlsxLimits) -> list[tuple[str, str]]:
    workbook = _read_xml(archive, "xl/workbook.xml")
    relationships = _read_xml(archive, "xl/_rels/workbook.xml.rels")
    targets = {}
    for relationship in relationships.iter():
        if _local(relationship.tag) == "Relationship":
            targets[relationship.attrib.get("Id")] = (
                relationship.attrib.get("Target", ""),
                relationship.attrib.get("Type", ""),
            )
    sheets = []
    for node in workbook.iter():
        if _local(node.tag) != "sheet":
            continue
        relation_id = node.attrib.get(f"{{{DOC_REL_NS}}}id")
        target_value, relationship_type = targets.get(relation_id, ("", ""))
        target = str(target_value)
        if (
            not target
            or target.startswith("//")
            or "\\" in target
            or ".." in PurePosixPath(target.lstrip("/")).parts
            or not str(relationship_type).endswith("/worksheet")
        ):
            raise XlsxRejected("malformed_xlsx")
        resolved = posixpath.normpath(target[1:] if target.startswith("/") else posixpath.join("xl", target))
        _safe_name(resolved)
        if not resolved.startswith("xl/worksheets/"):
            raise XlsxRejected("malformed_xlsx")
        sheet_name = str(node.attrib.get("name") or "Sheet")
        if len(sheet_name) > 31:
            raise XlsxRejected("malformed_xlsx")
        sheets.append((sheet_name, resolved))
    if not sheets or len(sheets) > limits.max_sheets:
        raise XlsxRejected("sheet_limit")
    return sheets


def _sheet_rows(
    archive: zipfile.ZipFile,
    name: str,
    shared: list[str],
    limits: XlsxLimits,
    cell_counter: list[int],
) -> list[tuple[int, dict[int, tuple[str, bool]]]]:
    root = _read_xml(archive, name)
    rows = []
    previous_row_number = 0
    for row in root.iter():
        if _local(row.tag) != "row":
            continue
        if len(rows) >= limits.max_rows:
            raise XlsxRejected("row_limit")
        if len(row.attrib) > 16 or any(len(str(key)) + len(str(value)) > 128 for key, value in row.attrib.items()):
            raise XlsxRejected("malformed_xlsx")
        try:
            row_number = int(row.attrib.get("r") or len(rows) + 1)
        except ValueError as exc:
            raise XlsxRejected("malformed_xlsx") from exc
        if row_number <= previous_row_number or row_number > 1_048_576:
            raise XlsxRejected("malformed_xlsx")
        previous_row_number = row_number
        cells: dict[int, tuple[str, bool]] = {}
        for cell in row:
            if _local(cell.tag) != "c":
                continue
            cell_counter[0] += 1
            if cell_counter[0] > limits.max_cells:
                raise XlsxRejected("cell_limit")
            if len(cell.attrib) > 16 or any(len(str(key)) + len(str(value)) > 128 for key, value in cell.attrib.items()):
                raise XlsxRejected("malformed_xlsx")
            match = CELL_RE.fullmatch(str(cell.attrib.get("r") or ""))
            if match is None:
                raise XlsxRejected("invalid_cell_coordinate")
            column = _column_number(match.group(1))
            if int(match.group(2)) != row_number:
                raise XlsxRejected("invalid_cell_coordinate")
            formula = any(_local(child.tag) == "f" for child in cell)
            value_node = next((child for child in cell if _local(child.tag) == "v"), None)
            inline = "".join(node.text or "" for node in cell.iter() if _local(node.tag) == "t")
            raw = value_node.text if value_node is not None and value_node.text is not None else inline
            if cell.attrib.get("t") == "s" and raw:
                try:
                    raw = shared[int(raw)]
                except (IndexError, TypeError, ValueError) as exc:
                    raise XlsxRejected("malformed_xlsx") from exc
            cells[column] = (str(raw or ""), formula)
        rows.append((row_number, cells))
    return rows


def inspect_xlsx(
    data: bytes,
    limits: XlsxLimits | None = None,
    *,
    selected_sheet: str | None = None,
    selected_column: str | None = None,
) -> dict:
    """Return an ASIN-only projection; unrelated workbook cells never leave this function."""
    limits = limits or XlsxLimits()
    archive = _validate_archive(data, limits)
    try:
        shared = _shared_strings(archive, limits)
        loaded = []
        cell_counter = [0]
        for sheet_name, sheet_path in _sheet_paths(archive, limits):
            rows = _sheet_rows(archive, sheet_path, shared, limits, cell_counter)
            first_nonempty = next((index for index, (_, row) in enumerate(rows) if any(value.strip() for value, _ in row.values())), None)
            columns = []
            if first_nonempty is not None:
                header_row_number, header_cells = rows[first_nonempty]
                for number, (value, _) in sorted(header_cells.items()):
                    columns.append({"letter": _column_letter(number), "header": value.strip()[:80], "row": header_row_number})
            loaded.append({"name": sheet_name, "rows": rows, "columns": columns})
    finally:
        archive.close()

    chosen = None
    if selected_sheet is not None or selected_column is not None:
        if not selected_sheet or not selected_column or not re.fullmatch(r"[A-Z]{1,3}", selected_column.upper()):
            raise XlsxRejected("invalid_selection")
        sheet = next((item for item in loaded if item["name"] == selected_sheet), None)
        if sheet is None:
            raise XlsxRejected("invalid_selection")
        column_number = _column_number(selected_column.upper())
        header_index = next((index for index, (_, row) in enumerate(sheet["rows"]) if row.get(column_number, ("", False))[0].strip()), None)
        if header_index is None:
            raise XlsxRejected("invalid_selection")
        chosen = (sheet, column_number, header_index)
    else:
        matches = []
        for sheet in loaded:
            for index, (_, row) in enumerate(sheet["rows"][:20]):
                for number, (value, formula) in row.items():
                    if not formula and re.sub(r"\s+", " ", value.strip().lower()) in HEADER_NAMES:
                        matches.append((sheet, number, index))
        if len(matches) == 1:
            chosen = matches[0]

    public_sheets = [{"name": item["name"], "columns": item["columns"]} for item in loaded]
    if chosen is None:
        return {
            "selection_required": True,
            "sheets": public_sheets,
            "selected_sheet": None,
            "selected_column": None,
            "valid_rows": [],
            "duplicate_rows": [],
            "invalid_rows": [],
            "unique_count": 0,
            "can_start": False,
        }

    sheet, column_number, header_index = chosen
    valid_rows = []
    duplicate_rows = []
    invalid_rows = []
    first_seen = {}
    for row_number, row in sheet["rows"][header_index + 1 :]:
        raw, formula = row.get(column_number, ("", False))
        value = raw.strip().upper()
        if not value and not formula:
            continue
        if formula:
            invalid_rows.append({"row": row_number, "reason": "formula_not_allowed"})
            continue
        if ASIN_RE.fullmatch(value) is None:
            invalid_rows.append({"row": row_number, "reason": "invalid_asin"})
            continue
        if value in first_seen:
            duplicate_rows.append({"row": row_number, "first_row": first_seen[value], "asin": value})
            continue
        if len(first_seen) >= limits.max_unique_asins:
            raise XlsxRejected("unique_asin_limit")
        first_seen[value] = row_number
        valid_rows.append({"row": row_number, "asin": value})
    return {
        "selection_required": False,
        "sheets": public_sheets,
        "selected_sheet": sheet["name"],
        "selected_column": _column_letter(column_number),
        "header_row": sheet["rows"][header_index][0],
        "valid_rows": valid_rows,
        "duplicate_rows": duplicate_rows,
        "invalid_rows": invalid_rows,
        "unique_count": len(valid_rows),
        "can_start": bool(valid_rows) and not invalid_rows,
    }
