# Changelog

质量与工程化迭代记录。新节点追加在上方。

## 2026-08-25 · 数据产物先于建模

- 工作流改为：拆题 → Coder 数据准备（`cleaned/{主名}__{sheet}.csv`）→ 后端写 `data_contract.json` → 建模手读限长渲染文本选模型 → 解题 Coder **重置对话** 后校验再求解。
- 建模手不接解释器；无表格则跳过清洗，按机理题建模。
- Writer 预处理章只拿表清单 + 清洗摘要，不拿整份 JSON。

## 2026-08-25 · 运行时上下文盘点

- 新增 [runtime-context.md](./runtime-context.md)：四个 Agent 的历史隔离、交接物、以及「不要用方法 A」如何漏进论文。
- 治理方向：过程约束与赛题文本拆开；Writer 不吃原始 `ques_all` / Coder 收工闲聊。

## 2026-08-25 · 现状

护栏、E2E 评分、LLM 观测已就绪；内容质量修复未开始。

- **已完成**：阶段 0 Makefile/pytest；阶段 3 唯一赛题 `2024高教杯C题` + `eval_task.py`；阶段 4 `llm.response` token/延迟；**数据准备先于建模**（`data_contract.json`）。质量基线任务 `20260812-163504-32b726a1`。
- **未开始**：阶段 1 UserOutput / md 预处理单测；阶段 2 引用、公式、Word 模板、冻数字、Writer 空章节。
- **运行时缺口**：每 `ques*` 仍预注入 3 份完整 skill；Writer 直接吃 Coder 散落输出（无 `frozen_numbers`）；Pandoc 无 `--reference-doc`；图注用文件名。
- **同级探查**：S–B 参考见 [sibling-references.md](./sibling-references.md)。第一刀意向：图表模板注入 + 冻数字给 Writer。
