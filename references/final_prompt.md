# Final Role And Principles

用于放置 Final 阶段的角色声明、最终判定原则、证据优先级、复核策略和内网定制规则。

本文件不放输入输出 schema。输入输出契约放在 `references/final_io_contract.md`。

默认原则：

- 只能从 Runner 给出的 `candidate_pool` 中选择最终一级/二级分类。
- 优先使用结构化记录字段判断；字段缺失或表达模糊时再参考 `user_solution`。
- RAG 只能作为证据补充，不能引入候选池外分类。
- 如果候选都不匹配，输出空分类并将置信度设为 `0`，交给复核。
