#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.sax.saxutils import escape


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT = SKILL_DIR / "scripts" / "excel_workfile.py"
NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def cell_ref(row_index: int, column_index: int) -> str:
    letters = ""
    number = column_index
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return f"{letters}{row_index}"


def write_minimal_xlsx(path: Path, rows: list[list[str]]) -> None:
    shared_strings: list[str] = []
    shared_index: dict[str, int] = {}

    def shared(value: str) -> int:
        if value not in shared_index:
            shared_index[value] = len(shared_strings)
            shared_strings.append(value)
        return shared_index[value]

    sheet_rows = []
    for row_index, row in enumerate(rows, 1):
        cells = []
        for column_index, value in enumerate(row, 1):
            cells.append(f'<c r="{cell_ref(row_index, column_index)}" t="s"><v>{shared(value)}</v></c>')
        sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')

    shared_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        f'count="{len(shared_strings)}" uniqueCount="{len(shared_strings)}">'
        + "".join(f"<si><t>{escape(value)}</t></si>" for value in shared_strings)
        + "</sst>"
    )
    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(sheet_rows)}</sheetData></worksheet>"
    )

    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
            "</Types>",
        )
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        zf.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            "</Relationships>",
        )
        zf.writestr("xl/sharedStrings.xml", shared_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)


def read_xlsx_rows(path: Path) -> list[dict[str, str]]:
    with zipfile.ZipFile(path) as zf:
        shared_strings = []
        shared_xml = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        for item in shared_xml.findall(f"{NS}si"):
            shared_strings.append("".join(text.text or "" for text in item.iter(f"{NS}t")))
        sheet = ET.fromstring(zf.read("xl/worksheets/sheet1.xml"))

    rows: list[list[str]] = []
    for row in sheet.find(f"{NS}sheetData") or []:
        values: dict[int, str] = {}
        for cell in row.findall(f"{NS}c"):
            ref = cell.attrib.get("r", "A1")
            col = 0
            for char in ref:
                if char.isalpha():
                    col = col * 26 + ord(char.upper()) - ord("A") + 1
            raw = cell.find(f"{NS}v")
            inline_text = cell.find(f"{NS}is/{NS}t")
            if cell.attrib.get("t") == "s" and raw is not None:
                value = shared_strings[int(raw.text or "0")]
            elif inline_text is not None:
                value = inline_text.text or ""
            elif raw is not None:
                value = raw.text or ""
            else:
                value = ""
            values[col - 1] = value
        rows.append([values.get(index, "") for index in range(max(values.keys(), default=-1) + 1)])
    if not rows:
        return []
    headers = rows[0]
    return [{headers[index]: row[index] if index < len(row) else "" for index in range(len(headers))} for row in rows[1:]]


