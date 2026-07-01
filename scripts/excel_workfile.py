#!/usr/bin/env python3
"""Maintain an Excel workfile for no-API issue classification."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence
from xml.sax.saxutils import escape

from pipeline_core import ContractError, normalize_name, validate_taxonomy_selection


SKILL_DIR = Path(__file__).resolve().parents[1]
REFERENCE_DIR = SKILL_DIR / "references"
DEFAULT_CLASSIFICATION_OPTIONS = REFERENCE_DIR / "classification_options.json"
SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
OFFICE_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

FIELD_ALIASES = {
    "id": ["id", "record_id", "case_id", "ticket_id", "工单编号", "问题编号", "记录编号", "单号", "编号"],
    "problem_overview": [
        "problem_overview",
        "问题概述",
        "问题标题",
        "故障标题",
        "概述",
        "标题",
        "overview",
        "summary",
        "title",
    ],
    "probelm_details": [
        "probelm_details",
        "problem_details",
        "问题明细",
        "问题详情",
        "问题描述",
        "故障现象",
        "故障详情",
        "现象描述",
        "detail",
        "details",
        "description",
        "desc",
    ],
    "solution_details": [
        "solution_details",
        "解决方案",
        "处理方案",
        "解决措施",
        "处理措施",
        "处理结果",
        "solution",
        "resolution",
        "fix",
    ],
}

RECORD_FIELDS = ["id", "problem_overview", "probelm_details", "solution_details"]
INTERNAL_COLUMNS = [
    "__ic_record_id",
    "__ic_problem_overview",
    "__ic_probelm_details",
    "__ic_solution_details",
    "__ic_user_solution",
]
RESULT_COLUMNS = [
    "selected_level_1",
    "selected_level_2",
    "confidence",
    "status",
    "needs_review",
    "mapping_justification",
    "topk_candidates",
    "error_message",
    "processed_at",
]
SUCCESS_STATUSES = {"classified", "low_confidence"}
RETRY_STATUSES = {"retry", "unresolved", "failed"}


class ExcelWorkfileError(RuntimeError):
    pass


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON file {path}: {exc}") from exc


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def column_index(cell_ref: str) -> int:
    match = re.match(r"([A-Z]+)", cell_ref.upper())
    if not match:
        return 0
    index = 0
    for char in match.group(1):
        index = index * 26 + ord(char) - ord("A") + 1
    return index - 1


def column_name(index: int) -> str:
    letters = ""
    number = index + 1
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def cell_ref(row_index: int, column_zero_index: int) -> str:
    return f"{column_name(column_zero_index)}{row_index}"


def first_xlsx_sheet_path(zf: zipfile.ZipFile) -> str:
    fallback = "xl/worksheets/sheet1.xml"
    try:
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    except (KeyError, ET.ParseError):
        return fallback

    first_sheet_id = ""
    for node in workbook.iter():
        if local_name(node.tag) == "sheet":
            first_sheet_id = node.attrib.get(f"{OFFICE_REL_NS}id", "")
            break
    if not first_sheet_id:
        return fallback

    for rel in rels:
        if rel.attrib.get("Id") == first_sheet_id:
            target = rel.attrib.get("Target", "worksheets/sheet1.xml")
            return "xl/" + target.lstrip("/") if not target.startswith("xl/") else target
    return fallback


def read_shared_strings(zf: zipfile.ZipFile) -> List[str]:
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except (KeyError, ET.ParseError):
        return []
    values: List[str] = []
    for item in root.findall(f"{SHEET_NS}si"):
        values.append("".join(node.text or "" for node in item.iter(f"{SHEET_NS}t")))
    return values


def cell_text(cell: ET.Element, shared_strings: Sequence[str]) -> str:
    raw = cell.find(f"{SHEET_NS}v")
    inline = cell.find(f"{SHEET_NS}is/{SHEET_NS}t")
    cell_type = cell.attrib.get("t")
    if cell_type == "s" and raw is not None:
        try:
            return shared_strings[int(raw.text or "0")]
        except (IndexError, ValueError):
            return ""
    if inline is not None:
        return inline.text or ""
    if raw is not None:
        return raw.text or ""
    return ""


def read_xlsx_matrix(path: Path) -> List[List[str]]:
    with zipfile.ZipFile(path) as zf:
        shared_strings = read_shared_strings(zf)
        sheet_xml = zf.read(first_xlsx_sheet_path(zf))
    sheet = ET.fromstring(sheet_xml)
    sheet_data = sheet.find(f"{SHEET_NS}sheetData")
    if sheet_data is None:
        return []

    rows: List[List[str]] = []
    for row in sheet_data.findall(f"{SHEET_NS}row"):
        values: Dict[int, str] = {}
        for cell in row.findall(f"{SHEET_NS}c"):
            ref = cell.attrib.get("r", "A1")
            values[column_index(ref)] = cell_text(cell, shared_strings)
        width = max(values.keys(), default=-1) + 1
        rows.append([values.get(index, "") for index in range(width)])
    return rows


def write_xlsx_matrix(path: Path, rows: Sequence[Sequence[str]]) -> None:
    with zipfile.ZipFile(path) as zf:
        entries = {name: zf.read(name) for name in zf.namelist()}
        sheet_path = first_xlsx_sheet_path(zf)

    sheet_rows = []
    for row_index, row in enumerate(rows, 1):
        cells = []
        for column_zero_index, value in enumerate(row):
            text = "" if value is None else str(value)
            cells.append(
                f'<c r="{cell_ref(row_index, column_zero_index)}" t="inlineStr"><is><t>{escape(text)}</t></is></c>'
            )
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    max_row = len(rows)
    max_col = max((len(row) for row in rows), default=0)
    dimension = f'A1:{cell_ref(max_row, max_col - 1)}' if max_row and max_col else "A1"
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="{dimension}"/>'
        f"<sheetData>{''.join(sheet_rows)}</sheetData>"
        "</worksheet>"
    ).encode("utf-8")

    tmp = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, sheet_xml if name == sheet_path else data)
    os.replace(tmp, path)


def rows_to_dicts(matrix: Sequence[Sequence[str]]) -> tuple[List[str], List[Dict[str, str]]]:
    if not matrix:
        return [], []
    headers = [str(item) for item in matrix[0]]
    data_rows: List[Dict[str, str]] = []
    for row_number, row in enumerate(matrix[1:], 2):
        item = {headers[index]: (row[index] if index < len(row) else "") for index in range(len(headers))}
        item["__row_number"] = str(row_number)
        data_rows.append(item)
    return headers, data_rows


def align_width(rows: List[List[str]], width: int) -> None:
    for row in rows:
        if len(row) < width:
            row.extend([""] * (width - len(row)))


def ensure_columns(headers: List[str], rows: List[List[str]], columns: Sequence[str]) -> None:
    for column in columns:
        if column not in headers:
            headers.append(column)
    align_width(rows, len(headers))


def infer_field_map(headers: Sequence[str]) -> Dict[str, str]:
    normalized = {normalize_name(header).lower(): header for header in headers}
    field_map: Dict[str, str] = {}
    for field, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            key = normalize_name(alias).lower()
            if key in normalized:
                field_map[field] = normalized[key]
                break
    return field_map


def first_value(row: Mapping[str, str], column: str | None) -> str:
    if not column:
        return ""
    return str(row.get(column, "")).strip()


def normalized_record(row: Mapping[str, str], field_map: Mapping[str, str]) -> Dict[str, str]:
    record = {
        "id": first_value(row, field_map.get("id")),
        "problem_overview": first_value(row, field_map.get("problem_overview")),
        "probelm_details": first_value(row, field_map.get("probelm_details")),
        "solution_details": first_value(row, field_map.get("solution_details")),
    }
    record["user_solution"] = "\n".join(
        value for value in [record["problem_overview"], record["probelm_details"], record["solution_details"]] if value
    )
    return record


def load_field_map(path: Path) -> Dict[str, str]:
    payload = read_json(path)
    field_map = payload.get("field_map", payload) if isinstance(payload, dict) else {}
    return {field: str(field_map.get(field, "")).strip() for field in RECORD_FIELDS if field_map.get(field)}


def load_classification_options(path: Path) -> Mapping[str, Mapping[str, Sequence[str]]]:
    payload = read_json(path)
    if not isinstance(payload, dict) or not payload:
        raise SystemExit(f"classification_options is empty or invalid: {path}")
    return payload


def update_row(row: List[str], headers: Sequence[str], values: Mapping[str, Any]) -> None:
    if len(row) < len(headers):
        row.extend([""] * (len(headers) - len(row)))
    for key, value in values.items():
        if key in headers:
            row[headers.index(key)] = "" if value is None else str(value)


def command_inspect_input(args: argparse.Namespace) -> None:
    matrix = read_xlsx_matrix(args.input)
    headers, rows = rows_to_dicts(matrix)
    field_map = infer_field_map(headers)
    sample_records = [normalized_record(row, field_map) for row in rows[: args.sample_size]]
    write_json(
        args.out,
        {
            "confirmation_required": True,
            "field_map": field_map,
            "sample_records": sample_records,
        },
    )
    print(f"Wrote field map review to {args.out}")


def command_init(args: argparse.Namespace) -> None:
    matrix = read_xlsx_matrix(args.input)
    if not matrix:
        raise SystemExit(f"Excel file has no rows: {args.input}")
    rows = [list(row) for row in matrix]
    headers = rows[0]
    field_map = load_field_map(args.field_map)
    missing = [field for field in RECORD_FIELDS if field not in field_map]
    if missing:
        raise SystemExit(f"Field map is missing required fields: {', '.join(missing)}")

    ensure_columns(headers, rows, INTERNAL_COLUMNS + RESULT_COLUMNS)
    _, data_rows = rows_to_dicts(rows)
    for offset, row_dict in enumerate(data_rows, 1):
        record = normalized_record(row_dict, field_map)
        update_row(
            rows[offset],
            headers,
            {
                "__ic_record_id": record["id"],
                "__ic_problem_overview": record["problem_overview"],
                "__ic_probelm_details": record["probelm_details"],
                "__ic_solution_details": record["solution_details"],
                "__ic_user_solution": record["user_solution"],
                "status": row_dict.get("status") or "pending",
                "needs_review": row_dict.get("needs_review") or "",
            },
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    if args.out != args.input:
        with zipfile.ZipFile(args.input) as zf:
            with zipfile.ZipFile(args.out, "w", compression=zipfile.ZIP_DEFLATED) as out:
                for name in zf.namelist():
                    out.writestr(name, zf.read(name))
    write_xlsx_matrix(args.out, rows)
    print(f"Initialized Excel workfile {args.out}")


def record_from_workfile(row: Mapping[str, str]) -> Dict[str, str]:
    record = {
        "id": row.get("__ic_record_id", "").strip(),
        "problem_overview": row.get("__ic_problem_overview", "").strip(),
        "probelm_details": row.get("__ic_probelm_details", "").strip(),
        "solution_details": row.get("__ic_solution_details", "").strip(),
        "user_solution": row.get("__ic_user_solution", "").strip(),
    }
    if not record["user_solution"]:
        record["user_solution"] = "\n".join(
            value for value in [record["problem_overview"], record["probelm_details"], record["solution_details"]] if value
        )
    return record


def command_next_batch(args: argparse.Namespace) -> None:
    matrix = read_xlsx_matrix(args.workbook)
    headers, rows = rows_to_dicts(matrix)
    if "__ic_record_id" not in headers:
        raise SystemExit("Workbook is not initialized. Run excel_workfile.py init first.")

    fresh_items = []
    retry_items = []
    for index, row in enumerate(rows):
        status = normalize_name(row.get("status", "")).lower()
        if status in SUCCESS_STATUSES:
            continue
        item = {
            "row_number": int(row["__row_number"]),
            "record_index": index,
            "record": record_from_workfile(row),
            "previous_status": status or "pending",
            "previous_error": row.get("error_message", ""),
        }
        if status in RETRY_STATUSES:
            retry_items.append(item)
        else:
            fresh_items.append(item)

    items = (fresh_items + retry_items)[: args.batch_size]
    write_json(args.out, {"items": items})
    print(f"Wrote {len(items)} pending/retry records to {args.out}")


def result_rows(payload: Any) -> List[Mapping[str, Any]]:
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return payload["results"]
    if isinstance(payload, list):
        return payload
    raise SystemExit("Results JSON must be a list or an object with a results array")


def derived_status(row: Mapping[str, Any], review_threshold: float) -> Dict[str, Any]:
    selected_l1 = normalize_name(row.get("selected_level_1"))
    selected_l2 = normalize_name(row.get("selected_level_2"))
    if not selected_l1 and not selected_l2:
        return {"status": "retry", "needs_review": "true"}
    confidence = float(row.get("confidence", 0.0))
    if confidence < review_threshold:
        return {"status": "low_confidence", "needs_review": "true"}
    return {"status": "classified", "needs_review": "false"}


def command_apply_results(args: argparse.Namespace) -> None:
    matrix = [list(row) for row in read_xlsx_matrix(args.workbook)]
    if not matrix:
        raise SystemExit(f"Excel file has no rows: {args.workbook}")
    headers = matrix[0]
    ensure_columns(headers, matrix, RESULT_COLUMNS)
    classification_options = load_classification_options(args.classification_options)
    payload = read_json(args.results)

    row_by_number = {number: row for number, row in enumerate(matrix, 1)}
    applied = 0
    for result in result_rows(payload):
        try:
            row_number = int(result.get("row_number", result.get("record_index", -1)))
        except (TypeError, ValueError):
            raise SystemExit(f"Result row is missing valid row_number: {result}") from None
        if row_number not in row_by_number or row_number == 1:
            raise SystemExit(f"Result row_number not found in workbook data rows: {row_number}")

        target = row_by_number[row_number]
        try:
            validated = validate_taxonomy_selection(result, classification_options)
            status = derived_status(validated, args.review_threshold)
            confidence = validated["confidence"]
            if confidence == int(confidence):
                confidence_text = str(int(confidence))
            else:
                confidence_text = str(confidence)
            update_row(
                target,
                headers,
                {
                    "selected_level_1": validated.get("selected_level_1", ""),
                    "selected_level_2": validated.get("selected_level_2", ""),
                    "confidence": confidence_text,
                    "status": status["status"],
                    "needs_review": status["needs_review"],
                    "mapping_justification": validated.get("mapping_justification", ""),
                    "topk_candidates": json.dumps(validated.get("topk_candidates", []), ensure_ascii=False)
                    if not isinstance(validated.get("topk_candidates", ""), str)
                    else validated.get("topk_candidates", ""),
                    "error_message": "",
                    "processed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            )
        except ContractError as exc:
            update_row(
                target,
                headers,
                {
                    "selected_level_1": normalize_name(result.get("selected_level_1")),
                    "selected_level_2": normalize_name(result.get("selected_level_2")),
                    "confidence": str(result.get("confidence", "")),
                    "status": "retry",
                    "needs_review": "true",
                    "mapping_justification": result.get("mapping_justification", ""),
                    "error_message": str(exc),
                    "processed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            )
        applied += 1

    write_xlsx_matrix(args.workbook, matrix)
    print(f"Applied {applied} results to {args.workbook}")


def command_status(args: argparse.Namespace) -> None:
    matrix = read_xlsx_matrix(args.workbook)
    _, rows = rows_to_dicts(matrix)
    counts: Dict[str, int] = {
        "pending": 0,
        "retry": 0,
        "classified": 0,
        "low_confidence": 0,
        "unresolved": 0,
        "failed": 0,
    }
    for row in rows:
        status = normalize_name(row.get("status", "")).lower() or "pending"
        counts[status] = counts.get(status, 0) + 1
    successful = sum(counts.get(status, 0) for status in SUCCESS_STATUSES)
    remaining = len(rows) - successful
    print(
        json.dumps(
            {
                "total": len(rows),
                "complete": remaining == 0,
                "remaining": remaining,
                "successful": successful,
                "success_statuses": sorted(SUCCESS_STATUSES),
                "counts": counts,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Maintain a no-API Excel classification workfile")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect-input", help="Infer Excel column mapping")
    inspect_parser.add_argument("--input", type=Path, required=True)
    inspect_parser.add_argument("--out", type=Path, required=True)
    inspect_parser.add_argument("--sample-size", type=int, default=3)
    inspect_parser.set_defaults(func=command_inspect_input)

    init_parser = subparsers.add_parser("init", help="Create an Excel classification workfile")
    init_parser.add_argument("--input", type=Path, required=True)
    init_parser.add_argument("--out", type=Path, required=True)
    init_parser.add_argument("--field-map", type=Path, required=True)
    init_parser.set_defaults(func=command_init)

    next_parser = subparsers.add_parser("next-batch", help="Export the next pending batch for the current model")
    next_parser.add_argument("--workbook", type=Path, required=True)
    next_parser.add_argument("--batch-size", type=int, default=10)
    next_parser.add_argument("--out", type=Path, required=True)
    next_parser.set_defaults(func=command_next_batch)

    apply_parser = subparsers.add_parser("apply-results", help="Write model classification results back to Excel")
    apply_parser.add_argument("--workbook", type=Path, required=True)
    apply_parser.add_argument("--results", type=Path, required=True)
    apply_parser.add_argument("--classification-options", type=Path, default=DEFAULT_CLASSIFICATION_OPTIONS)
    apply_parser.add_argument("--review-threshold", type=float, default=0.72)
    apply_parser.set_defaults(func=command_apply_results)

    status_parser = subparsers.add_parser("status", help="Summarize workfile progress")
    status_parser.add_argument("--workbook", type=Path, required=True)
    status_parser.set_defaults(func=command_status)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ExcelWorkfileError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
