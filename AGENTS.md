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

### 例题 Fixture（三个已解析，直接加载构造 Problem）

```python
from app.tests.conftest import load_problem_fixture, list_problem_fixtures

# 三个例题：2023华数杯C题 / 2024高教杯C题 / 2025五一杯C题
fixture = load_problem_fixture("2025五一杯C题")
# fixture["ques_all"]        → 完整题目文本
# fixture["data_files"]      → 数据文件清单
# fixture["expected_ques_count"] → 子问题数

from app.schemas.request import Problem
problem = Problem(task_id=..., ques_all=fixture["ques_all"])
```

Fixture 文件：`backend/fixtures/problems/{name}.json`

### E2E 评分卡（跑完任务后）

```bash
cd backend
uv run python scripts/eval_task.py --task-id {task_id}
uv run python scripts/eval_task.py --task-id {task_id} \
  --baseline fixtures/baseline/social-media/expected.json
```

评分维度见 `docs/enhance/evaluation.md`：Agent 质量、绘图质量、论文结构。

## 边界

- **开源本地项目**：不做 CI/GitHub Actions，改完本地 `make check` 或手动 lint/test 即可
- 不改 `frontend/src/components/ui/`（shadcn-vue 生成）
- 不改 `.env` 里的实际密钥
- 根目录 `skills/` 与后端主链路无关，除非用户明确要求

## 文档

- [`CLAUDE.md`](CLAUDE.md) — 命令、结构、代码风格
- [`docs/enhance/`](docs/enhance/) — 质量提升计划与现状
