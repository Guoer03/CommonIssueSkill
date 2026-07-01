# Classification Contracts

## Excel Workfile

- 原始 Excel 不直接覆盖；先复制成 `.classifying.xlsx` 工作副本。
- Excel 工作副本是批量处理状态载体；已写回的 `status`、`needs_review`、`selected_level_1`、`selected_level_2` 等列用于断点续跑。
- 每次只把当前 pending/retry batch 放进上下文，默认 10 条；长文本或上下文压力大时使用 5 条。不要把历史已处理记录追加进 prompt。
- 分类只能依据当前轮 batch、当前轮可用的 RAG 召回数据和分类选项。不要参考历史对话、历史 batch、此前已处理记录或由历史记录总结出的“分类经验”。
- `next-batch` 导出 `status` 为空、`pending`、`retry`、`unresolved`、`failed` 或其他非完成状态的行；`classified`、`low_confidence` 视为已成功分类。
- 未尝试 pending 行优先进入 batch；retry/unresolved/failed 行排在后面，避免单条异常阻塞后续记录。
- `apply-results` 写回前必须规范化名称并校验 `selected_level_1/selected_level_2` 是否来自 `classification_options`。
- 非法分类不强行修正，写入 `status=retry`、`needs_review=true` 和 `error_message`，后续循环继续处理。
- `status` 命令只有在所有数据行都是 `classified` 或 `low_confidence` 时才输出 `complete=true`。

## 证据优先级

分类判断按以下优先级取证：

1. 问题根因或问题明细中明确描述的直接原因。
2. 问题明细中的现象、告警、对象、模块、失败环节。
3. 解决方案只在根因或明细缺失时作为辅助判断；不能仅凭“升级、重启、倒换、更换”等处理动作决定分类。
4. 问题概述只作为兜底线索。

## TopK 候选

- TopK 只输出 `level_1`、`level_2`、`confidence`。
- 候选必须来自 `classification_options`。
- 当前模型应根据 TopK 置信度控制候选数量：
  - `top1_confidence >= 0.85` 且 `top1 - top2 >= 0.15` 时最多保留 3 个；
  - 其他情况最多保留 6 个；
  - 证据不足时可以少于 3 个，甚至 0 个。
- TopK 置信度只用于排序和扩缩候选池。传入 Final 的 `candidate_pool` 不包含 TopK 置信度，只包含 `level_1`、`level_2`、`inline_features`。

## Final 复核状态

Final 输出 `confidence`，写回时派生状态：

- TopK 无候选或 Final 空分类：`status=retry`，`needs_review=true`，后续循环必须重新分类。
- Final `confidence < review_threshold`：`status=low_confidence`，`needs_review=true`。
- Final 分类合法且置信度达标：`status=classified`，`needs_review=false`。
- 输出非法分类：`status=retry`，`needs_review=true`。

默认 `review_threshold=0.72`。

## 名称规范化和合法性校验

写回结果前必须规范化模型输出再校验：

- 去除首尾空白；
- 全角转半角；
- 合并多余空格；
- 将 `／` 和 `\` 统一成 `/`；
- 去除斜杠两侧空格。

合法性规则：

- TopK 输出的 `level_1` 必须存在于 `classification_options`。
- TopK 输出的 `level_2` 必须属于该 `level_1`。
- Final 输出的 `selected_level_1/selected_level_2` 必须来自 `classification_options`。二者都为空只能作为临时 retry，不是最终完成结果。
- level3 不允许作为 TopK 或 Final 的分类输出。
