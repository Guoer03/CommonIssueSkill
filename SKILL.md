---
name: issue-classification-pipeline
description: 执行可迁移复用的问题记录分类。用于把用户提供的 10 条以下问题记录或一个 Excel 文件分类到用户提供的一级/二级分类体系中；小批量记录由当前运行 skill 的模型直接返回结果，Excel 输入使用工作副本追加分类结果列、按批读取 pending/retry 行、由当前模型分类、再写回 Excel，并循环到每条记录都有合法一级/二级分类。适用于没有模型 API、只需要处理少量记录或 Excel 表格的问题分类任务；使用前必须有用户提供的分类标签或 references/classification_options.json。
---

# 问题分类流水线

## 核心原则

保持 skill 包无状态。`SKILL.md`、`references/` 和 `scripts/` 只放静态能力；用户数据、Excel 工作副本、批次文件和结果文件都放在用户任务目录中。不要在 skill 目录保存用户数据、运行状态或业务结果。

本 skill 不调用远端模型 API。分类由当前运行 skill 的模型完成；脚本只负责 Excel 读取、字段映射、批次导出、结果写回、断点续跑和分类合法性校验。

## 输入分流

- 10 条以下记录：使用 Inline Mode。不创建工作文件，当前模型直接按分类选项、TopK 判断原则和 Final 判断原则返回结果。
- Excel 文件：使用 Excel Workfile Mode。复制原始 Excel 为 `.classifying.xlsx` 工作副本，在末尾追加状态列和结果列；每次读取少量 pending/retry 行，由当前模型分类后写回工作副本，直到每条记录都有合法一级/二级分类。

分类选项默认放在 `references/classification_options.json`，也可以由用户在任务中提供。结构为：

```json
{
  "一级分类": {
    "二级分类": ["level3补充信息", "level3补充信息"]
  }
}
```

`level3` 只作为二级分类的补充特征，不参与最终分类输出。

如果分类选项为空或用户完全没有提供标签，必须停止并要求用户提供分类标签。

## Excel Workfile 流程

1. 检查分类选项是否非空。
2. 对 Excel 运行 `inspect-input`，推断 `id`、`problem_overview`、`probelm_details`、`solution_details` 的列映射，并把样例展示给用户确认。
3. 用户确认字段映射后运行 `init`，复制原始 Excel 为工作副本并追加内部标准化列与结果列。
4. 运行 `next-batch` 导出当前 pending/retry 记录，默认每批 10 条；长文本或上下文压力大时可降到 5 条。未尝试 pending 行优先，retry 行排在后面，避免单条异常挡住后续记录。
5. 当前模型只能基于当前轮 batch、当前轮可用的 RAG 召回数据和分类选项完成分类。不要参考历史对话、历史 batch、此前已处理记录或由历史记录总结出的“分类经验”来判断当前记录。
6. 将当前模型的分类结果整理成 `references/excel_workfile_mode.md` 中 `apply-results` 接受的 JSON，再运行 `apply-results` 写回 Excel。脚本会规范化分类名称、校验分类合法性、派生 `status/needs_review`。如果 topk/final 提示词输出 XML 或其他格式，当前 agent 负责转换成写回 JSON。
7. 重复 `next-batch` 和 `apply-results`，直到 `status` 输出 `complete=true`。只有 `classified` 和 `low_confidence` 算完成；`retry`、`failed`、`unresolved` 都必须继续处理。

Excel 输入要求第一行是表头，默认处理第一个工作表。原始 Excel 不直接覆盖，始终写入工作副本。

## Excel Workfile 命令

字段映射检查：

```bash
python3 /Users/wangminghai/.codex/skills/issue-classification-pipeline/scripts/excel_workfile.py inspect-input \
  --input records.xlsx \
  --out field_map_review.json \
  --sample-size 3
```

确认或修改 `field_map_review.json` 后初始化工作副本：

```bash
python3 /Users/wangminghai/.codex/skills/issue-classification-pipeline/scripts/excel_workfile.py init \
  --input records.xlsx \
  --out records.classifying.xlsx \
  --field-map field_map_review.json
```

导出下一批 pending/retry 记录：

```bash
python3 /Users/wangminghai/.codex/skills/issue-classification-pipeline/scripts/excel_workfile.py next-batch \
  --workbook records.classifying.xlsx \
  --batch-size 10 \
  --out current_batch.json
```

当前模型分类后，准备结果 JSON：

```json
{
  "results": [
    {
      "row_number": 2,
      "record_id": "case-001",
      "selected_level_1": "一级分类",
      "selected_level_2": "二级分类",
      "confidence": 0.86,
      "mapping_justification": "根据问题根因、问题明细和分类边界说明匹配逻辑。",
      "topk_candidates": []
    }
  ]
}
```

写回 Excel：

```bash
python3 /Users/wangminghai/.codex/skills/issue-classification-pipeline/scripts/excel_workfile.py apply-results \
  --workbook records.classifying.xlsx \
  --results current_results.json \
  --classification-options references/classification_options.json
```

查看进度；只有 `complete=true` 才表示每条记录都已有合法分类：

```bash
python3 /Users/wangminghai/.codex/skills/issue-classification-pipeline/scripts/excel_workfile.py status \
  --workbook records.classifying.xlsx
```

## Inline Mode

当输入记录少于 10 条时，不创建工作文件。当前模型直接完成分类并返回表格，字段至少包括：

- `record_index`
- `record_id`
- `selected_level_1`
- `selected_level_2`
- `confidence`
- `status`
- `needs_review`
- `mapping_justification`
- `topk_candidates`

Inline Mode 仍必须遵守 `references/contracts.md` 的分类合法性、证据优先级和复核规则。

Excel Mode 的完成标准更严格：最终不能留下空分类、非法分类或仅记录错误的行。遇到单条输出异常时，将该行写成 `status=retry`，继续处理其他行，最后再回到 retry 行直到全部分类成功。

## 必读参考

- `references/classification_options.json`：固定一级/二级分类选项；数组值是二级分类下的 level3 补充信息。
- `references/topk_prompt.md`：TopK 候选筛选角色、原则和领域规则。
- `references/topk_io_contract.md`：TopK 输入输出契约。
- `references/final_prompt.md`：Final 最终判定角色、原则和领域规则。
- `references/final_io_contract.md`：Final 输入输出契约。
- `references/contracts.md`：Excel workfile、复核状态、名称规范化和合法性校验规则。
- `references/excel_workfile_mode.md`：Excel Workfile Mode 的详细操作协议；处理 Excel 时必须阅读。

## 内置脚本

- `scripts/excel_workfile.py`：维护 Excel 工作副本，提供 `inspect-input`、`init`、`next-batch`、`apply-results`、`status`。
- `scripts/pipeline_core.py`：名称规范化、候选校验、taxonomy 合法性校验、复核状态派生等核心函数。
