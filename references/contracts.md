# Pipeline Contracts

## RAG 复用

- 每条记录只执行一次 RAG 检索，生成 `rag_pool`。
- TopK 使用 `rag_pool` 中相似度最高的 3 条。
- Final 使用同一个 `rag_pool`，优先选择 `level_1/level_2` 属于候选池的样例，最多 5 条。
- 候选池内样例不足时，可补充全局高相似样例，但必须标记 `out_of_candidate_pool_reference=true`。
- RAG 永远不能引入 classification_options 或 candidate_pool 外的新分类。

## TopK 候选池

- TopK 只输出 `level_1`、`level_2`、`confidence`。
- Runner 必须校验候选存在于 `classification_options`。
- Runner 从 `classification_options[level_1][level_2]` 注入 `inline_features`，也就是该二级分类下全部 level3。
- Runner 根据 TopK 置信度动态决定传给 Final 的候选数量：
  - `top1_confidence >= 0.85` 且 `top1 - top2 >= 0.15` 时最多 3 个；
  - 其他情况最多 6 个；
  - TopK 返回更少候选时不补齐。

## Final 复核状态

Final 输出 `confidence`，Runner 派生状态：

- TopK 无候选：`status=unresolved`，跳过 Final，`needs_review=true`。
- Final 输出空分类：`status=unresolved`，`needs_review=true`。
- Final `confidence < review_threshold`：`status=low_confidence`，`needs_review=true`。
- Final 分类合法且置信度达标：`status=classified`，`needs_review=false`。

默认 `review_threshold=0.72`。

## 名称规范化和合法性校验

Runner 必须规范化模型输出再校验：

- 去除首尾空白；
- 全角转半角；
- 合并多余空格；
- 将 `／` 和 `\` 统一成 `/`；
- 去除斜杠两侧空格。

合法性规则：

- TopK 输出的 `level_1` 必须存在于 `classification_options`。
- TopK 输出的 `level_2` 必须属于该 `level_1`。
- Final 输出的 `selected_level_1/selected_level_2` 必须来自该记录的 `candidate_pool`。
- level3 不允许作为 TopK 或 Final 的分类输出。
- 非法输出应重试；重试后仍非法则进入 `failed` 或 `needs_review`。

## Workspace 状态

每次批量任务状态都放在 `workdir/state.sqlite`。Skill 目录不保存任何业务状态。
