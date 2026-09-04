# Changelog

质量与工程化迭代记录。新节点追加在上方。

## 2026-09-03 · EDA 收尾与 DeepSeek Modeler 工具兼容

- EDA 根据原始 csv/xlsx 的 sheet 清单推导必须落盘的 `cleaned/` 文件；文件齐全后直接交由后端构建并校验 `data_contract.json`，不再让 Coder 为重复回读和总结空转。`LLMRuntimeBudgetExceeded` 也不再被 Coder 当作可重试异常吞掉。
- 任务 `20260903-153509-77cc5ea1` 验证 EDA 写齐 4 张表后发出 `eda.outputs_ready`、通过 contract 并进入 Modeler；不再以 EDA token 熔断结束。
- DeepSeek Chat 默认开启 thinking，Modeler 请求现对 `deepseek-*` 显式传递 `thinking=disabled` 并恢复强制 `submit_model_plan` 工具选择。真实探针 `20260903-modeler-probe` 在 13.5 秒内返回有效 plan tool call；Coder 和 Writer 仍保留原有 thinking 配置。

## 2026-09-03 · Modeler 结构化输出有界恢复验证

- Modeler 改为通过 `submit_model_plan` 工具 schema 交付计划；提示词收紧为紧凑有界约定，配置为最多 3 次尝试、单次 60s、1600 tokens、每字段 2400 字符，并记录 Modeler phase trace。失败的原始 Modeler 内容不持久化。
- 完整 fixture 任务 `20260903-145530-d8cb5f73` 中 EDA 成功；Modeler 连续三次仅 reasoning、未产生工具调用，最终以稳定的 `Modeler output recovery exhausted` 结束。trace 已验证，未生成 `res.md` / `res.docx`，任务未成功。
- `deepseek-v4-pro` thinking mode 未生成工具调用是下一步的外部模型能力或配置阻断；本轮未通过完整 E2E，不应宣称 full E2E passed。

## 2026-09-02 · 上下文隔离与结构化结果交接

- Coder 按 `eda` / `ques*` / `sensitivity_analysis` 隔离对话历史，保留共享解释器和数据产物；Writer 每章使用独立 history。
- Coordinator 将可公开题面与执行约束拆成双通道，约束仅交给 Modeler/Coder，Writer 不再读取原始 `ques_all`。
- 新增精简 `PhaseResult`，按阶段保存状态、有限事实、限制和真实产物指纹；失败阶段不再调用 Writer 生成精确结论。
- 本轮未引入 source inspection、task manifest、按需 skill 加载或大规模 eval 扩展；完整技能仍按阶段预加载。

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
