#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
RUNNER = SKILL_DIR / "scripts" / "run_pipeline.py"
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from run_pipeline import DEFAULT_CLASSIFICATION_OPTIONS, RunnerError, run_with_retries  # noqa: E402


class RunPipelineTest(unittest.TestCase):
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

            self.assertEqual(count, 2)
            self.assertIn("问题根因：系统升级后安装包校验失败", text)


if __name__ == "__main__":
    unittest.main()
