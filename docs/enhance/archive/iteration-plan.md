# 迭代计划：运行时质量与证据闭环

## 当前状态

工程护栏、唯一赛题基线、LLM trace、清洗先于建模、阶段上下文隔离和 `PhaseResult` 已落地。后续仅改 `backend/` 主链路；每项先补测试，执行 `make check`，再用 2024 高教杯 C 题跑 `make eval` 对比基线。只有 `regression_check.all_pass=true` 且关键指标无退化才提交。

- `20260903-153509-77cc5ea1` 已验证 EDA 在写齐预期 cleaned 文件后直接通过 contract，不再因重复回读和总结耗尽阶段 token。
- `20260903-modeler-probe` 已验证 DeepSeek Modeler 关闭 thinking 后可以提交 `submit_model_plan`。下一次完整 fixture 需验证 ques* 求解、Writer 和 DOCX 链路；不得再把此前 thinking-only 的 Modeler 失败当作当前阻断。

## P0：正确性与可恢复性

1. 任务成功必须在 `res.md`、`res.docx` 与结构检查全部成功后才发布；导出失败不得显示成功。
2. 解释器创建后必须 `try/finally` 清理；为单次 Jupyter 执行设置超时、interrupt 和 restart，避免死循环或遗留 kernel。
3. Coder/Writer 改为完整多工具调用循环，逐一回填 tool response；Writer 支持多次检索、检索失败降级和最终正文校验。
4. 章节记录 `pending/running/succeeded/failed`，空响应或异常触发有限重试并写入终态，禁止静默拼接。
5. LLM 重试使用非阻塞等待；增加 phase 的调用、时间和 token 上限及熔断事件。

验收：补多工具调用、空章节、导出失败、kernel 超时/cleanup 的单测；任务终态、DOCX 和错误信息一致。

## P1：成本与可验证交接

1. 改为 metadata-only skill 预加载，正文、图表模板按需 `load_skill`；同步修正基线门禁，不能要求与实现冲突。
2. 对代码、stdout、错误和文件清单实施独立预算；大输出落盘，只向后续轮次传结构化摘要和路径。
3. Modeler 第一阶段已实现紧凑计划 schema：严格校验 `version`、按 Coordinator 声明动态生成的 `quesN` / `sensitivity_analysis` 键集合，以及每个计划字符串的类型、非空和长度；每个紧凑字符串提示涵盖数据/模型、约束/验证和图表。数据表、目标、约束、方法、验证、参数、图表、回退方案等拆分为细粒度可验证的 Pydantic 字段仍属后续 P1，尚未完成。DeepSeek Chat 的 Modeler 调用已关闭 thinking 并通过真实工具探针，完整 E2E 仍待重跑确认。
4. Coder 产出 `claims.json` / `result.json`，每个论文数字带单位和来源定位；冻结为 `frozen_numbers.json`，Writer 只读冻结 claim。
5. 由显式 manifest 登记 paper figure、诊断图、表和 claim source；最终一致性审计检查论文数字、符号、方法、图表和文件。

验收：评分卡新增 token/延迟/turn、数字可追溯率、图 claim 覆盖率和导出通过率；同一 fixture 至少多次运行，按中位数比较成本与质量。
