# TopK Role And Principles

用于放置 TopK 阶段的角色声明、领域判断原则、分类优先级、反误判规则和内网定制规则。

本文件不放输入输出 schema。输入输出契约放在 `references/topk_io_contract.md`。

默认原则：

- 只从用户提供的 `classification_options` 中召回候选一级/二级分类。
- 宁缺毋滥；证据不足时可以少给候选，不要为了凑数输出低质量候选。
- 不要预测 level3；level3 只作为二级分类的补充特征。
- 优先依据问题本身的语义证据判断，不要仅凭处理动作词决定分类。
