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

第一版使用文本 JSON、Pydantic 校验和一次修复；不把 provider 强制 function call 作为全链路依赖。

## 下一阶段：M2 Coder ReAct

仅在 M1 达到完成定义后开启。本阶段的范围和验收届时再写；M1 内不做任何 Coder 实现。
