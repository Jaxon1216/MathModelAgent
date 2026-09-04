# 重建设计决策

这里记录值得保留的概念，但不导入其旧实现。后续实现必须向
`experiment-log.md` 追加新的证据。

| 决策/概念 | 状态 | 关联 commit 与证据 | 结论与重做阶段 |
|---|---|---|---|
| 用 `2e555ad` 作为重建锚点 | 采用 | `2e555ad` 的 `backend/app` 与 trace 版本 `1d71df1` 相同；任务 `20260812-163504-32b726a1` 产出完整 Markdown 和 DOCX | 所有分阶段工作从此开始。 |
| 归档此前增强工作 | 采用 | checkpoint `6670d1b`；任务 `20260903-132033-3befb447` 至 `20260903-153509-77cc5ea1` | 保存尝试和评分卡，不携带其代码。 |
| 清洗数据 contract 再交给 Modeler | 保留概念 | commit `cbb75f8`；任务 `20260903-153509-77cc5ea1` 在后续 Modeler 失败前已写齐预期 cleaned 文件 | 基础计划契约稳定后，作为 M1 输入增强重新实现。 |
| Agent 上下文隔离 | 保留概念 | commit `a958c4d`；没有成功完整 E2E 验证该实现 | M1 后重新评估，要求隔离单测和成功 E2E。 |
| `PhaseResult` 结果交接 | 保留概念 | commit `6778352`；没有成功完整 E2E 验证该实现 | 在 Coder/Writer 阶段重做，不属于 M1。 |
| 完整 tool-call loop | 保留概念 | commit `59d8d5b`；任务 `20260903-134852-c9cc07ab` 完成 EDA 但未完成全任务 | M2 用有界状态和结果产物重做。 |
| 强制 Modeler function call | 拒绝当前实现 | checkpoint `6670d1b`；任务 `20260903-145530-d8cb5f73`、`20260903-153509-77cc5ea1` 三次尝试均无 tool call | M1 使用文本 JSON、Pydantic 校验和一次修复。 |
| runtime budget / interpreter cleanup | 保留概念 | commits `9a691b4`、`70362b0`；任务 `20260903-143558-929e7890`、`20260903-152345-3fbc9a28` 暴露 EDA token 熔断 | 受影响阶段稳定后独立引入；预算不能代替完成策略。 |
