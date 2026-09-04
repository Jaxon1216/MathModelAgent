# 实验日志

按时间顺序追加证据。不要为适配新理论改写旧记录；需要更正时追加新证据。

## 2026-08-12 - 完整运行基线

- 代码锚点：`1d71df1`；`2e555ad` 的 `backend/app` 相同，并保存了评分卡。
- 任务：`20260812-163504-32b726a1`。
- 观测产物：`res.md`、`res.docx`、图片和公式对象齐全；`phase_fail=0`，`image_coverage=1.0`。
- 注意：旧评分卡因要求并未发生的 `load_skill` tool call 而记录
  `regression_check.all_pass=false`。这是评分规则错配，不是运行时失败证据。

## 2026-09-03 - 已归档的 EDA 预算实验

- checkpoint：`6670d1b`。
- 任务 `20260903-143558-929e7890` 和 `20260903-152345-3fbc9a28` 分别在
  `203721/200000` 和 `211532/200000` token 时触发 EDA 熔断。
- 结论：累积式 ReAct history 与 phase 全局 token 上限不兼容。这是历史证据，不是当前实现。

## 2026-09-03 - 已归档的 Modeler tool-call 实验

- checkpoint：`6670d1b`。
- 任务 `20260903-145530-d8cb5f73` 和 `20260903-153509-77cc5ea1` 三次
  Modeler 尝试均为 `no_tool_call`，每次耗尽 1600 token reasoning allowance。
- 结论：provider 强制 tool call 不能作为 M1 的全链路强依赖。

## 2026-09-04 - 建立重建起点

- 归档分支：`archive/workflow-enhance-wip-20260904`，commit `6670d1b`。
- 当前分支：`rebuild/modeler-first`，从 `2e555ad` 创建。
- 未从 archive 拷贝运行时代码；当前范围由 `roadmap.md` 的 M1 定义。
