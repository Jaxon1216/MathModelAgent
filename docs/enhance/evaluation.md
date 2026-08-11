# 评测指标

## 设计原则

1. **只保留会持续变化的指标** — 一次修完就永远为 true 的检查（如「引用有没有冒号」）放进单测，不放进跑分卡
2. **不单独存参数表** — 阈值写死在 eval 脚本或 fixture 的 `expected.json`，不搞配置中心
3. **分三层** — 单测（确定性） / 跑任务后自动打分（结构性） / 人工抽检（语义）

---

## 借鉴的方案（抽象描述）

### 方案 A：会话结束聚合统计

Trace 在任务运行中流式写 JSONL；**任务结束时**一次性从事件列表算出汇总面板，不持久化中间状态。

典型汇总项：
- 总步数 / 各 phase 的 turn 数
- 工具调用次数（按 tool 名分组）
- 错误列表（类型、发生在哪一步）
- 模型调用次数、token、耗时（需在 LLM 层补事件后才可用）

实现方式：eval 脚本读完整 JSONL 后现算，或在 workflow 末尾 emit 一条 `task.summary` 事件携带聚合结果。

### 方案 B：冻结事件类型清单

维护一份**固定的 event 名称表**（如 `tool.call`、`execute.done`、`phase.end`），前后端和 eval 脚本共用同一份语义。新增事件只增不改，避免 trace 含义漂移。

本项目已有：`trace_recorder.py` + 前端 Trace pill。eval 只读现有 JSONL，不必重做采集。

### 方案 C：基线 trace 行为断言

对固定赛题录一条「已知合格」的运行记录，提取**行为模式**（调用了哪些工具、各 phase 出图下限、错误率上限），写入 `expected.json`。

之后每次改 Agent，用同一赛题重跑，对比行为是否退化——断言的是**模式**（如 min_png、max_error_rate），不是一次性格式细节。

### 方案 D：LLM 调用明细事件（可选，后期）

每次模型请求落盘：`latency_ms`、`usage`（input/output/total tokens）、caller（哪个 Agent）。用于按 Agent 分桶统计成本，以及发现某 Agent 空转/爆 token。

当前 `llm.py` 只有一行 log preview，需补 `llm.response` 类事件后再纳入跑分。

---

## 第一层：单测（改后处理必跑）

固定逻辑，修一次测一次，**不进 E2E 评分卡**：

- 引用 `{[^1] ...}` / `{[^1]: ...}` 解析
- `\(...\)` → `$...$` 预处理
- docx 转换 smoke（有图、有公式对象）

---

## 第二层：任务结束自动汇总（从 trace + work_dir 现算）

跑完任务后，脚本读 JSONL + work_dir，**一次性输出 scorecard JSON**（方案 A）。

### A. Agent 质量（稳定性 & 效率）

| 指标 | 来源 | 为何长期有用 |
|------|------|--------------|
| `execute_error_rate` | `execute.done` 中 error 占比 | 代码执行稳定性，随 prompt/技能变化 |
| `react_retry_total` | `react.reflect` 计数 | Coder 自我修正频率，过高说明 prompt 或环境有问题 |
| `turns_per_phase` | `subtask.summary.turns` | 完成子任务效率，防 Agent 空转 |
| `completion_blocked` | `react.completion_check.blocked_exit` | Coder 试图缺图交差，绘图链路回归 |
| `phase_fail` | `phase.end success=false` | 整阶段失败，硬红线 |
| `tool_calls_by_name` | `tool.call` 分布 | 行为画像（execute_code / load_skill / search_papers） |

### A2. LLM 成本（`llm_metrics`）

| 指标 | 来源 | 为何长期有用 |
|------|------|--------------|
| `llm_call_count` | `llm.response` 计数 | 总调用次数 |
| `total_tokens` / `total_latency_ms` | `llm.response` 聚合 | 成本与耗时 |
| `by_agent` | 按 Agent 分桶 | 定位哪个 Agent 爆 token |
| `by_model` | 按 model 分桶 | 多模型混用时的成本分布 |

### B. 绘图质量

| 指标 | 来源 | 为何长期有用 |
|------|------|--------------|
| `png_per_ques` | `subtask.summary.png_count` 按 phase | 每问是否持续出够图（当前规则 ≥2） |
| `png_total` | `artifact.created` kind=png | 全文图表量 |
| `image_coverage` | res.md 中 `![]()` 数 / work_dir png 数 | Writer 是否把 Coder 的图写进论文 |
| `duplicate_png_names` | 跨 phase 重复文件名 | Coder 是否重画已有图 |

「某工具是否被调用过」这类修完即永久 true 的门禁 → 改逻辑后用单测 + 人工看一次即可，不进跑分卡。

### C. 论文结构（导出前可机读）

| 指标 | 来源 | 为何长期有用 |
|------|------|--------------|
| `empty_sections` | res.json 各 key 的 content 长度=0 | ques 章节缺失会反复出现 |
| `ref_count` | res.md 中 `[^n]:` 脚注定义数 | 参考文献是否生成 |
| `in_text_cite_count` | 正文 `[^n]` 出现次数 | 引用是否进正文 |
| `alt_is_filename_ratio` | 图片 alt 是否等于 `.png` 文件名 | 图注质量启发式 |
| `docx_exists` / `docx_has_images` / `docx_has_math` | res.docx 解析（python-docx） | 导出管线没挂、图/公式是否嵌入 |

格式类细节（符号表语法等）→ 单测覆盖，不进跑分卡。

---

## 第三层：人工抽检（约 5 分钟）

机器难判、但对论文质量最关键：

- docx 段首缩进、三线表样式（reference.docx 是否生效）
- 图注中文是否贴切、分析段落是否 ≥3 行
- 摘要与正文 ques 章节是否一致
- 数值是否与 Coder stdout / csv 一致（幻觉）

---

## 基线行为断言（方案 C）

固定基线赛题 + `expected.json`，断言行为模式：

```json
{
  "must_call_tools": ["execute_code", "load_skill"],
  "min_png_per_ques": 2,
  "max_execute_error_rate": 0.15,
  "max_empty_sections": 0,
  "min_image_coverage": 0.8
}
```

失败时查 trace 哪 phase 退化，而不是盯已修好的格式细节。

唯一基线赛题：`backend/fixtures/problems/2024高教杯C题.json`  
期望指标：`backend/fixtures/baseline/2024高教杯C题/expected.json`

---

## 怎么跑

```bash
# 1. 单测
cd backend && make test

# 2. 跑一轮任务（本地 Redis + backend，或 docker-compose；前端或 API 均可）
# 记下 task_id

# 3. 打分（阶段 3 实现）
cd backend
make eval TASK_ID={task_id}
# 或带对比
uv run python scripts/eval_task.py --task-id {new_id} \
  --baseline fixtures/baseline/2024高教杯C题/expected.json \
  --compare {old_id} --save-scorecard
```

本地 `make test` + 改 Agent 后跑基线 + eval；不做 CI。

---

## 与现有 trace 的关系

`trace_recorder.py` 已覆盖大部分事件。eval 脚本 **只读 JSONL + work_dir**，不必改采集架构。

可选增强：
- workflow 末尾 emit `task.summary`（方案 A：聚合 stats 写入 trace 末行）
- `llm.py` 补 `llm.response` 事件（方案 D：token / latency / caller）
