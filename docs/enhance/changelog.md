# Changelog

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
