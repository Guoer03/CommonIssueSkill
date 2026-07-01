#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from pipeline_core import (  # noqa: E402
    ContractError,
    derive_final_status,
    enrich_candidate_pool,
    validate_final_selection,
    validate_taxonomy_selection,
)


CLASSIFICATION_OPTIONS = {
    "操作维护类失效": {
        "系统升级/安装失效": ["安装包校验失败", "升级后模块异常"],
        "监控/跟踪失效": ["跟踪任务失败", "监控无数据"],
    },
    "网络资源类失效": {
        "链路中断": ["传输链路告警", "端口物理中断"],
    },
}


class PipelineContractTest(unittest.TestCase):
    def test_topk_candidates_are_validated_and_enriched_with_level3_features(self) -> None:
        candidates = [
            {
                "level_1": " 操作维护类失效 ",
                "level_2": "系统升级／安装失效",
                "confidence": 0.91,
            },
            {
                "level_1": "操作维护类失效",
                "level_2": "监控 / 跟踪失效",
                "confidence": 0.51,
            },
        ]

        candidate_pool = enrich_candidate_pool(candidates, CLASSIFICATION_OPTIONS)

        self.assertEqual(candidate_pool[0]["level_1"], "操作维护类失效")
        self.assertEqual(candidate_pool[0]["level_2"], "系统升级/安装失效")
        self.assertEqual(candidate_pool[0]["confidence"], 0.91)
        self.assertEqual(candidate_pool[0]["inline_features"], ["安装包校验失败", "升级后模块异常"])
        self.assertEqual(candidate_pool[1]["level_2"], "监控/跟踪失效")

    def test_final_low_confidence_is_marked_for_review_and_selection_must_be_from_candidates(self) -> None:
        candidate_pool = [
            {
                "level_1": "操作维护类失效",
                "level_2": "系统升级/安装失效",
                "inline_features": ["安装包校验失败"],
            }
        ]
        final_row = {
            "selected_level_1": "操作维护类失效",
            "selected_level_2": "系统升级／安装失效",
            "mapping_justification": "根因提到升级后安装包校验失败，与候选分类的 level3 特征匹配。",
            "confidence": 0.61,
        }

        validated = validate_final_selection(final_row, candidate_pool)
        status = derive_final_status(candidate_pool, validated, review_threshold=0.72)

        self.assertEqual(validated["selected_level_2"], "系统升级/安装失效")
        self.assertEqual(status["status"], "low_confidence")
        self.assertTrue(status["needs_review"])

        with self.assertRaisesRegex(ContractError, "not in candidate pool"):
            validate_final_selection(
                {
                    "selected_level_1": "网络资源类失效",
                    "selected_level_2": "链路中断",
                    "mapping_justification": "错误地跳出了候选池。",
                    "confidence": 0.9,
                },
                candidate_pool,
            )

    def test_final_selection_can_be_validated_directly_against_taxonomy(self) -> None:
        row = {
            "selected_level_1": " 操作维护类失效 ",
            "selected_level_2": "系统升级／安装失效",
            "confidence": "0.88",
        }

        validated = validate_taxonomy_selection(row, CLASSIFICATION_OPTIONS)

        self.assertEqual(validated["selected_level_1"], "操作维护类失效")
        self.assertEqual(validated["selected_level_2"], "系统升级/安装失效")
        self.assertEqual(validated["confidence"], 0.88)

        with self.assertRaisesRegex(ContractError, "not in classification_options"):
            validate_taxonomy_selection(
                {
                    "selected_level_1": "网络资源类失效",
                    "selected_level_2": "不存在二级",
                    "confidence": 0.9,
                },
                CLASSIFICATION_OPTIONS,
            )


if __name__ == "__main__":
    unittest.main()
