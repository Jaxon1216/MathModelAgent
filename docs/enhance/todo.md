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

## EDA / 数据预处理解耦（后续改造）

背景：当前 EDA 阶段三处联动强制画图，产出的诊断图（直方图、箱线图）不应算入论文插图预算。

**脚本端（先改）：**
- [ ] `coder_agent.py:_min_figures_for_phase` — eda 改为返回 0（不卡图片下限）
- [ ] `coder_agent.py:_ensure_phase_skills` — eda 不预注入 `figure-reporting`（诊断图不被论文追踪）
- [ ] `flows.py` — EDA prompt 去掉"可视化"，改为"数据清洗并保存清洗后数据到当前目录"
- [x] 基线 `expected.json` 已不含 `min_eda_png`（EDA 图不进论文插图预算）

**Skill 端（后续改）：**
- [ ] `eda.md` 重构为数据预处理 skill，强化清洗流程：缺失值处理 → 数据类型转换 → 异常值处理 → 特征工程
- [ ] 诊断概览（相关性热力图、分布图）标记为"可选，不进论文"（参考 MathModelHub `data-preprocess/SKILL.md`）

## 内容质量修复（阶段 2，未开始）

见 `iteration-plan.md` 阶段 2：

- [ ] 引用与文献：regex 对齐 + Writer prompt + 强制搜文献
- [ ] 符号表公式：`\(...\)` → `$...$` 预处理 + 模板修改
- [ ] Word 排版模板：`reference.docx` + `--reference-doc`
- [ ] 图片与图注：alt 中文描述 + Coder `figure-reporting` 强化
- [ ] Writer 章节完整性：空响应检测 + retry
