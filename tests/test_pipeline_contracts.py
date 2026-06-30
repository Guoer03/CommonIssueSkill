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
    parse_final_xml,
    parse_topk_xml,
    select_rag_for_stages,
    validate_final_selection,
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
    def test_topk_xml_is_parsed_validated_and_enriched_with_level3_features(self) -> None:
        xml = """
        <results>
          <result>
            <record_index>0</record_index>
            <thinking_process>命中升级失败和安装异常。</thinking_process>
            <candidates>
              <candidate>
                <level_1> 操作维护类失效 </level_1>
                <level_2>系统升级／安装失效</level_2>
                <confidence>0.91</confidence>
              </candidate>
              <candidate>
                <level_1>操作维护类失效</level_1>
                <level_2>监控 / 跟踪失效</level_2>
                <confidence>0.51</confidence>
              </candidate>
            </candidates>
          </result>
        </results>
        """

        parsed = parse_topk_xml(xml)
        candidate_pool = enrich_candidate_pool(parsed[0]["candidates"], CLASSIFICATION_OPTIONS)

        self.assertEqual(parsed[0]["record_index"], 0)
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
        xml = """
        <results>
          <result>
            <record_index>0</record_index>
            <thinking_process>候选与根因部分匹配。</thinking_process>
            <selected_level_1>操作维护类失效</selected_level_1>
            <selected_level_2>系统升级／安装失效</selected_level_2>
            <mapping_justification>根因提到升级后安装包校验失败，与候选分类的 level3 特征匹配。</mapping_justification>
            <confidence>0.61</confidence>
          </result>
        </results>
        """

        final_rows = parse_final_xml(xml)
        validated = validate_final_selection(final_rows[0], candidate_pool)
        status = derive_final_status(candidate_pool, validated, review_threshold=0.72)

        self.assertEqual(validated["selected_level_2"], "系统升级/安装失效")
        self.assertEqual(status["status"], "low_confidence")
        self.assertTrue(status["needs_review"])

        bad_xml = """
        <result>
          <record_index>0</record_index>
          <selected_level_1>网络资源类失效</selected_level_1>
          <selected_level_2>链路中断</selected_level_2>
          <mapping_justification>错误地跳出了候选池。</mapping_justification>
          <confidence>0.9</confidence>
        </result>
        """
        with self.assertRaisesRegex(ContractError, "not in candidate pool"):
            validate_final_selection(parse_final_xml(bad_xml)[0], candidate_pool)

    def test_rag_pool_is_reused_for_topk_and_candidate_filtered_final(self) -> None:
        rag_pool = [
            {
                "case_id": "a",
                "level_1": "操作维护类失效",
                "level_2": "系统升级/安装失效",
                "similarity": 0.94,
            },
            {
                "case_id": "b",
                "level_1": "网络资源类失效",
                "level_2": "链路中断",
                "similarity": 0.92,
            },
            {
                "case_id": "c",
                "level_1": "操作维护类失效",
                "level_2": "监控/跟踪失效",
                "similarity": 0.9,
            },
            {
                "case_id": "d",
                "level_1": "操作维护类失效",
                "level_2": "系统升级/安装失效",
                "similarity": 0.88,
            },
        ]
        candidate_pool = [
            {"level_1": "操作维护类失效", "level_2": "系统升级/安装失效"},
        ]

        selected = select_rag_for_stages(rag_pool, candidate_pool, topk_k=3, final_k=3)

        self.assertEqual([item["case_id"] for item in selected["topk_rag"]], ["a", "b", "c"])
        self.assertEqual([item["case_id"] for item in selected["final_rag"]], ["a", "d", "b"])
        self.assertFalse(selected["final_rag"][0]["out_of_candidate_pool_reference"])
        self.assertTrue(selected["final_rag"][2]["out_of_candidate_pool_reference"])


if __name__ == "__main__":
    unittest.main()
