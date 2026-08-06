# 迭代计划：质量与工程化增强

## 目标

1. **内容质量**：论文引用、公式、图注、章节完整性、docx 排版达标
2. **迭代稳定性**：Makefile + 测试 + 基线评估，改一处可自动验证没退步（无 CI）
3. **可观测性补强**：在现有 trace 基础上补 LLM 请求明细（可选）

## 原则

- **串行推进**：阶段 N 验收通过再进 N+1
- **测试先行**：阶段 2 每项内容修复须先有失败用例（红灯）再改代码（绿灯）
- **范围聚焦 backend**：不改 `skills/`，不接入 Typst/LaTeX harness
- **人工把关**：每阶段结束由 maintainer review +（阶段 3 起）跑基线赛题

---

## 分支策略

### 建议：新开分支，不在 `reAct_coder` 上继续

| 选项 | 建议 |
|------|------|
| 继续 `reAct_coder` | ❌ 该分支聚焦 Coder ReAct/技能/trace，与本计划（工程护栏 + 导出 + Writer）主题不同，混杂 commit 难 review |
| 新建 `feat/quality-harness` | ✅ 推荐。从当前 `reAct_coder` 或 merge 后的 main 切出，独立 PR |

```bash
git checkout reAct_coder   # 或 main
git pull
git checkout -b feat/quality-harness
```

### 提交粒度建议

- 阶段 0：1 个 commit（Makefile + pytest + CI）
- 阶段 1：按模块拆分（UserOutput / md 预处理 / docx 断言）
- 阶段 2：每项修复 1 commit（引用 / 公式 / template / Writer / Coder）
- 阶段 3：eval 脚本 + 基线 fixture 各 1 commit

---

## 阶段 0 · 工程护栏最小集

**工期**：0.5–1 天  
**目标**：`make check` 一键跑通 lint + test

### 交付物

- [ ] `backend/Makefile`
  - `make lint` → ruff check
  - `make format` → ruff format
  - `make test` → pytest
  - `make typecheck` → pyright（可选，CI 可先 warning）
  - `make check` → lint + test
- [ ] `pyproject.toml` dev 依赖：`pytest`, `pytest-cov`, `pytest-asyncio`
- [ ] 收敛 `app/tests/`：删除或改写 print 式脚本为 pytest；保留/迁移 `test_common_utils.py`
### 验收

```bash
cd backend && make check   # 全部通过
```

暂不卡覆盖率阈值。不做 GitHub Actions / CI。

---

## 阶段 1 · 纯函数基线（无需 LLM）

**工期**：1 天  
**目标**：后处理与导出链路的确定性逻辑有测试保护

### 1.1 UserOutput 引用链路

**文件**：`backend/app/models/user_output.py`

| 用例 | 输入 | 期望 |
|------|------|------|
| 无冒号引用 | `{[^1] Author (2020)...}` | 解析成功，生成 `[^1]` 脚注 |
| 有冒号引用 | `{[^1]: Author...}` | 同上（兼容两种格式） |
| 去重 | 相同内容两次引用 | UUID 合并，编号递增 |
| 参考文献节 | 两章各一引用 | `append_footnotes_to_text` 输出 2 条 |

**同步修改**：统一 Writer prompt 与 regex（阶段 2 实施时可 TDD：先写测试红灯）

### 1.2 Markdown 预处理（新建函数）

**建议位置**：`backend/app/utils/md_preprocess.py`

- `\(...\)` → `$...$`（表格内符号）
- 可选：图片 alt 占位规范化

### 1.3 md_2_docx 集成

**文件**：`backend/app/utils/common_utils.py`

- 转换前调用预处理
- 预留 `--reference-doc` 参数位（阶段 2 填入真实 template）

### 1.4 docx 结构断言（可选，依赖 python-docx）

- golden `res.md` fixture → 转换 → 断言图片数、段落数、是否含 footnote

### 验收

```bash
cd backend && make test   # 新增用例全部绿
```

---

## 阶段 2 · 内容质量修复

**工期**：2–3 天  
**前提**：阶段 1 测试已绿  
**方式**：每项「测试红灯 → 改代码 → 测试绿灯 → commit」

### 2.1 引用与文献

| 项 | 改动 |
|----|------|
| regex 对齐 | `UserOutput` 兼容 `{[^n] ...}` 与 `{[^n]: ...}` |
| Writer prompt | 统一为一种格式；正文用 `[^n]`，参考文献列表规范 |
| 强制搜文献 | `RepeatQues` 阶段 `tool_choice` 强制 `search_papers`，或拆成 search → write 两步 |
| WriterAgent | 支持多轮 tool call（当前只处理第一个 tool call） |

