---
name: classify-issue-records
description: 执行高吞吐问题分类。用于将包含问题概述、问题明细、问题根因、解决方案字段的记录，分类到用户已提供的一级/二级分类标签或 taxonomy 中，尤其适合批量处理 CSV、JSONL、XLSX 风格的数据集，例如 15000 条客服、运营、缺陷、事故、投诉或质量问题记录。使用本 skill 前必须有用户提供的分类标签；如果用户完全没有提供标签，必须先要求用户给出，用户仍未提供时直接结束，不要自动生成分类体系。若用户已提供标签但缺少定义或边界，可以自动补全 definition 和 tie_break_rule，并逐项让用户确认；不要自动补全正例和反例。也用于去重、生成受约束的大模型分类批次、校验输出，并优化准确率和速度。
---

# 问题记录分类

## 目标

将问题记录按照用户提供的稳定两级分类体系打标。输入字段固定优先使用：问题概述、问题明细、问题根因、解决方案。处理批量数据时，把确定性的清洗、去重、回填、校验交给脚本，把需要语义判断的分类选择交给模型。

## 最快且准确的流程

1. **先确认用户已提供分类标签。** 分类标签可以是 taxonomy JSON、一级/二级标签表，或明确的标签清单。完全没有标签时先要求用户提供；用户仍未提供时直接结束，不继续处理。
2. **补齐并确认标签定义与边界。** 如果用户已提供标签但缺少定义或边界，可以自动补全 `definition` 和 `tie_break_rule`，但必须逐项让用户确认后才能进入批量分类。不要自动补全正例和反例。
3. **冻结用户确认后的分类体系。** 开始处理 15000 条数据后，不允许边跑边新增、推断、改写或补全标签。
4. **检查标签边界是否足够清楚。** 每个一级、二级标签至少要有编码、名称、定义和冲突判定规则。详见 `references/taxonomy-schema.md`。
5. **先标准化和去重。** 对源 CSV/JSONL 运行 `scripts/prepare_issue_batches.py`。只分类唯一问题文本，完成后再用 `scripts/expand_duplicate_classifications.py` 回填重复记录。
6. **先召回候选，再让模型选择。** 每条记录先从用户确认后的分类体系中召回或筛选 5-12 个候选二级标签，并记录候选集合入选原因；再让模型只在候选标签中选择。避免让模型每条都扫描 400 个标签。
7. **批量调用模型。** 候选标签较少时，每个 prompt 分类 20-50 条唯一记录。记录较长或分类边界混乱时，降低到 15-25 条。
8. **拦截低置信样本。** 置信度低于 0.72、模型疑似编造标签、根因模糊、字段冲突或落入宽泛兜底类时，设置 `review_flag=true`。只对这些样本做二次复核。
9. **交付前校验。** 使用 `scripts/validate_classifications.py` 对分类体系和源记录进行校验，检查覆盖率、非法一级/二级组合、置信度分布和过度集中的标签。
10. **生成抽检复核样本。** 使用 `scripts/create_review_sample.py` 生成高风险样本池和分层抽样报告，复核结果用于发现系统性误判，不直接批量覆盖分类结果。

## 分类标签缺失处理

当用户没有提供分类标签、taxonomy 或一级/二级标签表时，使用此模式。

1. 立即要求用户提供分类标签，不要先处理数据、不要抽样生成标签、不要根据原始记录推断标签。
2. 明确告诉用户可接受的格式：
   - taxonomy JSON，格式见 `references/taxonomy-schema.md`；
   - CSV/表格形式的一级分类、二级分类、定义、正例、反例；
   - 简化版标签清单，但至少要能识别一级/二级从属关系。
3. 如果用户仍未提供分类标签，直接结束当前任务，并说明：缺少分类标签，无法进行受约束分类。
4. 不要输出临时分类体系、候选标签建议、自动聚类结果或“我先帮你生成一版 taxonomy”。只有用户明确改口要求创建标签体系时，才把那作为新的任务处理。

## 标签定义与边界补全模式

当用户已经提供一级/二级分类标签，但缺少定义或边界时，使用此模式。

1. 保持用户给出的标签名称、编码、一级/二级从属关系不变。不要新增标签、删除标签、合并标签、拆分标签或改名。
2. 只自动补全缺失的 `definition` 和 `tie_break_rule`：
   - `definition` 说明该标签覆盖什么问题、排除什么问题；
   - `tie_break_rule` 说明它和相邻或易混标签冲突时如何判断。
