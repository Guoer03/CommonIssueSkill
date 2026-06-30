# TopK Prompt Contract

用于从用户提供的分类选项中，为每条问题记录召回候选一级/二级分类。

## 输入

输入为 JSON 对象：

```json
{
  "records": [
    "问题概述：...\n问题明细：...\n问题根因：...\n解决方案：..."
  ],
  "classification_options": {
    "一级分类": {
      "二级分类": ["level3补充信息"]
    }
  },
  "rag_results": {
    "0": [
      {
        "case_id": "case_001",
        "record": "历史相似问题",
        "level_1": "历史一级分类",
        "level_2": "历史二级分类",
        "similarity": 0.91
      }
    ]
  }
}
```

规则：

- `records` 是字符串数组，每个元素是一条记录。
- `classification_options` 第一层是 `level_1`，第二层是 `level_2`，数组里的 `level3` 只作为辅助信息。
- `rag_results` 按 `record_index` 关联，每条记录最多使用前 3 条。
- 只能输出 `classification_options` 中存在的 `level_1/level_2`。
- 不要输出 level3，level3 不在本次分类范围。
- 宁缺毋滥；不确定时可以返回少于 3 个候选，甚至 0 个候选。
- 每个候选必须给出 `confidence`，范围为 0 到 1。

## 输出

只输出 XML。批量输出必须使用 `<results>` 包装，并为每条输入记录返回一个 `<result>`：

```xml
<results>
  <result>
    <record_index>0</record_index>
    <thinking_process>简短说明候选筛选依据。</thinking_process>
    <candidates>
      <candidate>
        <level_1>候选一级分类</level_1>
        <level_2>候选二级分类</level_2>
        <confidence>0.91</confidence>
      </candidate>
    </candidates>
  </result>
</results>
```

约束：

- `record_index` 必须对应输入数组位置，从 0 开始。
- `<candidates>` 下可以有多个 `<candidate>`。
- `<candidate>` 不允许出现 classification_options 外的分类。
- `<confidence>` 必须是小数，不要写百分号或中文描述。
- 不要在 XML 外输出解释文本。
