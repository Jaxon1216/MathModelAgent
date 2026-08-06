# 现状盘点（2026-08-07）

基于代码审查与任务 `20260802-105542-c916a31a` 的 trace / `res.md` / `res.docx` 分析。

## 1. 主链路架构

```
CoordinatorAgent → ModelerAgent → CoderAgent（+ skills catalog）
                                        ↓
                                  WriterAgent → UserOutput 拼接 res.md
                                        ↓
                                  md_2_docx() → res.docx
```

- **内容模板**：`backend/app/config/md_template.toml`（各章节写什么、多少字）
- **Writer 规范**：`backend/app/core/prompts/writer.py`
- **排版模板**：**无**。`md_2_docx()` 使用裸 Pandoc，未传 `--reference-doc`
- **CompTemplate**：目前仅 CHINA → 加载上述 TOML，与 Word 样式无关

## 2. 工程化对标

| 能力 | 现状 |
|------|------|
| Makefile | ❌ 无 |
| CI（GitHub Actions） | ❌ 无 |
| 单元测试 | ⚠️ 极弱：`app/tests/` 仅 1 个有效 unittest（`split_footnotes`），其余为手动脚本 |
| 覆盖率 | ❌ 无 pytest-cov |
| 基线 / golden 样例 | ❌ 无期望输出 diff |
| pre-commit | ❌ 无（仅有 Claude PostToolUse ruff/biome hook） |

## 3. 可观测性（已有，是优势）

| 组件 | 路径 / 说明 |
|------|-------------|
| Trace（结构化） | `backend/logs/traces/{task_id}.jsonl`，`trace_recorder.py` |
| Trace 事件 | task/phase、skill.load、tool.call、execute、react.turn、artifact、subtask.summary |
| 对话持久化 | `backend/logs/messages/{task_id}.json`（Redis 广播同时落盘） |
| 应用日志 | loguru → `backend/logs/*_error.log` |
| LLM 明细 | ⚠️ `llm.py` 仅 info 一行 preview，无 token/耗时/request 结构化落盘 |
| 产物 | `backend/project/work_dir/{task_id}/`（res.md、res.docx、png、csv） |

Trace 查询示例（见 `AGENTS.md`）：

```bash
jq -r '.event' backend/logs/traces/{task_id}.jsonl | sort | uniq -c
jq 'select(.event=="subtask.summary")' backend/logs/traces/{task_id}.jsonl
```

## 4. md → docx 管线（核心缺口）

```python
# backend/app/utils/common_utils.py — md_2_docx()
pypandoc.convert_file(
    source_file=md_path,
    to="docx",
    format="markdown+tex_math_dollars",
    extra_args=["--resource-path", work_dir, "--mathml", "--standalone"],
    # 缺少: --reference-doc
)
```

**结论**：不是「按 Word 模板匹配样式」，而是 Pandoc 默认样式直接导出。

## 5. 已知内容/质量问题（样例任务验证）

任务 ID：`20260802-105542-c916a31a`

### 5.1 引用与参考文献

- Writer 产出：`{[^1] 作者...}`（无冒号）
- `UserOutput.replace_references_with_uuid` 期望：`{[^1]: 内容}`（有冒号）
- **regex 零匹配** → `footnotes` 全空 → 文末「参考文献」无条目 → Word 原样显示大括号
- `search_papers` **未被调用**；trace 无 tool 记录；Writer 直接编造引用格式

### 5.2 符号表公式

- `md_template.toml` 与 Writer 输出使用 `\(...\)`
- Pandoc 格式为 `markdown+tex_math_dollars`，只认 `$...$`
- docx 中符号列渲染为字面量 `( i )` 而非数学符号

### 5.3 图片与图注

- Markdown 写法正确：`![q1_behavior_overview](q1_behavior_overview.png)`
- 图片已嵌入 docx，但 alt 为文件名 → Pandoc 生成 `ImageCaption` 显示文件名
- 无图编号（图1、图2）与交叉引用体系

### 5.4 段首缩进

- Pandoc 默认 `BodyText` 无首行缩进
- 需 Word reference template 定义正文样式（如首行缩进 2 字符）

### 5.5 Writer 章节缺失

- `res.json` 中 `ques2` / `ques3` / `ques4` 的 `response_content` 为空
- Coder 阶段成功（有 png/csv），摘要却写了四问结果 → 正文不完整

## 6. 与根目录 skills/ 的边界

| | 运行时（backend） | harness（根目录 skills/） |
|---|-------------------|---------------------------|
| Coder 技能 | `backend/app/core/skills/catalog/*.md` | — |
| 写作/排版模板 | **未接入** | `skills/5writing`（Typst/LaTeX，IDE 侧） |
| 本计划范围 | ✅ | ❌ 不在此计划内 |

## 7. 为什么要先铺护栏再改内容

上述问题分散在 Writer prompt、`UserOutput` 后处理、Pandoc 参数三处。没有自动化测试和基线，每次改 prompt 只能人肉跑任务 + 肉眼看 docx，无法判断回归。因此迭代顺序必须是：

**工程护栏 → 纯函数测试 → 内容修复 → E2E 评估**
