# AGENTS.md

面向在本仓库改代码的 Agent。命令与代码风格见 [`CLAUDE.md`](CLAUDE.md)。

## 项目是什么

**MathModelAgent** 是 GitHub 上的开源数学建模自动化项目。

用户本地/Docker 跑后端 + 前端，上传赛题与数据，多 Agent 协作完成建模、写代码、出图、写论文，最终导出 `res.md` / `res.docx`。

主链路：

```
Coordinator → Modeler → Coder → Writer → res.md → Pandoc → res.docx
```

- 后端：`backend/`（FastAPI + Redis + 本地 Jupyter 解释器）
- 前端：`frontend/`（Vue 3）
- 质量迭代计划：`docs/enhance/`

## 两套 Skills，别混

| | 运行时（改这个影响任务） | 根目录 `skills/`（IDE/harness 用） |
|---|--------------------------|-------------------------------------|
| 路径 | `backend/app/core/skills/catalog/*.md` | `skills/` |
| 谁读 | Docker 里的 **CoderAgent** | Cursor 侧分阶段流程 |
| 关系 | 互不相通，除非手动拷进 catalog | 不会自动进 Coder |

改 Coder 行为 → `catalog/`、`coder_agent.py`、`matplotlib_setup.py`。  
Coder 只有 `execute_code`，参考代码须写在 skill 正文里。

当前 catalog：`eda`、`visualization`、`figure-reporting`、`mathematical-modeling`、`sensitivity-analysis`。

## 关键目录

| 路径 | 用途 |
|------|------|
| `backend/app/core/workflow.py` | 任务主流程 |
| `backend/app/core/prompts/` | 各 Agent prompt |
| `backend/app/config/md_template.toml` | Writer 章节内容模板（不是 Word 样式） |
| `backend/app/utils/common_utils.py` | `md_2_docx()` 导出 |
| `backend/project/work_dir/{task_id}/` | 任务产物 |
| `backend/logs/traces/{task_id}.jsonl` | 结构化 trace |
| `backend/logs/messages/{task_id}.json` | 对话记录 |

## 调试一轮任务

改 Coder / Writer / workflow 后，跑完任务对照 trace：

```bash
jq -r '.event' backend/logs/traces/{task_id}.jsonl | sort | uniq -c
jq 'select(.event=="subtask.summary")' backend/logs/traces/{task_id}.jsonl
```

## 测试与 E2E 评估

### 工程护栏（每次改完跑）

```bash
cd backend && make check    # lint + test 一键
```

### M1 Modeler 分层验证

```bash
cd backend
make test-modeler           # 纯本地 Modeler 契约、扫描、桥接测试
make smoke-modeler          # 真实模型并发 3 次；会追加 experiment-log.md
```

普通 `make test` 排除 `live_modeler`，不会意外调用真实模型。完整 E2E 只在
`docs/enhance/roadmap.md` 的当前阶段验收时运行一次。
Modeler 契约修复次数通过 `.env.dev` 的 `MODELER_MAX_REPAIR_ATTEMPTS` 配置，
接受任意非负整数，默认 3；不要与 Provider 网络重试或 Coder 最大轮数混用。

### 例题 Fixture（仅 2024 高教杯 C 题）

评测与构造 `Problem` 只用这一道题，不要再加第二套 fixture。

```python
from app.tests.conftest import load_problem_fixture, list_problem_fixtures

fixture = load_problem_fixture("2024高教杯C题")
# fixture["ques_all"]        → 完整题目文本
# fixture["data_files"]      → 数据文件清单
# fixture["expected_ques_count"] → 3

from app.schemas.request import Problem
problem = Problem(task_id=..., ques_all=fixture["ques_all"])
```

Fixture：`backend/fixtures/problems/2024高教杯C题.json`  
阈值：`backend/fixtures/baseline/2024高教杯C题/expected.json`

### 质量基线（唯一对照）

唯一保留的 E2E 基线任务是 **`20260812-163504-32b726a1`**（2026-08-12）。ques 各 4 张图、`image_coverage=1.0`、无空章节、Word 含图与公式。不要把同日更早的 `044127` / `070143` 当基线。

