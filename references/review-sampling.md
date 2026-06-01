# 抽检复核流程

用于在批量分类完成后生成复核样本池和复核报告。复核只发现风险和提出建议，不直接大规模改写分类结果。

## 复核样本池

优先进入复核池：

- `review_flag=true` 的记录；
- `confidence < 0.72` 的记录；
- 兜底类、其他类、待复核类；
- 高频二级标签中的抽样记录；
- 每个一级分类的分层抽样记录；
- 用户指定的高风险标签或重点业务标签。

## 使用脚本

```bash
python3 /path/to/classify-issue-records/scripts/create_review_sample.py \
  workdir/classifications_all.csv \
  --records workdir/records_normalized.jsonl \
  --out workdir/review
```

输出：

- `review_sample.csv`：需要人工或二次模型复核的样本池；
- `review_report.json`：抽样策略、抽样数量、原因分布、标签分布。

## 复核原则

- 先复核高风险样本，再看分层抽样样本。
- 检查 `l1_reason` 是否能解释候选标签为什么被初筛出来。
- 检查 `l2_reason` 是否能解释最终二级标签选择。
- 检查 `evidence_fields` 是否真的支持分类原因。
- 如果发现系统性误判，优先修改 taxonomy 边界或 prompt 规则，再只重跑受影响样本。
- 未经用户确认，不要把复核建议直接批量覆盖到最终结果。
