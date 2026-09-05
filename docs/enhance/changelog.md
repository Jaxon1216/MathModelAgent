# Changelog

## 2026-09-05 · M2 Coder 工具循环与 ResultPackage 收口

- 修复 Coder P0：同一模型响应中的混合 `load_skill` / `execute_code` 和多个
  `execute_code` 现在严格按响应顺序处理，所有 `tool_call_id` 在下一次模型请求前
  均收到成功或失败结果。
- 每个 Coder phase 开始重置 history 与 turn counter，不复用上一题私有上下文；继续
  共享工作目录和解释器。
- 新增版本化 `ResultPackage`，为每个 phase 原子保存代码快照、stdout、文件指纹、
  图表、指标来源、限制及 partial 失败证据。Writer 改为消费 package 材料。
- Repair 耗尽和 turn 上限生成 `partial` package 后继续进入 Writer；package 写入失败
  仅记录 limitation，不新增总流程阻塞。
- 新增 P0 工具回填、phase 隔离、ResultPackage 持久化和 Writer handoff 测试；
  目标 Pyright 0 errors，`make check`: 223 passed、1 skipped、1 deselected。
- 本轮未运行真实模型、完整 E2E、`eval` 或 M1.5 E2E，未修改质量基线和历史
  scorecard。

## 2026-09-05 · M2 渐进式 SkillsRegistry 本地切片

- Coder 的 skills 边界改为 `SkillRegistry`（L1）和显式 `load_skill`（L2）：
  启动扫描只读取 frontmatter，正文仅在模型真实工具调用后返回；加载正文使用内容
  SHA-256 前缀作为版本标识。
- 删除 phase 起始时的 `_ensure_phase_skills` 正文预注入。成功、未知和非法加载请求
  各自保留对应 tool result；trace 记录 `tool_call_id`、版本、来源、结果和 phase。
- 执行错误后的 Repair 重建当前 phase 的短 history，只保留已加载 skill 名称和版本；
  正文必须通过新的 L2 调用再次取得。
- 新增 registry、Coder history/trace 和按 phase 的 eval gate 测试。聚焦测试 24
  passed，目标 Pyright 0 errors，`make check`: 217 passed、1 skipped、1 deselected。
- 本轮未运行真实模型、完整 E2E、`eval` 或 M1.5 E2E，未改质量基线和历史
  scorecard；当时 ResultPackage 与工具循环尚未完成，后续收口见本页上一条记录。

## 2026-09-05 · M1.5 数据可靠性本地验收

- Tasks 1-9 已完成实现与本地验收，新增版本化 `TaskFacts`、`TaskOutline`、
  逐 Sheet `DataProfile`、
  `CleaningPlan/DataIssue`、冻结 `DataContract` 和逐题 `QuestionPlan`，
  所有产物均带来源、状态、受控路径与指纹。
- 主流程改为先完成只读画像、白名单清洗、确定性验证和最多两次局部 Repair，
  再生成计划；失败、取消或文件漂移会在 Coder、Writer 和最终发布前阻断。
- 旧 Coder 继续通过单一兼容桥接运行，问题 flow 由 outline 拓扑顺序驱动；
  EDA 只读取 frozen contract，不再承担隐式清洗。
- 新增 `make test-m15`。唯一 2024 高教杯 C 题真实附件的离线 fake-LLM 测试
  验证 4 张输入 Sheet、3 个排除的结果模板、源 SHA/mtime、三问计划引用和依赖
  顺序；`make test-m15`: 146 passed，`make check`: 203 passed, 1 skipped,
  1 deselected，全量应用 Pyright 0 errors。
- 本轮未运行真实模型 smoke、完整 E2E 或 eval，未生成新 scorecard，也未修改
  `experiment-log.md`；本记录仅表示 M1.5 实现任务完成，不替代既有 E2E 证据。

## 2026-09-04 · M1 Modeler 基建

- 建立新的 `domain / agents / orchestration / runtime / prompts` Modeler 边界，旧 Coder 和 Writer 保持不动。
- Python 只读扫描 CSV/XLSX 表头，按 sheet 构造 DataCatalog，并区分输入表与 `result*` 输出模板。
- Modeler 改为文本 JSON + Pydantic 校验，检查问题覆盖、真实表列引用和 handoff 长度。
- JSON 修复次数由 `MODELER_MAX_REPAIR_ATTEMPTS` 控制，默认 3，允许任意非负整数。
- 增加统一异常、取消和 `modeler` phase trace，以及本地测试、真实 smoke、旧 Coder 桥接测试。
- 当前本地测试全绿，固定配置三路真实 Modeler smoke 均首轮通过；完整 E2E 尚未运行。