3. 不要自动补全 `positive_examples` 和 `negative_examples`。如果用户已经提供正例或反例，保留原文；如果没有，留空或省略。
4. 把补全后的定义和边界整理成可审阅清单，逐项让用户确认。标签很多时，可以按一级分类分组展示，但每个一级和二级标签都必须能被用户看到并确认。
5. 用户未确认前，不要进入批量分类。用户要求修改某些定义或边界时，先更新这些字段，再重新请求确认。
6. 用户确认后，将这版 taxonomy 视为冻结版本，后续分类只能使用其中已有标签。

## 批量分类模式

当用户提供的分类体系已存在，且定义/边界已由用户确认冻结时，使用此模式。

1. 运行预处理脚本，分类 `records_unique.jsonl` 和 `batches/`，不要直接分类完整原始文件。
2. 使用 `references/batch-prompt.md` 作为 prompt 契约。输出严格 JSONL 或 JSON 数组，字段包括：
   - `record_id`
   - `l1_code`, `l1_name`
   - `l2_code`, `l2_name`
   - `confidence`
   - `evidence_fields`
   - `l1_reason`：解释为什么初筛出这 5-12 个候选标签
   - `l2_reason`：解释为什么最终选择该二级标签
   - `review_flag`
3. 分类运行期间绝不新增、推断、改写或补全标签。只能使用用户提供的 taxonomy 中已有编码。
4. 如果一条记录可归入多个标签，优先选择最符合问题根因的标签；其次参考问题明细。解决方案仅在问题根因缺失或需要辅助判断时使用，不能仅凭“升级、重启、倒换、更换”等处理动作决定分类。问题概述只作为最后参考。根因缺失或字段矛盾时设置 `review_flag=true`。
5. 唯一记录分类完成后，回填重复记录：

```bash
python3 /path/to/classify-issue-records/scripts/expand_duplicate_classifications.py workdir/classifications_unique.csv workdir/duplicate_map.csv --out workdir/classifications_all.csv
```

6. 校验完整输出：

```bash
python3 /path/to/classify-issue-records/scripts/validate_classifications.py workdir/classifications_all.csv --taxonomy taxonomy.json --records workdir/records_normalized.jsonl --out workdir/validation
```

7. 生成抽检复核样本：

```bash
python3 /path/to/classify-issue-records/scripts/create_review_sample.py workdir/classifications_all.csv --records workdir/records_normalized.jsonl --out workdir/review
```

复核样本优先覆盖 `review_flag=true`、低置信、兜底类、高频标签和每个一级分类的分层抽样。详见 `references/review-sampling.md`。

## 准确率控制

用于高风险或噪声较大的数据集：

- 正式全量运行前，基于用户提供的分类标签创建至少 200 条人工复核过的金标样本。
- 只对模糊样本做双重分类：低置信、稀有标签、高影响标签、缺少问题根因的记录。
- 批量分类后生成 `review_sample.csv` 和 `review_report.json`，复核高风险样本和分层抽样样本。
- 通过抽检追踪每个标签的精确率。一级分类过宽、二级“其他”占比过高，通常说明分类体系需要修改。
- 维护混淆日志。两个标签反复混淆时，在批量运行前增加冲突判定规则或合并标签。
- 不要为了形式上的 20x20 牺牲边界清晰度。清晰的 20 个一级分类和可变数量二级分类，优于互相重叠的 400 个标签。

## 速度控制

用于保持 15000 条级别任务的处理速度：

- 先按四个输入字段的标准化文本去重，再调用模型。
- 使用 `cache_key` 缓存分类结果；重复运行时复用已有分类。
- 短记录每批 40-50 条，长记录每批 15-25 条。
- 每个 prompt 只放候选标签。只有排查问题时才加载完整 taxonomy。
- CSV/JSONL 准备、重复记录回填、结果校验全部使用本地确定性脚本。

## 内置资源

- `scripts/prepare_issue_batches.py`：标准化 CSV/JSONL 问题记录，生成唯一记录、重复映射、抽样文件和 JSON 批次。
- `scripts/expand_duplicate_classifications.py`：把唯一记录分类结果回填到每条原始记录。
- `scripts/validate_classifications.py`：校验分类结果与 taxonomy、源记录覆盖率是否一致。
- `scripts/create_review_sample.py`：生成抽检复核样本池和复核报告。
- `references/taxonomy-schema.md`：taxonomy JSON 结构、标签编写规则和质量检查。
- `references/batch-prompt.md`：受约束批量分类的 prompt 模板。
- `references/review-sampling.md`：抽检复核策略和复核原则。