| 路径 | 内容 |
|------|------|
| `backend/fixtures/baseline/2024高教杯C题/scorecards/20260812-163504-32b726a1.json` | 评分卡（进 git） |
| `backend/project/work_dir/20260812-163504-32b726a1/` | 论文与图（本地产物，不进 git） |
| `backend/logs/traces/20260812-163504-32b726a1.jsonl` | 该轮 trace（不进 git） |

### E2E 评分卡（跑完任务后）

```bash
cd backend
# 打分 + 回归检测 + 保存 scorecard
make eval TASK_ID={task_id}

# 或手动指定参数
uv run python scripts/eval_task.py --task-id {task_id} \
  --baseline fixtures/baseline/2024高教杯C题/expected.json \
  --save-scorecard

# 与质量基线对比
uv run python scripts/eval_task.py --task-id {new_task_id} \
  --baseline fixtures/baseline/2024高教杯C题/expected.json \
  --compare 20260812-163504-32b726a1
```

新任务 scorecard 写入：`backend/fixtures/baseline/2024高教杯C题/scorecards/{task_id}.json`

当前阶段的验收以 `docs/enhance/roadmap.md` 为准，真实任务证据只追加到
`docs/enhance/experiment-log.md`。旧评分口径保存在
`docs/enhance/archive/evaluation.md`，不得把它当作当前阶段的唯一门禁。

### 分阶段迭代闭环

改 Agent / prompt / workflow 时按此流程：

1. **定范围** — 阅读 `target-state.md`、`roadmap.md` 和 `decisions.md`；只改当前阶段允许的文件。
2. **护栏** — 补当前阶段的聚焦测试并运行 `cd backend && make check`。
3. **跑证据** — 仅在 roadmap 的完成定义要求时，用固定的 2024 高教杯 C 题和固定模型配置运行任务，记下 `task_id`。
4. **存档** — 运行 `make eval TASK_ID={task_id}` 保存 scorecard，并把结论和配置追加到 `experiment-log.md`。
5. **验收后推进** — 只有 roadmap 的完成定义满足后才 commit 并开启下一阶段；`regression_check.all_pass` 只是辅助诊断，不能替代阶段验收。

**退化时查什么日志：**

```bash
# 事件分布
jq -r '.event' backend/logs/traces/{task_id}.jsonl | sort | uniq -c

# 哪一 phase 失败
jq 'select(.event=="phase.end" and .payload.success==false)' backend/logs/traces/{task_id}.jsonl

# Coder 执行错误
jq 'select(.event=="execute.done" and .payload.error!=null)' backend/logs/traces/{task_id}.jsonl

# ReAct 重试
jq 'select(.event=="react.reflect")' backend/logs/traces/{task_id}.jsonl

# 每 phase 出图与 turns
jq 'select(.event=="subtask.summary")' backend/logs/traces/{task_id}.jsonl

# LLM token / 延迟
jq 'select(.event=="llm.response")' backend/logs/traces/{task_id}.jsonl
```

**关注指标：**

| 维度 | 关键字段 | 期望 |
|------|----------|------|
| Agent | `execute_error_rate`, `phase_fail`, `react_retry_total` | 错误率低、无 phase 失败 |
| LLM | `total_tokens`, `total_latency_ms`, `by_agent` | 成本可控、无异常爆 token |
| 绘图 | `png_per_phase` (ques* >=2), `image_coverage` (>=0.8) | 每问有图、Writer 引用图 |
| 论文 | `empty_section_count` (0), `ref_count`, `in_text_cite_count` | 无空章节、有参考文献 |
| 导出 | `docx_has_images`, `docx_has_math` | Word 含图片与公式对象 |
| 回归 | `regression_check.all_pass` | 全部 pass 才可 commit |

## 边界

- **开源本地项目**：不做 CI/GitHub Actions，改完本地 `make check` 或手动 lint/test 即可
- 不改 `frontend/src/components/ui/`（shadcn-vue 生成）
- 不改 `.env` 里的实际密钥
- 根目录 `skills/` 与后端主链路无关，除非用户明确要求

## 文档

- [`CLAUDE.md`](CLAUDE.md) — 命令、结构、代码风格
- [`docs/enhance/`](docs/enhance/) — 质量提升计划与现状
