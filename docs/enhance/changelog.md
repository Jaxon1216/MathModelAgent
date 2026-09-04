# Changelog

## 2026-09-04 · M1 Modeler 基建

- 建立新的 `domain / agents / orchestration / runtime / prompts` Modeler 边界，旧 Coder 和 Writer 保持不动。
- Python 只读扫描 CSV/XLSX 表头，按 sheet 构造 DataCatalog，并区分输入表与 `result*` 输出模板。
- Modeler 改为文本 JSON + Pydantic 校验，检查问题覆盖、真实表列引用和 handoff 长度。
- JSON 修复次数由 `MODELER_MAX_REPAIR_ATTEMPTS` 控制，默认 3，允许任意非负整数。
- 增加统一异常、取消和 `modeler` phase trace，以及本地测试、真实 smoke、旧 Coder 桥接测试。
- 当前本地测试全绿，固定配置三路真实 Modeler smoke 均首轮通过；完整 E2E 尚未运行。
