# Final Prompt Contract

用于在 TopK 候选池、内联 level3 特征和 RAG 证据的约束下，选择最终唯一一级/二级分类。

## 输入

输入为 JSON 对象：

```json
{
  "items": [
    {
      "record_index": 0,
      "record": "问题概述：...\n问题明细：...\n问题根因：...\n解决方案：...",
      "candidate_pool": [
        {
          "level_1": "候选一级分类",
          "level_2": "候选二级分类",
          "confidence": 0.91,
          "inline_features": ["该二级分类下的 level3 补充信息"]
        }
      ],
      "rag_results": [
        {
          "case_id": "case_001",
          "record": "历史相似问题",
          "level_1": "历史一级分类",
          "level_2": "历史二级分类",
          "similarity": 0.91,
          "out_of_candidate_pool_reference": false
        }
      ]
    }
  ]
}
```

规则：

- 只能从 `candidate_pool` 中选择最终 `level_1/level_2`。
- `inline_features` 是该二级分类下全部 level3 补充信息，只能作为判断证据，不允许作为最终分类输出。
- `rag_results` 只能作为证据，不能引入候选池外分类。
- `out_of_candidate_pool_reference=true` 的 RAG 样例只可参考表达方式，不可决定分类。
- 如果所有候选都不匹配，可以输出空的 `selected_level_1` 和 `selected_level_2`，并将 `confidence` 设为 `0`。

## 输出

只输出 XML。批量输出必须使用 `<results>` 包装：

```xml
<results>
  <result>
    <record_index>0</record_index>
    <thinking_process>简短说明判定过程。</thinking_process>
    <selected_level_1>最终确定的一级分类名称</selected_level_1>
    <selected_level_2>最终确定的二级分类名称</selected_level_2>
    <mapping_justification>必须详细阐述匹配逻辑，包括原始问题证据、候选匹配点、RAG 参考点和排除其他候选的理由。</mapping_justification>
    <confidence>0.86</confidence>
  </result>
</results>
```

约束：

- `record_index` 必须对应输入 item 的 `record_index`。
- `selected_level_1` 和 `selected_level_2` 必须来自该 item 的 `candidate_pool`，除非无法判断时二者都为空。
- `mapping_justification` 必须详细阐述匹配逻辑。
- `confidence` 范围为 0 到 1，不要写百分号。
- 不要在 XML 外输出解释文本。
