# 待办事项

## 观测层（当前进行中）

- [x] Makefile + pytest 工程护栏
- [x] LLM 层观测：Usage 补字段 + `llm.response` trace 事件
- [x] E2E 评估脚本 `eval_task.py` + 基线 expected.json
- [x] 例题预解析 fixture（`fixtures/problems/`）
- [x] AGENTS.md 补测试与评估使用说明
- [x] `eval_task.py` 补 LLM 层指标聚合（`llm.response` 事件 → 总 token、延迟）
- [x] `in_text_cite_count` 正则修复（`\[\^\d+\]` 会误匹配脚注定义行）
- [x] `docx_smoke` 加强：解析 docx 内容，检查是否含 drawing / oMath
- [x] eval 支持 `--save-scorecard` / `--compare` + `make eval`
- [x] 基线收敛为唯一赛题 `2024高教杯C题`（不再维护 social-media / 另两道例题）

## EDA / 数据预处理解耦（已落地：清洗先于建模）

- [x] 建模前 Coder 数据准备，产物 `cleaned/{主名}__{sheet}.csv` + `data_contract.json`
- [x] 建模手读 contract 限长文本；解题 Coder 重置对话
- [x] `coder_agent.py:_min_figures_for_phase` — eda 返回 0（不卡图片下限）
- [x] `coder_agent.py:_ensure_phase_skills` — eda 只预注入 `eda`，不注入 `figure-reporting`
- [x] `flows.py` — 数据准备 prompt 改为清洗落盘；求解 flows 不再含 eda
- [x] 基线 `expected.json` 已不含 `min_eda_png`（EDA 图不进论文插图预算）
- [x] `eda.md` 写死清洗文件名口径；诊断图标记为可选、不进论文

**Skill 端（后续）：**
- [ ] 诊断概览（相关性热力图、分布图）若需要，仍不进论文插图预算

## 上下文与结果交接（已落地）

- [x] Coder 每个 phase 独立 history，解释器与 `data_contract` 继续共享
- [x] Writer 每章节独立 history
- [x] Coordinator 将题面事实与执行约束拆成双通道
- [x] Writer 不再读取原始 `ques_all`，过程约束只发送给 Modeler/Coder
- [x] `phase_results/{phase}.json` 保存状态、有限事实、限制和真实产物指纹
- [x] Coder 失败时跳过对应 Writer，输出明确的未完成说明
- [ ] 根据完整基线结果决定是否进一步改为 metadata-only skill 预加载
- [ ] source inspection 与逐 sheet 数据血缘
- [ ] task manifest 与路由终态统一

## 内容质量修复（下一波：论文实质，未开始）

见 `iteration-plan.md` 阶段 2：

- [ ] 引用与文献：regex 对齐 + Writer prompt + 强制搜文献
- [ ] 符号表公式：`\(...\)` → `$...$` 预处理 + 模板修改
- [ ] Word 排版模板：`reference.docx` + `--reference-doc`
- [ ] 图片与图注：alt 中文描述 + Coder `figure-reporting` 强化
- [ ] Writer 章节完整性：空响应检测 + retry
