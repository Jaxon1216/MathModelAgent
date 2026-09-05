# 同级项目参考

借鉴 `mma/` 同级仓库，提升论文画图、写作与运行效率。只抄机制，不换主链路（仍是 Coordinator → Modeler → Coder → Writer → Pandoc）。

按优先级 **S → B**。路径均为已验证的本机绝对路径。
使用时需详细下钻探查，实际为准，本文注释仅为参考，不要引入风险代码。

## S

### 1. mma-chart-templates

| | |
|---|---|
| 根目录 | `/Users/bytedance/code/Easton/mma/mma-chart-templates` |
| 参考 | `/Users/bytedance/code/Easton/mma/mma-chart-templates/agents.md` |
| | `/Users/bytedance/code/Easton/mma/mma-chart-templates/tornado_sensitivity/template.py` |
| | `/Users/bytedance/code/Easton/mma/mma-chart-templates/prediction_interval/template.py` |
| | `/Users/bytedance/code/Easton/mma/mma-chart-templates/multi_panel_combined/template.py` |
| 价值 | 运行时注入用的 matplotlib 模板（返回 `Figure`、不存盘、图注不进画布）。补齐 Tornado、预测区间、三联组合等竞赛图。注入前须改成现有 `COLORS`，去掉 `sns.set_theme()`。 |

### 2. MathModeling-skills

| | |
|---|---|
| 根目录 | `/Users/bytedance/code/Easton/mma/mathmodel-skill/MathModeling-skills` |
| 参考 | `/Users/bytedance/code/Easton/mma/mathmodel-skill/MathModeling-skills/.claude/skills/figure-table-planner/SKILL.md` |
| | `/Users/bytedance/code/Easton/mma/mathmodel-skill/MathModeling-skills/.claude/skills/solution-package-builder/SKILL.md` |
| | `/Users/bytedance/code/Easton/mma/mathmodel-skill/MathModeling-skills/.claude/skills/paper-section-writer/SKILL.md` |
| | `/Users/bytedance/code/Easton/mma/mathmodel-skill/MathModeling-skills/.claude/skills/consistency-auditor/SKILL.md` |
| | `/Users/bytedance/code/Easton/mma/mathmodel-skill/MathModel-Skill/packages/codex/skills/paper-formal-writer/scripts/check_paper_format.py` |
| 价值 | 图分 Type 1 诊断 / Type 3 论文；`frozen_numbers.json` 冻数字后再交给 Writer；材料包 + 一致性审计。不要搬人工 gate。同目录 `MathModel-Skill` 的 format 脚本可参考 Word 公式/章节检查。 |

### 3. Mrite

| | |
|---|---|
| 根目录 | `/Users/bytedance/code/Easton/mma/Mrite` |
| 参考 | `/Users/bytedance/code/Easton/mma/Mrite/Skill/CLAUDE.md` |
| | `/Users/bytedance/code/Easton/mma/Mrite/模板/` |
| 价值 | 国赛章节规范：摘要字数/一页、先算后画、图注在 caption、每图必须被正文引用、参考文献一一对应、编译后修排版。抽规则进 Writer / eval，不要把 XeLaTeX 换成默认导出。 |

## A

### 4. moban

| | |
|---|---|
| 根目录 | `/Users/bytedance/code/Easton/mma/moban` |
| 参考 | `/Users/bytedance/code/Easton/mma/moban/最新版！2026数学建模国赛标准论文Word模板.doc` |
| | `/Users/bytedance/code/Easton/mma/moban/2026数学建模国赛AI全自动自查表（标准版）.md` |
| 价值 | Pandoc `--reference-doc` 用国赛 Word 模板。自查表里可机器化的项（空章节、图表数值、灵敏度/检验、图文对应）进 `eval_task.py`。 |

### 5. nature-skills

| | |
|---|---|
| 根目录 | `/Users/bytedance/code/Easton/mma/nature-skills` |
| 参考 | `/Users/bytedance/code/Easton/mma/nature-skills/skills/nature-figure/static/core/contract.md` |
| | `/Users/bytedance/code/Easton/mma/nature-skills/skills/nature-figure/references/qa-contract.md` |
| | `/Users/bytedance/code/Easton/mma/nature-skills/skills/nature-writing/manifest.yaml` |
| 价值 | 画前先写一句话结论 + panel 证据链；渲染后 QA。`manifest.yaml` 按轴只加载所需片段，避免每阶段 dump 整份 skill。不要抄 Nature 英文腔。 |

## B

### 6. MathModelHub

| | |
|---|---|
| 根目录 | `/Users/bytedance/code/Easton/mma/MathModelHub` |
| 参考 | `/Users/bytedance/code/Easton/mma/MathModelHub/competitions/figure_style.py` |
| | `/Users/bytedance/code/Easton/mma/MathModelHub/competitions/问题一/generate_figures.py` |
| | `/Users/bytedance/code/Easton/mma/MathModelHub/docs/mcm_guide.md` |
| 价值 | 折线色盲配色、三联/四联尺寸、每图至少 3 行解读。美赛结构仅作对照。图内 `set_title()` 不要抄。 |

### 7. sci-box

| | |
|---|---|
| 根目录 | `/Users/bytedance/code/Easton/mma/sci-box` |
| 参考 | `/Users/bytedance/code/Easton/mma/sci-box/skills/scibox-figure/SKILL.md` |
| | `/Users/bytedance/code/Easton/mma/sci-box/skills/scibox-diagram/SKILL.md` |
| 价值 | 高级科研图配方（按需）；示意图的槽位/字数预算，可用 Python 画简化流程图。不要把 draw.io 链塞进 Coder。 |

### 8. Skill-Research-Figure

| | |
|---|---|
| 根目录 | `/Users/bytedance/code/Easton/mma/Skill-Research-Figure` |
| 参考 | `/Users/bytedance/code/Easton/mma/Skill-Research-Figure/README.md` |
| 价值 | 方法流程图的布局与配色原则（卡片、低饱和、突出贡献模块）。TikZ/Blender 不进运行时。 |

## HelloAgents · skill loader

不是论文质量参考，是 **技能加载实现** 参考。改 Coder `load_skill` / 预注入时读这里。

| | |
|---|---|
| 根目录 | `/Users/bytedance/code/Easton/mma/HelloAgents` |
| 参考 | `/Users/bytedance/code/Easton/mma/HelloAgents/hello_agents/skills/loader.py` |
| | `/Users/bytedance/code/Easton/mma/HelloAgents/hello_agents/skills/__init__.py` |
| 对照 | `/Users/bytedance/code/Easton/mma/MathModelAgent/backend/app/core/skills/registry.py` |
| 价值 | L1 只扫 frontmatter（`get_descriptions`）→ L2 按需 `load_skill` body → L3 `references/` / `scripts/`。当前 `SkillRegistry` 已实现 L1/L2；M2 不引入 L3。 |
