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

## 2026-09-04T15:15:09.199760+00:00 - Modeler 真实 smoke

- git revision：`0727178551ab1ddbfa4823cf79e0257d2855bc4b`
- worktree dirty：`true`
- M1 source dirty：`false`
- fixture：`fixtures/modeler/2024高教杯C题.json`，SHA256 `b24864568fefb5097167994dfd670311820c483df6491a53e4f2575c884a14ea`
- 模型：`deepseek-v4-pro`，API 类型：`openai-chat`，配置指纹：`185177e1ac83d746`
- 结果：`all_pass=true`

| run | success | latency_ms | attempts | raw_output_passed | failure |
|---:|---|---:|---:|---|---|
| 1 | true | 121355 | 1 | true |  |
| 2 | true | 205678 | 1 | true |  |
| 3 | true | 152576 | 1 | true |  |

## 2026-09-04T15:36:25.089018+00:00 - Modeler 真实 smoke

- git revision：`649ee789b03c58cd8b77817d72fad9023957e580`
- worktree dirty：`true`
- M1 source dirty：`false`
- fixture：`fixtures/modeler/2024高教杯C题.json`，SHA256 `b24864568fefb5097167994dfd670311820c483df6491a53e4f2575c884a14ea`
- 模型：`deepseek-v4-pro`，API 类型：`openai-chat`，配置指纹：`79f80944c583d544`
- 结果：`all_pass=true`

| run | success | latency_ms | attempts | raw_output_passed | failure |
|---:|---|---:|---:|---|---|
| 1 | true | 144473 | 1 | true |  |
| 2 | true | 197216 | 1 | true |  |
| 3 | true | 211317 | 1 | true |  |

## 2026-09-05 - M1 固定配置完整 E2E（门禁未通过）

- task id：`20260905-095821-1cf855ad`。
- git revision：`0cc1611004759e45c8712f6c417c097a3b855ece`；工作树和 M1
  source 均为 dirty，具体 fixture、输入文件 SHA256 和配置快照已写入 trace
  的 `fixture.run` 事件。
- fixture：`fixtures/problems/2024高教杯C题.json`；四个 Agent 均使用
  `deepseek-v4-pro` / `openai-chat`；配置指纹 `fbd58cbb717ee7a6`。
- 本地护栏：`make test-modeler` 29 passed；`make check` 49 passed、
  1 skipped、1 live deselected。
- runner 成功生成 `res.md` 和 `res.docx`；DOCX 含图片和公式对象。
- scorecard：
  `fixtures/baseline/2024高教杯C题/scorecards/20260905-095821-1cf855ad.json`；
  trace：`logs/traces/20260905-095821-1cf855ad.jsonl`。
- 评分结果：`regression_check.all_pass=false`。`phase_fail=0`、
  `execute_error_rate=0.079`、`image_coverage=0.818`，但
  `empty_section_count=1`，空章节为 `ques3`。
- 根因证据：Q3 Coder 成功并生成两张图；Writer 连续两轮返回
  `search_papers` tool call，当前 Writer 只处理首轮工具，第二轮直接读取空
  `content`，最终 `res.json.ques3.response_content=""`。这是基线 Writer
  多工具循环缺口，不是 Modeler 或 Q3 求解失败。
- 按路线图，本轮不运行第二次 E2E，不启动 M1.5；M1 保持未完成。

## 2026-09-05 - M1 Writer 修复后 E2E（人工中断）

- task id：`20260905-131531-6f928758`；git revision：
  `0cc1611004759e45c8712f6c417c097a3b855ece`，工作树和 M1 source 均为 dirty。
- 四个 Agent 均使用 `deepseek-v4-pro` / `openai-chat`；配置指纹
  `a27d3affa9302ccd`，Writer 工具轮数上限 2、搜索调用上限 4、OpenAlex
  超时 15 秒。
- 运行前护栏：`make test-modeler` 29 passed；`make check` 60 passed、
  1 skipped、1 live deselected；本次涉及文件 Pyright 0 errors。
- 运行完成 Coordinator、Modeler、EDA 和 EDA Writer；Q1 已生成两份结果表、
  4 张图和多项中间证据，但第三次 Coder 执行错误触发 retry 上限，进入 Q1
  Writer 后由用户中断。
