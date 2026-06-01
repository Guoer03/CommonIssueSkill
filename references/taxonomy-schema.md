# Taxonomy 结构

创建或校验 20x20 问题分类体系时使用本参考。

## JSON 结构

```json
{
  "version": "v1",
  "language": "zh-CN",
  "label_policy": {
    "single_label": true,
    "primary_decision_field_order": ["root_cause", "detail", "solution_as_auxiliary", "overview"],
    "low_confidence_threshold": 0.72
  },
  "labels": [
    {
      "l1_code": "L01",
      "l1_name": "一级分类名称",
      "definition": "这一类问题的判定边界。",
      "positive_examples": ["用户提供时保留；不要自动补全"],
      "negative_examples": ["用户提供时保留；不要自动补全"],
      "tie_break_rule": "与其他一级分类冲突时如何判断。",
      "children": [
        {
          "l2_code": "L01-01",
          "l2_name": "二级分类名称",
          "definition": "这一二级分类的具体边界。",
          "positive_examples": ["用户提供时保留；不要自动补全"],
          "negative_examples": ["用户提供时保留；不要自动补全"],
          "keywords": ["可选关键词"],
          "tie_break_rule": "与同一一级下其他二级分类冲突时如何判断。"
        }
      ]
    }
  ]
}
```

## 标签规则

- 使用稳定编码，例如 `L01`、`L01-01`，这样标签名称后续修改时不会破坏历史结果。
- 一级分类要有明确业务含义，并尽量互斥。
- 二级分类要可执行。优先使用根因类或解决方案模式类标签，少用模糊症状类标签。
- 标签名称保持用户原文不变；不要自动改名、合并、拆分或新增标签。
- 用户缺少定义或边界时，可以自动补全 `definition` 和 `tie_break_rule`，但必须逐项让用户确认后才能用于批量分类。
- 不要自动补全正例和反例。用户已经提供时保留；用户未提供时留空或省略。
- 只有确实无法覆盖时才设置“其他 / 需复核”。如果该类占比超过 5-8%，说明 taxonomy 需要修订。

## 质量检查

批量分类前：

- 确认一级分类约为 20 个。
- 确认高频一级分类有足够的二级覆盖。
- 搜索定义重叠、关键词重复、名称差异很小的标签。
- 确认所有自动补全的定义和边界都已由用户逐项确认。
- 跑一轮校准样本，并检查最常见的混淆组合。
- 正式全量分类前冻结 taxonomy 版本。

批量分类后：

- 复核数量异常高或异常低的标签。
- 复核所有未知标签编码和非法一级/二级组合。
- 复核低置信记录，以及 `review_flag=true` 过多的标签。
- 如果 taxonomy 后续演进，在 skill 外部维护版本变更记录。
