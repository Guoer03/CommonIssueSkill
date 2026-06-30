#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
RUNNER = SKILL_DIR / "scripts" / "run_pipeline.py"
sys.path.insert(0, str(SKILL_DIR / "scripts"))

import run_pipeline  # noqa: E402
from run_pipeline import (  # noqa: E402
    DEFAULT_CLASSIFICATION_OPTIONS,
    RunnerError,
    build_record_payload,
    load_record_payload,
    run_with_retries,
)


class RunPipelineTest(unittest.TestCase):
    def write_minimal_xlsx(self, path: Path, rows: list[list[str]]) -> None:
        shared_strings: list[str] = []
        shared_index: dict[str, int] = {}

        def shared(value: str) -> int:
            if value not in shared_index:
                shared_index[value] = len(shared_strings)
                shared_strings.append(value)
            return shared_index[value]

        def cell_ref(row_index: int, column_index: int) -> str:
            letters = ""
            number = column_index
            while number:
                number, remainder = divmod(number - 1, 26)
                letters = chr(65 + remainder) + letters
            return f"{letters}{row_index}"

        sheet_rows = []
        for row_index, row in enumerate(rows, 1):
            cells = []
            for column_index, value in enumerate(row, 1):
                cells.append(
                    f'<c r="{cell_ref(row_index, column_index)}" t="s"><v>{shared(value)}</v></c>'
                )
            sheet_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')

        shared_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            f'count="{len(shared_strings)}" uniqueCount="{len(shared_strings)}">'
            + "".join(f"<si><t>{value}</t></si>" for value in shared_strings)
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

    def test_topk_prompt_is_assembled_from_principles_and_io_contract(self) -> None:
        prompt = run_pipeline.build_topk_prompt({"records": [], "classification_options": {}, "rag_results": {}})

        self.assertLess(prompt.index("## 角色与判断原则"), prompt.index("## 强制输入输出契约"))
        self.assertLess(prompt.index("## 强制输入输出契约"), prompt.index("## 当前输入"))
        self.assertIn("records", prompt)
        self.assertNotIn("本文件不放", prompt)
        self.assertNotIn("不要写在这里", prompt)
        self.assertNotIn("用于放置", prompt)
        self.assertTrue((SKILL_DIR / "references" / "topk_prompt.md").exists())
        self.assertTrue((SKILL_DIR / "references" / "topk_io_contract.md").exists())

    def test_final_prompt_is_assembled_from_principles_and_io_contract(self) -> None:
        prompt = run_pipeline.build_final_prompt({"items": []})

        self.assertLess(prompt.index("## 角色与判断原则"), prompt.index("## 强制输入输出契约"))
        self.assertLess(prompt.index("## 强制输入输出契约"), prompt.index("## 当前输入"))
        self.assertIn("candidate_pool", prompt)
        self.assertNotIn("本文件不放", prompt)
        self.assertNotIn("不要写在这里", prompt)
        self.assertNotIn("用于放置", prompt)
        self.assertTrue((SKILL_DIR / "references" / "final_prompt.md").exists())
        self.assertTrue((SKILL_DIR / "references" / "final_io_contract.md").exists())

    def test_candidate_pool_for_final_removes_topk_confidence(self) -> None:
        self.assertTrue(hasattr(run_pipeline, "candidate_pool_for_final"))
        candidate_pool = run_pipeline.candidate_pool_for_final(
            [
                {
                    "level_1": "操作维护类失效",
                    "level_2": "系统升级/安装失效",
                    "confidence": 0.93,
                    "inline_features": ["安装包校验失败"],
                }
            ]
        )

        self.assertEqual(
            candidate_pool,
            [
                {
                    "level_1": "操作维护类失效",
                    "level_2": "系统升级/安装失效",
                    "inline_features": ["安装包校验失败"],
                }
            ],
        )
        self.assertNotIn("confidence", candidate_pool[0])

    def test_build_record_payload_uses_structured_record_schema(self) -> None:
        payload = build_record_payload(
            {
                "问题概述": "升级失败",
                "problem_details": "安装包校验失败",
                "solution_details": "回退版本",
            },
            record_column=None,
            record_id="rec_001",
        )

        self.assertEqual(payload["id"], "rec_001")
        self.assertEqual(payload["problem_overview"], "升级失败")
        self.assertEqual(payload["probelm_details"], "安装包校验失败")
        self.assertEqual(payload["solution_details"], "回退版本")
        self.assertIn("problem_overview: 升级失败", payload["user_solution"])
        self.assertIn("probelm_details: 安装包校验失败", payload["user_solution"])
        self.assertIn("solution_details: 回退版本", payload["user_solution"])

    def test_load_record_payload_wraps_legacy_plain_text(self) -> None:
        payload = load_record_payload("问题概述：升级失败")

        self.assertEqual(payload["id"], "")
        self.assertEqual(payload["problem_overview"], "")
        self.assertEqual(payload["probelm_details"], "")
        self.assertEqual(payload["solution_details"], "")
        self.assertEqual(payload["user_solution"], "问题概述：升级失败")

    def test_reference_inputs_are_packaged_with_the_skill(self) -> None:
        self.assertEqual(DEFAULT_CLASSIFICATION_OPTIONS, SKILL_DIR / "references" / "classification_options.json")
        self.assertTrue(DEFAULT_CLASSIFICATION_OPTIONS.exists())
        self.assertTrue((SKILL_DIR / "references" / "topk_prompt.md").exists())
        self.assertTrue((SKILL_DIR / "references" / "final_prompt.md").exists())

    def test_run_with_retries_retries_transient_batch_failures(self) -> None:
        calls = 0

        def flaky() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise RunnerError("temporary failure")
            return "ok"

        self.assertEqual(run_with_retries("test", 2, flaky, sleep_seconds=0), "ok")
        self.assertEqual(calls, 3)

    def test_python_runtime_config_fills_model_and_api_args(self) -> None:
        original = dict(run_pipeline.PYTHON_RUNTIME_CONFIG)
        try:
            run_pipeline.PYTHON_RUNTIME_CONFIG.update(
                {
                    "base_url": "http://inner-gateway/v1",
                    "model": "qwen-classifier",
                    "api_key_env": "INNER_MODEL_KEY",
                    "batch_size": 32,
                    "limit": 800,
                    "timeout": 120,
                    "temperature": 0.1,
                    "max_tokens": 8192,
                    "topk_rag_k": 4,
                    "final_rag_k": 7,
                    "review_threshold": 0.8,
                    "max_retries": 4,
                    "continue_on_failure": True,
                }
            )
            args = argparse.Namespace(
                command="final",
                base_url=None,
                model=None,
                api_key_env=None,
                batch_size=None,
                limit=None,
                timeout=None,
                temperature=None,
                max_tokens=None,
                topk_rag_k=None,
                final_rag_k=None,
                review_threshold=None,
                max_retries=None,
                continue_on_failure=None,
            )

            resolved = run_pipeline.resolve_runtime_config(args, env={})

            self.assertEqual(resolved.base_url, "http://inner-gateway/v1")
            self.assertEqual(resolved.model, "qwen-classifier")
            self.assertEqual(resolved.api_key_env, "INNER_MODEL_KEY")
            self.assertEqual(resolved.batch_size, 32)
            self.assertEqual(resolved.limit, 800)
            self.assertEqual(resolved.timeout, 120.0)
            self.assertEqual(resolved.temperature, 0.1)
            self.assertEqual(resolved.max_tokens, 8192)
            self.assertEqual(resolved.topk_rag_k, 4)
            self.assertEqual(resolved.final_rag_k, 7)
            self.assertEqual(resolved.review_threshold, 0.8)
            self.assertEqual(resolved.max_retries, 4)
            self.assertTrue(resolved.continue_on_failure)
        finally:
            run_pipeline.PYTHON_RUNTIME_CONFIG.clear()
            run_pipeline.PYTHON_RUNTIME_CONFIG.update(original)

    def test_cli_runtime_args_override_python_runtime_config(self) -> None:
        original = dict(run_pipeline.PYTHON_RUNTIME_CONFIG)
        try:
            run_pipeline.PYTHON_RUNTIME_CONFIG.update(
                {
                    "base_url": "http://python-gateway/v1",
                    "model": "python-model",
                    "batch_size": 20,
                    "continue_on_failure": False,
                }
            )
            args = argparse.Namespace(
                command="topk",
                base_url=None,
                model="cli-model",
                api_key_env=None,
                batch_size=64,
                limit=None,
                timeout=None,
                temperature=None,
                max_tokens=None,
                topk_rag_k=None,
                max_retries=None,
                continue_on_failure=True,
            )

            resolved = run_pipeline.resolve_runtime_config(args, env={})

            self.assertEqual(resolved.base_url, "http://python-gateway/v1")
            self.assertEqual(resolved.model, "cli-model")
            self.assertEqual(resolved.batch_size, 64)
            self.assertTrue(resolved.continue_on_failure)
            self.assertEqual(resolved.api_key_env, "ISSUE_CLASSIFIER_API_KEY")
        finally:
            run_pipeline.PYTHON_RUNTIME_CONFIG.clear()
            run_pipeline.PYTHON_RUNTIME_CONFIG.update(original)

    def test_init_creates_stateful_workspace_without_writing_into_skill_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_csv = root / "records.csv"
            options_json = root / "classification_options.json"
            workdir = root / "run_workspace"

            input_csv.write_text(
                "问题概述,问题明细,问题根因,解决方案\n"
                "升级失败,安装包校验失败,系统升级后安装包校验失败,回退版本\n"
                "监控异常,跟踪任务无数据,监控链路异常,重启跟踪任务\n",
                encoding="utf-8",
            )
            options_json.write_text(
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

            result = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "init",
                    "--input",
                    str(input_csv),
                    "--classification-options",
                    str(options_json),
                    "--workdir",
                    str(workdir),
                    "--batch-size",
                    "25",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((workdir / "state.sqlite").exists())
            self.assertTrue((workdir / "classification_options.json").exists())
            self.assertTrue((workdir / "manifest.json").exists())

            with sqlite3.connect(workdir / "state.sqlite") as conn:
                count = conn.execute("select count(*) from records").fetchone()[0]
                text = conn.execute("select record_text from records order by record_index limit 1").fetchone()[0]
                payload = json.loads(text)

            self.assertEqual(count, 2)
            self.assertEqual(payload["id"], "0")
            self.assertEqual(payload["problem_overview"], "升级失败")
            self.assertEqual(payload["probelm_details"], "安装包校验失败")
            self.assertEqual(payload["solution_details"], "回退版本")
            self.assertIn("user_solution", payload)

    def test_inspect_input_infers_excel_field_map_for_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_xlsx = root / "records.xlsx"
            report_json = root / "field_map_review.json"

            self.write_minimal_xlsx(
                input_xlsx,
                [
                    ["工单编号", "故障标题", "故障现象", "处理方案"],
                    ["case-001", "升级失败", "安装包校验失败", "回退版本"],
                    ["case-002", "监控异常", "跟踪任务无数据", "重启跟踪任务"],
                ],
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "inspect-input",
                    "--input",
                    str(input_xlsx),
                    "--out",
                    str(report_json),
                    "--sample-size",
                    "1",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(report_json.exists())
            report = json.loads(report_json.read_text(encoding="utf-8"))

            self.assertTrue(report["confirmation_required"])
            self.assertEqual(
                report["field_map"],
                {
                    "id": "工单编号",
                    "problem_overview": "故障标题",
                    "probelm_details": "故障现象",
                    "solution_details": "处理方案",
                },
            )
            self.assertEqual(report["sample_records"][0]["id"], "case-001")
            self.assertEqual(report["sample_records"][0]["problem_overview"], "升级失败")
            self.assertEqual(report["sample_records"][0]["probelm_details"], "安装包校验失败")
            self.assertEqual(report["sample_records"][0]["solution_details"], "回退版本")

    def test_excel_init_requires_confirmed_field_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_xlsx = root / "records.xlsx"
            options_json = root / "classification_options.json"
            workdir = root / "run_workspace"

            self.write_minimal_xlsx(
                input_xlsx,
                [
                    ["record_id", "problem_overview", "probelm_details", "solution_details"],
                    ["case-001", "升级失败", "安装包校验失败", "回退版本"],
                ],
            )
            options_json.write_text(
                json.dumps({"操作维护类失效": {"系统升级/安装失效": ["安装包校验失败"]}}, ensure_ascii=False),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "init",
                    "--input",
                    str(input_xlsx),
                    "--classification-options",
                    str(options_json),
                    "--workdir",
                    str(workdir),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("inspect-input", result.stderr)
            self.assertFalse((workdir / "state.sqlite").exists())

    def test_init_reads_xlsx_and_normalizes_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_xlsx = root / "records.xlsx"
            options_json = root / "classification_options.json"
            field_map_json = root / "field_map_review.json"
            workdir = root / "run_workspace"

            self.write_minimal_xlsx(
                input_xlsx,
                [
                    ["record_id", "problem_overview", "probelm_details", "solution_details"],
                    ["case-001", "升级失败", "安装包校验失败", "回退版本"],
                    ["case-002", "监控异常", "跟踪任务无数据", "重启跟踪任务"],
                ],
            )
            options_json.write_text(
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
            field_map_json.write_text(
                json.dumps(
                    {
                        "field_map": {
                            "id": "record_id",
                            "problem_overview": "problem_overview",
                            "probelm_details": "probelm_details",
                            "solution_details": "solution_details",
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "init",
                    "--input",
                    str(input_xlsx),
                    "--classification-options",
                    str(options_json),
                    "--workdir",
                    str(workdir),
                    "--field-map",
                    str(field_map_json),
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            with sqlite3.connect(workdir / "state.sqlite") as conn:
                count = conn.execute("select count(*) from records").fetchone()[0]
                record_id, text = conn.execute(
                    "select record_id, record_text from records order by record_index limit 1"
                ).fetchone()
                payload = json.loads(text)

            self.assertEqual(count, 2)
            self.assertEqual(record_id, "case-001")
            self.assertEqual(payload["id"], "case-001")
            self.assertEqual(payload["problem_overview"], "升级失败")
            self.assertEqual(payload["probelm_details"], "安装包校验失败")
            self.assertEqual(payload["solution_details"], "回退版本")


if __name__ == "__main__":
    unittest.main()
