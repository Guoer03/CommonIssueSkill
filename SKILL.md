---
name: issue-classification-pipeline
description: 执行可迁移复用的大规模问题记录分类流水线。用于把用户提供的问题记录分类到用户提供的一级/二级分类体系中，支持小于 10 条记录 inline 直接返回，也支持 10 条及以上、最高约 20w 级记录通过独立 run workspace、RAG 复用、topk 候选召回 prompt、final 最终判定 prompt、API runner、断点续跑、低置信复核和分类合法性校验完成批处理。适用于用户直接粘贴少量记录、上传 CSV/TSV/JSONL/XLSX/XLSM 表格、提供分类选项、RAG 样例和 OpenAI-compatible 模型 API 的问题分类任务。
---

# 问题分类流水线

## 核心原则

保持 skill 包无状态。`SKILL.md`、`references/` 和 `scripts/` 只放静态能力；每次批量任务的输入、RAG、状态库和结果都放到用户指定的 `workdir`。固定分类选项可以维护在 `references/classification_options.json`，但不要把用户数据、API key、运行状态写入 skill 目录。

## 输入分流

- 小于 10 条记录：使用 Inline Mode。不创建 `workdir`，直接按 `references/classification_options.json`、`references/topk_prompt.md`、`references/topk_io_contract.md`、`references/final_prompt.md` 和 `references/final_io_contract.md` 在当前响应中返回结果。
- 10 条及以上：使用 Workspace Mode。创建独立 `workdir`，用 `scripts/run_pipeline.py` 初始化状态、分阶段调用 API、断点续跑、校验和导出。

分类选项默认放在 `references/classification_options.json`，也可以在初始化工作区时用 `--classification-options` 指向外部文件覆盖。结构为：

```json
{
  "一级分类": {
    "二级分类": ["level3补充信息", "level3补充信息"]
  }
}
```

`level3` 只作为二级分类的内联特征传给 final 阶段，不参与本次最终分类输出。

## 工作流

1. 确认 `references/classification_options.json` 或用户传入的 `classification_options` 非空。没有分类选项时停止并要求提供。
2. 将每条记录标准化为 JSON record 对象，字段固定为 `id`、`problem_overview`、`probelm_details`、`solution_details`、`user_solution`。`user_solution` 是前三个业务字段的合并文本。表格输入支持 CSV、TSV、JSONL、XLSX、XLSM；Excel 默认读取第一个工作表，首行作为表头。Excel 输入必须先用 `inspect-input` 推断列映射并给用户确认，确认后再用 `--field-map` 初始化；只有用户明确接受自动推断时才使用 `--auto-field-map`。
3. 如果要接 RAG，可在 `scripts/rag_retrieve.py` 中实现内网召回逻辑，生成 `rag_results.jsonl` 后由 `init --rag-jsonl` 导入。每条记录只检索一次 RAG，形成 `rag_pool`。TopK 使用前 3 条；Final 使用候选池内优先的前 5 条。
4. TopK 阶段由 Runner 读取 `references/topk_prompt.md` 和 `references/topk_io_contract.md`，组装成完整最终 prompt 后提交给模型。输入 record 对象数组、分类选项和 RAG，输出多个候选 `level_1/level_2/confidence`。
5. Runner 校验 TopK 候选合法性，并从 `classification_options[level_1][level_2]` 注入 `inline_features`。
6. Runner 根据 TopK 置信度动态决定候选池大小：
   - `top1_confidence >= 0.85` 且 `top1 - top2 >= 0.15` 时最多传 3 个候选；
   - 否则最多传 6 个候选；
   - TopK 返回更少候选时不补齐。
7. Final 阶段由 Runner 读取 `references/final_prompt.md` 和 `references/final_io_contract.md`，组装成完整最终 prompt 后提交给模型。输入单条或批量 item：问题记录、候选池、RAG 结果。Final 只能从候选池中选择最终一级/二级。
8. Runner 强制解析 XML、规范化分类名称、校验最终分类是否来自候选池，并根据 `confidence` 派生 `status` 和 `needs_review`。
9. 导出最终 CSV，并保留低置信、未解析、非法输出和无候选样本用于人工复核。

## Workspace Mode 命令

Excel 输入先检查字段映射：

```bash
python3 /Users/wangminghai/.codex/skills/issue-classification-pipeline/scripts/run_pipeline.py inspect-input \
  --input records.xlsx \
  --out runs/job_001/field_map_review.json \
  --sample-size 3
```

将 `field_map_review.json` 中的 `field_map` 和 `sample_records` 展示给用户确认。用户确认或修改 `field_map` 后，再初始化工作区：

```bash
python3 /Users/wangminghai/.codex/skills/issue-classification-pipeline/scripts/run_pipeline.py init \
  --input records.xlsx \
  --workdir runs/job_001 \
  --field-map runs/job_001/field_map_review.json \
  --batch-size 20
```

Excel 输入要求第一行是表头。`inspect-input` 会自动推断常用表头，并输出如下结构，供用户确认或编辑：

```json
{
  "field_map": {
    "id": "工单编号",
    "problem_overview": "故障标题",
    "probelm_details": "故障现象",
    "solution_details": "处理方案"
  },
  "sample_records": []
}
```

- `record_id`、`id`、`工单编号`、`问题编号`、`编号` -> `record.id`
- `problem_overview`、`问题概述`、`问题标题`、`故障标题`、`overview`、`summary` -> `record.problem_overview`
- `probelm_details`、`problem_details`、`问题明细`、`问题详情`、`故障现象`、`问题描述` -> `record.probelm_details`
- `solution_details`、`解决方案`、`处理方案`、`处理措施`、`solution`、`resolution` -> `record.solution_details`