- 中断时 trace 有 90 次 LLM response、84 次 execute、5 次 reflect、1 个
  subtask.summary；未生成 `res.md` 或 `res.docx`，未生成 scorecard。
- 本轮属于人工中断证据，不构成 M1 成功或失败验收；按单次 E2E 约束不自动重跑，
  M1 保持开启，不启动 M1.5/M2。

## 2026-09-05 - Writer warning 与 Coder partial 语义 E2E

- task id：`20260905-140132-e19ba578`；四个 Agent 均使用
  `deepseek-v4-pro` / `openai-chat`；配置指纹 `a27d3affa9302ccd`。
- 运行前护栏：`make test-modeler` 29 passed；`make check` 62 passed、
  1 skipped、1 live deselected；全量 Pyright 0 errors。
- runner 成功生成 `res.md`（约 44 KB）和 `res.docx`（约 1.1 MB）。
  论文 11 个预期章节全部存在且非空，Word 含图片与公式。
- Coder 状态：Q1 `success`；EDA、Q2、Q3、敏感性分析在 retry 耗尽后为
  `partial`。Q2 保留 4 个可读产物、3 张图和 20 条成功执行指标；Q3 虽无
  新增图表，但保留 20 条成功执行指标。最后错误只写入 `coder.result`
  trace，未进入论文正文。
- Writer 所有章节均生成成功。论文检索与引用白名单正常，最终有 8 条参考文献、
  10 次正文引用；正文中无 `KeyError`、`Traceback`、检索错误或未完成占位。
- scorecard：
  `fixtures/baseline/2024高教杯C题/scorecards/20260905-140132-e19ba578.json`；
  trace：`logs/traces/20260905-140132-e19ba578.jsonl`。
- 指标：`phase_fail=0`、`missing_phase_end_count=0`、
  `missing_subtask_summary_count=0`、`empty_section_count=0`、
  `execute_error_rate=0.085`、`image_coverage=1.0`、9/9 图片均被引用。
  总计 137 次 LLM 调用、7,785,303 tokens。
- `regression_check.all_pass=false` 的唯一原因是 Q3 独立图片数为 0，低于原有
  `min_png_per_ques=2`。该项作为论文质量缺口保留，不阻止本轮完整交付，也不
  将 `partial/degraded` 自动升级为新的硬门禁。

## 2026-09-05 - 固定 Fixture E2E（数据清洗 schema 失败）

- task id：`20260905-195753-0d9d319b`；git revision：
  `f03886c51ffce30f1e54e54a280f2cc15756a31f`，运行开始时工作树和 M1 source
  均为 clean。
- fixture：`fixtures/problems/2024高教杯C题.json`，SHA256
  `832e9fa814a1b1b2d58ed28b53f1564abc2d8b6a38d9c398a0ed0e44b0c4c9b3`；
  四个 Agent 均使用 `deepseek-v4-pro` / `openai-chat`，配置指纹
  `a27d3affa9302ccd`。
- 运行前护栏：`make test-modeler` 为 37 passed；`make check` 为
  223 passed、1 skipped、1 deselected。
- `task_facts`、`task_outline` 和 4 个输入 Sheet 的 `data_profile` 均已通过；
  `task_outline` 正确声明 `ques3` 依赖 `ques2`。随后 `data_cleaning` 的首个
  规划请求耗时 239,031 ms 后返回，确定性 schema 校验拒绝
  `normalize-crop-catalog-keys`，原因是该操作只能规范化画像中声明的关联键。
- 因此任务在 `data_cleaning/schema` 终止，未进入 Modeler 最终 QuestionPlan、
  Coder、Writer 或 DOCX 导出；没有 `res.md`、`res.docx`、图片或
  `execute_code` 调用。这是 M1.5 数据清洗规划与真实模型响应的兼容缺口，不是
  Coder/Writer 退化。