### 2.2 符号表公式

| 项 | 改动 |
|----|------|
| `md_template.toml` | 示例改为 `$x$` 而非 `\( x \)` |
| Writer prompt | 明确要求符号表用 `$...$` |
| 预处理 | `\(...\)` → `$...$` 兜底 |

### 2.3 Word 排版模板

| 项 | 改动 |
|----|------|
| 新增 | `backend/app/templates/reference.docx` |
| 样式 | 正文首行缩进 2 字符；Heading 1/2/3；三线表 Table Style；Caption |
| `md_2_docx` | `--reference-doc=app/templates/reference.docx` |

> reference.docx 可用 Word 手工制作，或用 Pandoc 生成基准再微调样式。

### 2.4 图片与图注

| 项 | 改动 |
|----|------|
| Coder `figure-reporting` | 每张图 `print` 建议中文图题 |
| Writer prompt | `![图1 行为构成概览](q1_behavior_overview.png)`，alt 用中文描述 |
| 可选 | pandoc-crossref 图编号（后期） |

### 2.5 Writer 章节完整性

| 项 | 改动 |
|----|------|
| 空响应检测 | `set_res` 前校验 `response_content` 非空 |
| ques2–4 为空 | 排查 Writer timeout / 上下文过长 / 异常吞掉 |
| workflow | 空章节触发 retry 或告警 |

### 验收

- 基线任务 `20260802-105542-c916a31a` 同类赛题重跑：
  - 参考文献 ≥ 1 条且 Word 可点击/上标
  - 符号表为公式而非 `( i )`
  - docx 正文有首行缩进
  - 图注为中文非文件名
  - ques2–4 正文非空

---

## 阶段 3 · E2E 评估 Harness

**工期**：持续  
**目标**：固定赛题 + 自动打分，替代纯人肉验收

### 交付物

- [ ] `backend/fixtures/baseline/`：1–2 个小型赛题（附件 + 期望指标 JSON）
- [ ] `backend/scripts/eval_task.py`：读取 trace + work_dir，输出评分卡

### 评分维度

详见 [evaluation.md](./evaluation.md)。核心原则：**只追会持续变化的指标**（错误率、出图覆盖、空章节、turns），一次修好的格式 bug 放单测不进评分卡。

Golden trace 断言示例见 `evaluation.md`。

### 验收

```bash
cd backend && make eval BASELINE=fixtures/baseline/social-media/
# 输出 scorecard，与 expected.json 对比
```

---

## 阶段 4 · LLM 层观测（可选）

**文件**：`backend/app/core/llm/llm.py`

- 每次 `chat()` 落盘：model、latency_ms、prompt_tokens、completion_tokens、tool_calls
- 路径：`logs/llm/{task_id}.jsonl` 或接入 trace `llm.response` 事件
- 用途：prompt A/B、成本分析、debug 空响应

---

## 依赖关系图

```
阶段 0（Makefile/CI）
    ↓
阶段 1（纯函数测试）
    ↓
阶段 2（内容质量：引用/公式/template/Writer/Coder）
    ↓
阶段 3（E2E eval + 基线）
    ↓
阶段 4（LLM 日志，可与 3 并行）
```

**不可跳过阶段 0–1 直接大改 prompt**，否则无法证明改动有效。

---

## 风险与决策记录

| 决策 | 原因 |
|------|------|
| 不用 skills/5writing Typst 模板 | 那是 harness 侧；后端主链路是 md→docx，应补 reference.docx |
| 先不卡覆盖率 80% | 当前 codebase 几乎无测试，先建立习惯再提阈值 |
| 基线赛题选 social-media 类 | 已有完整 trace 与 work_dir 可对照 |
| Writer 多 tool call 放在阶段 2 | 影响 RepeatQues 文献质量，与引用修复同批 |

---

## 新 Context 恢复清单

1. 读 `docs/enhance/README.md` 看「当前进度」表
2. `git branch` 确认在 `feat/quality-harness`
3. `cd backend && make check`
4. 对照本文档未完成 checkbox 继续

---

## 变更日志

| 日期 | 变更 |
|------|------|
| 2026-08-07 | 初版：现状盘点 + 五阶段计划 + 分支策略 |