只有用户明确表示可接受自动推断时，才跳过人工确认：

```bash
python3 /Users/wangminghai/.codex/skills/issue-classification-pipeline/scripts/run_pipeline.py init \
  --input records.xlsx \
  --workdir runs/job_001 \
  --auto-field-map
```

如果已有预计算 RAG，可传入 JSONL/CSV，按 `record_id` 关联：

```bash
python3 /Users/wangminghai/.codex/skills/issue-classification-pipeline/scripts/run_pipeline.py init \
  --input records.csv \
  --rag-jsonl rag_results.jsonl \
  --workdir runs/job_001
```

`scripts/rag_retrieve.py` 是预留的空白占位文件；如果内网需要自动召回，请在该文件中实现向量库、BM25、ES 或其他检索逻辑，并输出可直接传给 `run_pipeline.py init --rag-jsonl` 的 JSONL：

```jsonl
{"record_id":"case-001","case_id":"hist-001","record":"历史相似问题文本","level_1":"一级分类","level_2":"二级分类","similarity":0.93,"mapping_justification":"历史样例分类理由"}
```

如果本次任务要临时使用外部分类选项，可显式覆盖：

```bash
python3 /Users/wangminghai/.codex/skills/issue-classification-pipeline/scripts/run_pipeline.py init \
  --input records.csv \
  --classification-options classification_options.json \
  --workdir runs/job_001
```

配置模型运行参数。直接编辑 `scripts/run_pipeline.py` 顶部的 `PYTHON_RUNTIME_CONFIG`，填写内网模型网关、模型名、批大小、超时、token 上限等参数。不要把真实 API key 写入 Python 文件，只填写 `api_key_env`，并在运行环境里设置对应环境变量：

```python
PYTHON_RUNTIME_CONFIG = {
    "base_url": "http://inner-model-gateway/v1",
    "model": "your-classifier-model",
    "api_key_env": "ISSUE_CLASSIFIER_API_KEY",
    "batch_size": 20,
    "limit": 500,
    "timeout": 90.0,
    "temperature": 0.0,
    "max_tokens": 4096,
    "topk_rag_k": 3,
    "final_rag_k": 5,
    "review_threshold": 0.72,
    "max_retries": 2,
    "continue_on_failure": False
}
```

```bash
export ISSUE_CLASSIFIER_API_KEY=...
```

运行时配置优先级为：命令行参数 > `PYTHON_RUNTIME_CONFIG` > 环境变量补位。

运行 TopK：

```bash
python3 /Users/wangminghai/.codex/skills/issue-classification-pipeline/scripts/run_pipeline.py topk \
  --workdir runs/job_001
```

运行 Final：

```bash
python3 /Users/wangminghai/.codex/skills/issue-classification-pipeline/scripts/run_pipeline.py final \
  --workdir runs/job_001
```

临时覆盖单个参数时，直接在命令行追加，例如：

```bash
python3 /Users/wangminghai/.codex/skills/issue-classification-pipeline/scripts/run_pipeline.py topk \
  --workdir runs/job_001 \
  --model another-model \
  --batch-size 10
```

导出结果：

```bash
python3 /Users/wangminghai/.codex/skills/issue-classification-pipeline/scripts/run_pipeline.py export \
  --workdir runs/job_001 \
  --out runs/job_001/classifications.csv
```

重复运行 `topk` 或 `final` 会跳过已成功记录，只处理 `pending` 或 `failed` 状态记录。

## Inline Mode

当记录数小于 10 时，不创建工作目录。直接构造：

- TopK 输入：`records` 对象数组、`references/classification_options.json` 中的 `classification_options`、每条记录的 `rag_results`。
- Final 输入：单条记录、TopK 候选池、候选池注入后的 `inline_features`、RAG 结果。

返回表格字段至少包括：

- `record_index`
- `selected_level_1`
- `selected_level_2`
- `confidence`
- `status`
- `needs_review`
- `mapping_justification`
- `topk_candidates`

Inline Mode 也必须遵守 `references/contracts.md` 的分类合法性和复核规则。

## 必读参考

- `references/classification_options.json`：固定一级/二级分类选项；数组值是二级分类下的 level3 补充信息。
- `references/topk_prompt.md`：TopK 角色声明、判断原则和内网定制规则。
- `references/topk_io_contract.md`：TopK 输入输出契约。
- `references/final_prompt.md`：Final 角色声明、最终判定原则和内网定制规则。
- `references/final_io_contract.md`：Final 输入输出契约。
- `references/contracts.md`：RAG 复用、低置信复核、名称规范化和合法性校验规则。
- `references/rag_retrieval_sdd.md`：RAG 召回模块 SDD；内网 coding agent 实现 `scripts/rag_retrieve.py` 前必须阅读。

Runner 组装 prompt 时会固定生成这些段落：任务说明、角色与判断原则、强制输入输出契约、当前输入 JSON、输出提醒。`topk_prompt.md` 和 `final_prompt.md` 中面向维护者的文件说明不会提交给模型。

## 内置脚本

- `scripts/run_pipeline.py`：创建 run workspace，执行 TopK/Final API 阶段并导出结果。
- `scripts/rag_retrieve.py`：RAG 召回预留占位文件；按内网检索方案实现后，输出 runner 可导入的 `rag_results.jsonl`。
- `scripts/pipeline_core.py`：XML 解析、名称规范化、候选校验、RAG 过滤、复核状态派生等核心函数。