- scorecard：
  `fixtures/baseline/2024高教杯C题/scorecards/20260905-195753-0d9d319b.json`；
  trace：`logs/traces/20260905-195753-0d9d319b.jsonl`。与唯一质量基线
  `20260812-163504-32b726a1` 的显式比较已写入 scorecard：
  `phase_fail=1`、`empty_section_count=11`、`image_coverage=0`，故
  `regression_check.all_pass=false`。M1 保持开启，后续修复仅应收口该
  CleaningPlan 合法操作与真实 profile 的兼容性。

## 2026-09-05 - 固定 Fixture E2E（清洗 Repair schema 失败）

- task id：`20260905-212110-ecd33559`；git revision：
  `f03886c51ffce30f1e54e54a280f2cc15756a31f`，工作树和 M1 source 均为 dirty，
  包含 CleaningPlan scope 违规的有界重规划修复。fixture、模型配置和指纹与上一轮
  相同：`2024高教杯C题`、四个 Agent `deepseek-v4-pro` / `openai-chat`、
  配置指纹 `a27d3affa9302ccd`。
- 运行前护栏：`make test-modeler` 为 37 passed；`make check` 为
  224 passed、1 skipped、1 deselected。
- 上一轮初始规划的 `normalize_join_key` scope 失败不再发生。初始 CleaningPlan
  通过；4 张输入表中前 3 张各自产生已验证的 cleaned CSV。第 4 张
  `附件2.xlsx::2023年统计的相关数据` 在 `rule:price-canonical-number`
  验证失败后进入 attempt 1 Repair。
- Repair 模型调用返回后，`validate_repair_scope` 拒绝其改变或删除既有失败规则
  `rule:price-canonical-number`，任务在 `data_cleaning/schema` 有界终止。该次
  E2E 证实初始规划修复有效，但暴露 Repair 对失败规则的允许修改范围与模型输出的
  不兼容；未进入 QuestionPlan、Coder、Writer 或 DOCX 导出。
- scorecard：
  `fixtures/baseline/2024高教杯C题/scorecards/20260905-212110-ecd33559.json`；
  trace：`logs/traces/20260905-212110-ecd33559.jsonl`。评分卡已与
  `20260812-163504-32b726a1` 显式对比：`phase_fail=1`、总计 3 次 LLM
  调用、35,207 tokens，`regression_check.all_pass=false`。M1 保持开启。

## 2026-09-05 - 固定 Fixture E2E（初始 CleaningPlan 重规划耗尽）

- task id：`20260905-223505-ba128b2b`；git revision：
  `f03886c51ffce30f1e54e54a280f2cc15756a31f`，工作树和 M1 source 均为 dirty，
  包含失败规则退役与 Repair 语义重规划的候选修复。fixture、模型配置和指纹与前两轮
  相同：`2024高教杯C题`、四个 Agent `deepseek-v4-pro` / `openai-chat`、
  配置指纹 `a27d3affa9302ccd`。
- 运行前护栏：Cleaning 相关单测 31 passed；`make test-modeler` 为 37 passed；
  `make check` 为 227 passed、1 skipped、1 deselected。
- `task_facts`、`task_outline` 和 4 个输入 Sheet 的 `data_profile` 均通过。
  初始 CleaningPlan 的第 1、2 次模型响应未通过本地 JSON/语义校验，触发仅包含
  错误摘要的重规划；第 3 次模型响应在 JSON 对象中途结束，报
  `Invalid JSON: EOF while parsing an object at line 129 column 37`。
- 因此 `data_cleaning` 在 3 次初始规划预算耗尽后以 `schema` 终止，耗时
  767,509 ms；没有产生 cleaned 表、DataContract、QuestionPlan、Coder/Writer
  产物或 DOCX。这证明 Repair 退役路径尚未获得真实运行覆盖，当前阻塞点回到
  初始 CleaningPlan 的真实模型输出稳定性。
- scorecard：
  `fixtures/baseline/2024高教杯C题/scorecards/20260905-223505-ba128b2b.json`；
  trace：`logs/traces/20260905-223505-ba128b2b.jsonl`。评分卡已与
  `20260812-163504-32b726a1` 显式对比：`phase_fail=1`、4 次 LLM 调用、
  70,537 tokens、`regression_check.all_pass=false`。M1 保持开启。
