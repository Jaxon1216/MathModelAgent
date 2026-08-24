# 质量与工程化增强（Enhance）

本目录记录 **MathModelAgent 后端** 的质量提升与工程化迭代计划，供切分支、换新 Agent context 或人工 review 时接续上下文。

> **范围说明**：只涉及 `backend/` 主链路与 Docker 任务运行时；根目录 `skills/` 为 Cursor/harness 侧，不在此计划内。

## 文档索引

| 文件 | 内容 |
|------|------|
| [current-state.md](./current-state.md) | 现状盘点：观测、测试、导出管线、已知问题 |
| [iteration-plan.md](./iteration-plan.md) | 分阶段迭代计划、原因、验收标准、分支策略 |
| [evaluation.md](./evaluation.md) | 评测指标：哪些值得追、怎么跑、golden trace |
| [sibling-references.md](./sibling-references.md) | 同级项目借鉴：S–B 路径与价值 |
| [changelog.md](./changelog.md) | 质量迭代记录 |

## 协作方式

- **开发**：Agent / 开发者按 `iteration-plan.md` 串行推进
- **把关**：人工 review PR + 基线任务跑分（阶段 3 起）
- **上下文恢复**：新 session 先读本目录 → 看当前分支与 `git log` → 对照 plan 中「当前进度」章节

## 当前进度

| 阶段 | 状态 | 备注 |
|------|------|------|
| 阶段 0 · 工程护栏 | ✅ 完成 | Makefile / pytest（无 CI） |
| 阶段 1 · 纯函数基线 | ⬜ 未开始 | UserOutput / md 预处理 |
| 阶段 2 · 内容质量 | ⬜ 未开始 | 引用、公式、Word 模板、Writer/Coder |
| 阶段 3 · E2E 评估 | ✅ 完成 | 唯一赛题 2024高教杯C题 + eval 脚本 + scorecard 存档 |
| 阶段 4 · LLM 观测 | ✅ 完成 | `llm.response` trace + eval 聚合 token/延迟 |

*请在每阶段完成后更新上表。*

## 推荐分支

从 `reAct_coder`（或当时 main）切出：

```bash
git checkout -b feat/quality-harness
```

本系列改动与 Coder ReAct 实验解耦，独立分支便于 review 与回滚。
