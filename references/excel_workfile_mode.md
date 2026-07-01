# Excel Workfile Mode

## 使用场景

当用户提供一个 Excel 文件，并且不希望配置或调用模型 API 时，使用 Excel Workfile Mode。分类动作由当前运行 skill 的模型完成；`scripts/excel_workfile.py` 只负责确定性文件操作。

## 操作协议

1. 运行 `inspect-input`，展示字段映射和样例，等待用户确认或修改。
2. 运行 `init` 创建 `.classifying.xlsx` 工作副本。
3. 运行 `next-batch` 导出 `current_batch.json`。
4. 当前模型读取当前 batch、分类选项和 prompt references，完成 TopK 候选和 Final 判定。
5. 当前模型把最终结果整理成 `apply-results` 的 JSON schema。
6. 运行 `apply-results` 写回工作副本。
7. 运行 `status` 查看 `complete` 和 `remaining`。只要 `complete=false`，继续下一批。

循环规则：

- 目标是所有记录最终都有合法一级/二级分类。
- `classified` 和 `low_confidence` 算成功分类；低置信只是需要复核，不阻塞循环完成。
- `retry`、`failed`、`unresolved` 都不是完成状态。
- 单条记录输出非法、空分类或格式异常时，写成 `retry` 并继续处理后续记录；下一轮在未尝试行处理完后再回到 retry 行。
- 默认 batch 大小为 10；长文本、分类标签较多或上下文压力大时降到 5。
- 每轮分类只能依据当前 batch、当前轮可用的 RAG 召回数据和分类选项。不要使用历史对话、历史 batch、此前已处理记录或由历史记录总结出的分类经验。

## Batch 输入

`next-batch` 生成：

```json
{
  "items": [
    {
      "row_number": 2,
      "record_index": 0,
      "record": {
        "id": "case-001",
        "problem_overview": "问题概述",
        "probelm_details": "问题明细",
        "solution_details": "解决方案",
        "user_solution": "三字段合并文本"
      }
    }
  ]
}
```

`row_number` 是 Excel 真实行号，写回时必须原样保留。

## Apply Results 输出

当前模型分类后，生成：

```json
{
  "results": [
    {
      "row_number": 2,
      "record_id": "case-001",
      "selected_level_1": "一级分类",
      "selected_level_2": "二级分类",
      "confidence": 0.86,
      "mapping_justification": "详细阐述匹配逻辑。",
      "topk_candidates": [
        {
          "level_1": "候选一级分类",
          "level_2": "候选二级分类",
          "confidence": 0.9
        }
      ]
    }
  ]
}
```

规则：

- `row_number` 必须来自当前 batch。
- `selected_level_1/selected_level_2` 必须来自分类选项；证据不足时选择最接近的合法分类并降低 `confidence`，不要把空分类作为最终结果。
- `mapping_justification` 必须说明原始问题证据、候选匹配点和排除其他候选的理由。
- `topk_candidates` 可选，但建议保留，方便人工复核。
- 每批处理完立即写回，避免对话上下文承担状态。
- 最终交付前必须运行 `status`，确认 `complete=true`。
