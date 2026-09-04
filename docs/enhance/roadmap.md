# 重建路线图

## 当前阶段：M1 建模步骤

### 范围

仅修改 Modeler 的输入、输出、校验和聚焦测试。不修改 Coder ReAct、Writer、导出、并发或图表策略。

### 完成定义

- 同一 fixture、同一模型配置下连续 3 次得到合法建模计划。
- 计划覆盖全部 `quesN` 与 `sensitivity_analysis`。
- 不编造数据列或文件。
- 一次完整 E2E 能把计划交给基线 Coder 并生成最终产物。
- 失败有明确原因，不依赖无界重试。

### 技术选择

第一版使用文本 JSON、Pydantic 校验和有限修复；不把 provider 强制 function call
作为全链路依赖。修复次数由 `MODELER_MAX_REPAIR_ATTEMPTS` 控制，默认 3，
接受任意非负整数；它只控制格式/契约修复，不控制网络重试或 Agent 步数。

### 验证节奏

1. 每次修改 Modeler 后运行 `cd backend && make test-modeler`。
2. 需要验证真实模型行为时运行 `make smoke-modeler`；该命令用固定 fixture
   并发执行 3 次独立 Modeler，不运行 Coordinator、Coder、Writer、解释器或 Pandoc。
3. 仅当三次 smoke 全部通过后运行一次完整 E2E，作为 M1 阶段验收。

### 当前验收状态

- 本地 Modeler 测试：通过。
- 固定配置三路真实 smoke：通过（revision `0727178`）。
- 新 ModelPlan 到旧 Coder 的 handoff 契约：通过。
- 完整 E2E：待运行。

## 下一阶段：M2 Coder ReAct

仅在 M1 达到完成定义后开启。本阶段的范围和验收届时再写；M1 内不做任何 Coder 实现。
