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

## 2026-09-04 - M1 结构切片（未验收）

- 新增 `domain`、`agents`、`prompts`、`runtime/llm` 和 `orchestration` 中的
  Modeler 边界；旧 Coder 仍通过唯一兼容边界消费文本交接。
- `make check`：18 passed，1 skipped；新目录和 `backend/tests/` 的 Pyright：0 errors。
- 尚未运行固定模型配置下的三次真实 Modeler 或一次完整 E2E，因此 M1 仍未完成。

## 2026-09-04 - M1 输入与验证基建（未验收）

- DataCatalog 改为 sheet 级输入表，区分 `input_tables` 和
  `output_templates`，同时保留规范列名与原始表头。
- Modeler phase 覆盖附件扫描与 LLM 调用；Provider、空响应、非法计划和取消
  均有独立终态。新增固定 smoke fixture、三路并发 live marker 和旧 Coder
  handoff 契约测试。
- `make test-modeler`：23 passed，2.96 秒；`make check`：33 passed、
  1 skipped、1 live deselected；M1 聚焦 Pyright：0 errors。
- 本轮未执行真实 `make smoke-modeler`，也未执行完整 E2E；M1 仍未完成。

## 2026-09-04T14:20:17.008441+00:00 - Modeler 真实 smoke

- git revision：`71db765f4893e1f8829091c74179e69b18ae5a67`
- fixture：`fixtures/modeler/2024高教杯C题.json`，SHA256 `b24864568fefb5097167994dfd670311820c483df6491a53e4f2575c884a14ea`
- 模型：`deepseek-v4-pro`，API 类型：`openai-chat`，配置指纹：`2022b51e2dd2505a`
- 结果：`all_pass=true`

| run | success | latency_ms | attempts | raw_output_passed | failure |
|---:|---|---:|---:|---|---|
| 1 | true | 97566 | 1 | true |  |
| 2 | true | 121131 | 1 | true |  |
| 3 | true | 170199 | 1 | true |  |

## 2026-09-04 - 开发态 smoke 说明

- 上述三次 smoke 使用了未提交工作树，虽然 3/3 首轮通过，但
  `git revision=71db765` 不能完整标识当时源码，因此不作为最终固定版本证据。
- runner 已补 `worktree_dirty` 字段，handoff 长度校验也已移入 ModelPlan
  验证阶段；后续应在 clean commit 上重跑。
- 当前 `make test-modeler`：26 passed，2.92 秒；`make check`：36 passed、
  1 skipped、1 live deselected；M1 聚焦 Pyright：0 errors。

## 2026-09-04T14:48:20.871969+00:00 - Modeler 真实 smoke

- git revision：`c6193d04904c660c5ed0367ea7e9a5c1fdf9dbbf`
- worktree dirty：`false`
- fixture：`fixtures/modeler/2024高教杯C题.json`，SHA256 `b24864568fefb5097167994dfd670311820c483df6491a53e4f2575c884a14ea`
- 模型：`deepseek-v4-pro`，API 类型：`openai-chat`，配置指纹：`2022b51e2dd2505a`
- 结果：`all_pass=true`

| run | success | latency_ms | attempts | raw_output_passed | failure |
|---:|---|---:|---:|---|---|
| 1 | true | 242554 | 1 | true |  |
| 2 | true | 145927 | 1 | true |  |
| 3 | true | 146403 | 1 | true |  |

## 2026-09-04 - Modeler 修复次数配置化（待固定版本 smoke）

- 新增 `MODELER_MAX_REPAIR_ATTEMPTS`，默认 1、范围 0-3；仅控制 JSON/契约
  修复，不改变 Provider 网络重试或 Coder 轮数。
- `make test-modeler`：29 passed；`make check`：39 passed、1 skipped、
  1 live deselected；M1 聚焦 Pyright：0 errors。
- 上一条 clean smoke 使用默认一次修复且 3/3 首轮通过；本配置接线提交后仍需
  以新 clean revision 重跑，才能作为当前源码的最终 smoke 证据。