class ExcelWorkfileTest(unittest.TestCase):
    def make_inputs(self, root: Path) -> tuple[Path, Path, Path]:
        input_xlsx = root / "records.xlsx"
        field_map = root / "field_map.json"
        options = root / "classification_options.json"
        write_minimal_xlsx(
            input_xlsx,
            [
                ["工单编号", "故障标题", "故障现象", "处理方案"],
                ["case-001", "升级失败", "安装包校验失败", "回退版本"],
                ["case-002", "监控异常", "跟踪任务无数据", "重启跟踪任务"],
            ],
        )
        field_map.write_text(
            json.dumps(
                {
                    "field_map": {
                        "id": "工单编号",
                        "problem_overview": "故障标题",
                        "probelm_details": "故障现象",
                        "solution_details": "处理方案",
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        options.write_text(
            json.dumps(
                {
                    "操作维护类失效": {
                        "系统升级/安装失效": ["安装包校验失败"],
                        "监控/跟踪失效": ["跟踪任务失败"],
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return input_xlsx, field_map, options

    def test_inspect_input_writes_confirmable_field_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_xlsx, _, _ = self.make_inputs(root)
            report = root / "field_map_review.json"

            result = subprocess.run(
                [sys.executable, str(SCRIPT), "inspect-input", "--input", str(input_xlsx), "--out", str(report)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertTrue(payload["confirmation_required"])
            self.assertEqual(payload["field_map"]["id"], "工单编号")
            self.assertEqual(payload["sample_records"][0]["id"], "case-001")

    def test_init_copies_workbook_and_appends_result_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_xlsx, field_map, _ = self.make_inputs(root)
            workfile = root / "records.classifying.xlsx"

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "init",
                    "--input",
                    str(input_xlsx),
                    "--out",
                    str(workfile),
                    "--field-map",
                    str(field_map),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            rows = read_xlsx_rows(workfile)
            self.assertEqual(rows[0]["__ic_record_id"], "case-001")
            self.assertEqual(rows[0]["__ic_user_solution"], "升级失败\n安装包校验失败\n回退版本")
            self.assertEqual(rows[0]["status"], "pending")
            self.assertIn("mapping_justification", rows[0])

    def test_next_batch_reads_only_pending_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_xlsx, field_map, options = self.make_inputs(root)
            workfile = root / "records.classifying.xlsx"
            batch = root / "batch.json"

            subprocess.run(
                [sys.executable, str(SCRIPT), "init", "--input", str(input_xlsx), "--out", str(workfile), "--field-map", str(field_map)],
                check=True,
            )
            results = root / "results.json"
            results.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "row_number": 2,
                                "selected_level_1": "操作维护类失效",
                                "selected_level_2": "系统升级/安装失效",
                                "confidence": 0.91,
                                "mapping_justification": "根因匹配安装包校验失败。",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "apply-results",
                    "--workbook",
                    str(workfile),
                    "--results",
                    str(results),
                    "--classification-options",
                    str(options),
                ],
                check=True,
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "next-batch",
                    "--workbook",
                    str(workfile),
                    "--batch-size",
                    "10",
                    "--out",
                    str(batch),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(batch.read_text(encoding="utf-8"))
            self.assertEqual([item["row_number"] for item in payload["items"]], [3])
            self.assertEqual(payload["items"][0]["record"]["id"], "case-002")

    def test_next_batch_default_size_is_ten_to_limit_context_growth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_xlsx = root / "records.xlsx"
            field_map = root / "field_map.json"
            workfile = root / "records.classifying.xlsx"
            batch = root / "batch.json"
            rows = [["工单编号", "故障标题", "故障现象", "处理方案"]]
            for index in range(12):
                rows.append([f"case-{index:03d}", "监控异常", f"第{index}条跟踪任务无数据", "重启跟踪任务"])
            write_minimal_xlsx(input_xlsx, rows)
            field_map.write_text(
                json.dumps(
                    {
                        "field_map": {
                            "id": "工单编号",
                            "problem_overview": "故障标题",
                            "probelm_details": "故障现象",
                            "solution_details": "处理方案",
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "init",
                    "--input",
                    str(input_xlsx),
                    "--out",
                    str(workfile),
                    "--field-map",
                    str(field_map),
                ],
                check=True,
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "next-batch",
                    "--workbook",
                    str(workfile),
                    "--out",
                    str(batch),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(batch.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["items"]), 10)
            self.assertEqual(payload["items"][0]["row_number"], 2)
            self.assertEqual(payload["items"][-1]["row_number"], 11)

    def test_apply_results_writes_valid_selection_and_derived_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_xlsx, field_map, options = self.make_inputs(root)
            workfile = root / "records.classifying.xlsx"
            results = root / "results.json"

            subprocess.run(
                [sys.executable, str(SCRIPT), "init", "--input", str(input_xlsx), "--out", str(workfile), "--field-map", str(field_map)],
                check=True,
            )
            results.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "row_number": 2,
                                "record_id": "case-001",
                                "selected_level_1": "操作维护类失效",
                                "selected_level_2": "系统升级／安装失效",
                                "confidence": 0.64,
                                "mapping_justification": "根因提到安装包校验失败，匹配系统升级/安装失效。",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "apply-results",
                    "--workbook",
                    str(workfile),
                    "--results",
                    str(results),
                    "--classification-options",
                    str(options),
                    "--review-threshold",
                    "0.72",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            row = read_xlsx_rows(workfile)[0]
            self.assertEqual(row["selected_level_2"], "系统升级/安装失效")
            self.assertEqual(row["status"], "low_confidence")
            self.assertEqual(row["needs_review"], "true")
            self.assertEqual(row["confidence"], "0.64")

    def test_apply_results_marks_illegal_selection_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_xlsx, field_map, options = self.make_inputs(root)
            workfile = root / "records.classifying.xlsx"
            results = root / "results.json"

            subprocess.run(
                [sys.executable, str(SCRIPT), "init", "--input", str(input_xlsx), "--out", str(workfile), "--field-map", str(field_map)],
                check=True,
            )
            results.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "row_number": 2,
                                "selected_level_1": "不存在一级",
                                "selected_level_2": "不存在二级",
                                "confidence": 0.9,
                                "mapping_justification": "非法分类。",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "apply-results",
                    "--workbook",
                    str(workfile),
                    "--results",
                    str(results),
                    "--classification-options",
                    str(options),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            row = read_xlsx_rows(workfile)[0]
            self.assertEqual(row["status"], "retry")
            self.assertEqual(row["needs_review"], "true")
            self.assertIn("not in classification_options", row["error_message"])

    def test_retry_rows_do_not_block_never_attempted_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_xlsx, field_map, options = self.make_inputs(root)
            workfile = root / "records.classifying.xlsx"
            bad_results = root / "bad_results.json"
            good_results = root / "good_results.json"
            batch = root / "batch.json"

            subprocess.run(
                [sys.executable, str(SCRIPT), "init", "--input", str(input_xlsx), "--out", str(workfile), "--field-map", str(field_map)],
                check=True,
            )
            bad_results.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "row_number": 2,
                                "selected_level_1": "不存在一级",
                                "selected_level_2": "不存在二级",
                                "confidence": 0.9,
                                "mapping_justification": "非法分类。",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "apply-results",
                    "--workbook",
                    str(workfile),
                    "--results",
                    str(bad_results),
                    "--classification-options",
                    str(options),
                ],
                check=True,
            )

            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "next-batch",
                    "--workbook",
                    str(workfile),
                    "--batch-size",
                    "1",
                    "--out",
                    str(batch),
                ],
                check=True,
            )
            payload = json.loads(batch.read_text(encoding="utf-8"))
            self.assertEqual([item["row_number"] for item in payload["items"]], [3])

            good_results.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "row_number": 3,
                                "selected_level_1": "操作维护类失效",
                                "selected_level_2": "监控/跟踪失效",
                                "confidence": 0.88,
                                "mapping_justification": "问题明细提到跟踪任务无数据，匹配监控/跟踪失效。",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "apply-results",
                    "--workbook",
                    str(workfile),
                    "--results",
                    str(good_results),
                    "--classification-options",
                    str(options),
                ],
                check=True,
            )

            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "next-batch",
                    "--workbook",
                    str(workfile),
                    "--batch-size",
                    "1",
                    "--out",
                    str(batch),
                ],
                check=True,
            )
            payload = json.loads(batch.read_text(encoding="utf-8"))
            self.assertEqual([item["row_number"] for item in payload["items"]], [2])

    def test_status_reports_incomplete_until_every_row_has_valid_classification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_xlsx, field_map, options = self.make_inputs(root)
            workfile = root / "records.classifying.xlsx"
            results = root / "results.json"

            subprocess.run(
                [sys.executable, str(SCRIPT), "init", "--input", str(input_xlsx), "--out", str(workfile), "--field-map", str(field_map)],
                check=True,
            )
            results.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "row_number": 2,
                                "selected_level_1": "不存在一级",
                                "selected_level_2": "不存在二级",
                                "confidence": 0.9,
                                "mapping_justification": "非法分类。",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "apply-results",
                    "--workbook",
                    str(workfile),
                    "--results",
                    str(results),
                    "--classification-options",
                    str(options),
                ],
                check=True,
            )

            result = subprocess.run(
                [sys.executable, str(SCRIPT), "status", "--workbook", str(workfile)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertFalse(payload["complete"])
            self.assertEqual(payload["remaining"], 2)
            self.assertEqual(payload["counts"]["retry"], 1)


if __name__ == "__main__":
    unittest.main()
