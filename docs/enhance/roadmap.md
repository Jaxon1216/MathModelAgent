# 重建路线图

## 阶段原则

- 当前阶段仍是 M1。只有前一阶段的完成定义全部有证据后，下一阶段才成为当前实施阶段。
- 路线图顺序为 M1 收口、M1.5 数据可靠性、M2 受限执行与结果契约、M3
  论文证据与导出；标准 A2A 不属于这些阶段的前置依赖。
- 阶段状态以 `experiment-log.md` 中追加的运行证据为准。历史记录和历史
  scorecard 保持原样，不为适配新门禁而回写。

## 当前阶段：M1 建模步骤收口

### 范围

关闭现有 Modeler 输入、输出、校验和基线 Coder/Writer 兼容边界。M1 只处理完成
验收与不中断交付所需的兼容问题，不提前实现完整的数据可靠性、依赖调度、
ResultPackage 状态机或导出重构。

第一版继续使用文本 JSON、Pydantic 校验和有限修复；不把 provider 强制
function call 作为全链路依赖。修复次数由 `MODELER_MAX_REPAIR_ATTEMPTS`
控制，默认 3，接受任意非负整数；它只控制格式/契约修复，不控制网络重试或
Agent 步数。

### 当前事实

- 本地 Modeler 测试和新 ModelPlan 到旧 Coder 的 handoff 契约已通过。
- 固定配置三路真实 Modeler smoke 已通过；对应 revision、fixture、模型配置
  和运行结果已记录在 `experiment-log.md`。smoke 不运行完整 Coordinator、
  Coder、Writer、解释器或 Pandoc，不能替代 E2E。
- 固定配置完整 E2E 已运行（task `20260905-095821-1cf855ad`），工作流生成了
  `res.md` 和 `res.docx`，但评分卡因 `ques3` 空章节得到
  `regression_check.all_pass=false`。M1 因此仍未完成。
- 修复 Writer 有界工具循环并增加 Coder `success/partial` 兼容交接后，固定
  fixture E2E `20260905-140132-e19ba578` 已完整交付 Markdown/Word。全部
  11 个章节非空，图片覆盖率 1.0，Word 含图片和公式；Q2、Q3 与敏感性分析
  在 retry 耗尽后以 partial 继续，错误只保留在 trace。当前评分卡只因 Q3
  没有独立图片而未全部通过，具体证据见 `experiment-log.md`。

### 验收输入与证据

- 只使用唯一的 2024 高教杯 C 题 fixture；固定模型、API 类型和影响行为的配置。
- E2E 必须关联可定位的代码版本，并记录工作树状态，避免把无法复现的源码状态
  当作固定版本证据。
- 成功运行必须生成可校验的最终产物，并保存新任务的 task id、scorecard 和
  trace；scorecard 与质量基线任务 `20260812-163504-32b726a1` 对比。
- task id、代码版本、模型配置、fixture、scorecard 路径、trace 路径和结论必须
  追加到 `experiment-log.md`。本阶段不修改已有实验记录或历史 scorecard。

### 阶段化评测门禁

- M1 仍使用 phase 起始时预注入 skill 正文的实现，因此不得仅因没有真实
  `load_skill` tool call 判定失败。为 M1 新任务生成评分卡时，从 M1 门禁移除
  该要求，但不回写任何历史 scorecard。
- M1 继续检查真实 `execute_code`、执行错误率、phase 失败、图片覆盖、空章节、
  最终 Markdown/DOCX、图片、公式、引用和章节完整性等现有质量门禁。
- M2 完成 L2 按需加载切换后，恢复真实且可追踪的 `load_skill` tool-call 门禁；
  只检查实际需要相关技能的 phase，不要求无条件加载全部技能。
- `partial/degraded`、缺失 summary 和 phase 状态继续进入评分卡与告警，但当前
  不新增 `max_degraded_phases`、`max_phase_fail` 等阻塞门禁。检索失败且正文
  完整时只记录 limitation；partial phase 不阻止 `res.md/res.docx` 交付。

### 验证顺序

1. 运行 `cd backend && make test-modeler` 和 `make check`，确认本地契约与工程护栏。
2. 在可定位的代码版本上，以同一 fixture 和同一模型配置运行三次独立真实
   Modeler smoke；现有三路通过记录作为当前事实，后续代码变化时按需重跑。
3. 仅在上述条件满足后运行一次固定配置完整 E2E，保存评分卡并与唯一质量基线
   对比，再向 `experiment-log.md` 追加证据。

### 完成定义

- 三次真实 smoke 均得到合法计划，覆盖全部 `quesN` 和
  `sensitivity_analysis`，且不编造数据列或文件。
- 固定配置完整 E2E 能把计划交给基线 Coder，并生成可校验的 `res.md`、
  `res.docx`、图片和公式对象。
- M1 适用的全部质量门禁通过，task id、代码和模型配置、scorecard、trace 及
  对比结论均有可定位记录。
- 失败有结构化、可定位的原因，不依赖无界重试。只有以上条件全部有证据时，
  才能关闭 M1 并进入 M1.5。

### E2E 失败处理

- 若完整 E2E 未生成最终产物、评分门禁未通过，或暴露 Modeler 到基线
  Coder/Writer 的兼容问题，M1 保持开启，不开始 M1.5 或 M2 的运行时重构。
- 将失败 task id、scorecard、trace 和原因追加到 `experiment-log.md`。
- 修复范围仅限关闭 M1 所必需的回归或兼容问题；修复后重新执行 M1 所需验证。

## 后续阶段：M1.5 数据可靠性闭环

### 实现状态（2026-09-05）

- 经批准的 Tasks 1-9 已完成实现与本地验收。主工作流已按
  `TaskFacts -> TaskOutline -> DataProfile -> CleaningPlan/DataIssue ->
  DataContract -> QuestionPlan` 接入，旧 Coder 仅通过单一文本桥接消费冻结
  cleaned 路径、验证字段和依赖声明。
- Task 7 最终集成审查确认：问题 phase 使用 outline 的稳定拓扑顺序，EDA
  只读消费冻结契约；未解决 issue、非法计划、取消或契约指纹漂移会阻断
  Coder/Writer/最终发布，阶段 trace 保留唯一终态与稳定失败分类。
- 唯一真实附件目录 `backend/app/example/example/2024高教杯C题/` 的离线
  fake-LLM 测试通过：4 张输入 Sheet 均形成 profile、cleaned 产物和 frozen
  contract，3 个 `result*.xlsx` 模板全部排除；源文件 SHA-256 与 mtime 不变，
  `QuestionPlan` 覆盖 `ques1..ques3` 且兼容 flow 满足依赖顺序。
- 本地验证为 `make test-m15`: 146 passed、`make check`: 203 passed,
  1 skipped, 1 deselected，以及全量应用 Pyright 0 errors。上述证据不包含真实模型或完整
  E2E；本轮未运行 `smoke-modeler`、`e2e-fixture`、`eval`，未生成 scorecard，
  未修改 `experiment-log.md`。因此本记录只关闭 M1.5 实现任务，不改变既有
  M1 E2E 结论，也不启动后续阶段。

### 前置条件与范围

M1 的完成定义全部满足并有实验记录后才开始 M1.5。本阶段按
`TaskOutline -> DataProfile -> CleaningPlan/DataIssue -> DataContract -> QuestionPlan`
建立可版本化、可校验的阶段产物，不实现 M2 的 Coder 执行状态机。

### TaskOutline

- Coordinator 从只读 `TaskFacts` 生成一次全局任务骨架，声明稳定的问题标识、
  问题依赖、执行顺序和总体交付物。
- 调度器以依赖图和执行顺序为依据；`ques_count` 最多作为兼容或派生字段，
  不再单独决定调度。
- 只有 `TaskOutline` 明确标记为无依赖的问题才可并行；存在依赖的问题必须按
  已声明顺序执行。

### DataProfile

- 只读扫描附件中的每个文件和 Sheet，不修改源数据。
- 每张输入表记录行数、字段、原始与规范类型、缺失情况、重复情况、候选键和
  可关联字段，并保留源文件与 Sheet 定位。
- 输出模板与输入表继续分开，不能把待填写模板误判为建模数据。

### CleaningPlan 与 DataIssue

- `CleaningPlan` 只能声明有明确理由的局部操作，包括有限的类型转换、缺失值
  处理、去重或关联修复；cleaned 产物写入独立路径，原始数据始终只读。
- 清洗后以确定性规则验证字段存在性、类型、行数、键、缺失策略和关联结果。
- 验证失败时输出结构化 `DataIssue`，至少定位对应表、规则、期望、实际结果和
  证据；Repair 只能处理该表和该规则。
- 每个清洗问题最多 Repair 两次。耗尽后保留失败状态和证据，并阻止下游把该
  数据标记为有效，不转入开放式 EDA 或无界 ReAct。

### DataContract 与 QuestionPlan

- 只有通过确定性验证的 cleaned 表才能进入版本化 `DataContract`；契约只暴露
  可用表、字段、类型、统计口径、关联方式和产物路径。
- `DataContract` 冻结前不得生成最终 `QuestionPlan`。
- 每个 `QuestionPlan` 只能读取 `TaskOutline`、冻结的 `DataContract` 和已声明
  的上游问题结果，并明确单题目标、输入、模型、验证方法和图表计划。

### 阶段产物

- 版本化 `TaskOutline`、逐表 `DataProfile`、`CleaningPlan`、零个或多个
  `DataIssue`、冻结的 `DataContract`，以及覆盖全部问题的 `QuestionPlan`。
- 每项产物记录 schema 版本、输入来源、校验状态和产物路径；失败状态不得伪装
  为可供下游消费的成功契约。

### 验证与完成定义

- 契约测试覆盖依赖调度、无依赖并行、表级画像、源数据只读、确定性清洗验证、
  局部 Repair 上限和失败阻断。
- 使用唯一 fixture 验证所有输入表均被画像，cleaned 产物与 `DataContract`
  一致，且最终 `QuestionPlan` 不引用契约外字段或未声明的上游结果。
- 只有上述测试通过，全部问题形成可校验计划，并且失败路径保留结构化证据时，
  才关闭 M1.5 并进入 M2。

## 后续阶段：M2 受限执行与结果契约

### 前置条件与范围

按 2026-09-05 的实现决策，M2 不等待 M1.5 E2E；M1 / M1.5 的真实运行证据仍按其
各自门禁记录。本阶段只收口 Coder 的工具调用、phase 上下文和结果交接边界，保持
现有 `QuestionPlan -> 文本兼容桥接 -> Coder` 主链路，不引入通用 Agent 编排框架、
跨 phase 依赖阻断或新的总流程失败门禁。

### 第一个独立小提交：渐进式 SkillsLoader

#### 实施状态（2026-09-05，已完成）

- 已完成 L1/L2 与 Repair 短上下文的本地切片：`SkillRegistry` 只读取
  frontmatter 建立 `SkillDescriptor` 索引，正文仅能由真实 `load_skill` tool call
  取得，并以内容 SHA-256 前缀标识版本。
- 已删除 Coder phase 起始时的正文预注入路径；成功、未知和非法 skill 请求均以对应
  `tool_call_id` 回填，trace 记录名称、版本、加载结果、来源和 phase。Repair 仅保留
  已加载 skill 的名称与版本，不复制正文。
- 新增 L2 trace 到 eval 的按 phase 检查能力，但未修改现有基线或历史 scorecard。
  聚焦测试 24 passed，目标 Pyright 0 errors。
- P0 已修复：同一响应中混合 `load_skill` / `execute_code` 或包含多次
  `execute_code` 时，按响应顺序执行，并在下一次模型请求前为每个
  `tool_call_id` 回填成功或失败结果。
- 每个 phase 开始均重置 Coder history 与 phase turn counter；受控工作目录和同一
  解释器继续共享。Repair 耗尽会生成 `partial` ResultPackage，并继续交给 Writer，
  不新增总流程阻塞。
- `ResultPackage` 已原子落盘到 `result_packages/{phase}.json`，同时保存代码和
  stdout 快照、文件指纹、图表、指标定位、限制和 partial 失败证据。Writer 改为消费
  package 渲染材料；package 写入自身失败时仅记录 limitation 并保留原有交付路径。
- 新增 ResultPackage、phase 隔离和 P0 工具循环测试。`make check` 为 223 passed、
  1 skipped、1 deselected，目标 Pyright 0 errors。本轮未运行真实模型、完整 E2E、
  `eval` 或 M1.5 E2E，也未改质量基线或历史 scorecard。

- 工具 schema 中保留 catalog skill 的名称和简短 description，作为 L1 能力
  索引；不在 phase 开始时传入 skill 正文。
- 删除 `_ensure_phase_skills` 和用户消息中的正文预注入路径。skill 正文只能由
  模型显式调用 L2 `load_skill` 按需获取。
- skill 名称、版本标识、加载结果和使用 phase 必须进入 trace。M2 验收从此要求
  实际需要相关技能的 phase 产生真实 `load_skill` tool call。
- Repair 建立新短上下文时，只保留已加载 skill 的名称和版本标识，不复制已加载
  正文；需要正文时由模型按 L2 重新取得。
- “要求绘图但未加载 skill”的专用 Verifier、通用 Inspect/Verify 状态机和跨 phase
  依赖阻断不属于 M2 的完成定义；有明确收益时另立阶段。

M2 不实现 HelloAgents L3 references、scripts 或其他资源读取。未来如确有需求，
必须另立变更，并先定义路径白名单、单次与累计大小预算以及可追溯读取记录。

### 已完成的 phase 隔离与工具循环

- 每个问题 phase 创建独立短 history，不继承其他 phase 或自由对话历史；不同
  phase 继续共享同一个受控工作目录和解释器，以复用声明的文件产物。
- 同一模型响应包含一个或多个 tool call 时，执行循环按响应顺序处理全部调用；
  每个 `tool_call_id` 恰好回填一个成功或失败结果。全部结果回填前不得发起下一次
  模型请求。

### ResultPackage

`ResultPackage` 是每个求解 phase 的唯一终态产物，并替代 `CoderToWriter`
自由文本交接。下游不得以 Coder 对话历史、散落 stdout 或未登记文件代替它。

每个版本化 package 至少包含：

- 成功或 partial 状态、对应 phase 和生成时间。
- 代码与 stdout 快照、指标来源定位、图表、限制和适用范围。
- partial 时的稳定失败分类、失败证据及已完成产物。
- 所有登记文件的路径与 SHA-256 指纹。

partial package 仍须完整落盘，并继续交付给 Writer；M2 不将 partial 或 package
写入失败升级为总流程阻塞。跨问题依赖失败传播和最终发布阻断留待另立阶段。

### 验证与完成定义

- phase 起始上下文不含预加载正文，L2 调用、名称、版本、调用 ID 和 phase 可追踪；
  Repair 不重复携带正文。
- 混合 `load_skill` / `execute_code`、多个 `execute_code`、未知工具和工具参数失败
  均按顺序一一回填；回填完成前不发起下一次模型请求。
- phase history / turn counter 隔离、共享解释器和工作目录，以及 Repair 耗尽后的
  partial 交付均通过契约测试。
- 成功和 partial `ResultPackage` 均带代码/输出快照、实际文件指纹和指标来源定位；
  Writer 仅消费 package 材料，partial 不阻断论文交付。
- 上述本地契约测试、Pyright 和 `make check` 全部通过即关闭 M2；真实 E2E 仍按 M1 /
  M1.5 的独立证据规则执行。

## 后续阶段：M3 论文证据、写作与导出

### 前置条件与范围

M2 完成且所有可执行问题均产生终态 `ResultPackage` 后才开始 M3。本阶段先冻结
论文证据，再生成正文和执行格式导出；Writer 不参与仍会改变事实的求解过程。

### PaperClaims 冻结

- 汇总并校验全部终态 `ResultPackage` 后生成版本化 `PaperClaims`。只有验证
  通过的 package 能贡献可引用事实；失败 package 保留为限制或阻断证据。
- 每个可引用数字、图和结论必须记录来源 package、稳定定位、单位和适用限制；
  图表还须对应 manifest 中存在且指纹一致的实际文件。
- 冻结前执行一致性审计，核对数字、图、结论、单位、限制和文件。审计失败时
  不得发布 claims，也不得让 Writer 从其他来源补齐事实。

### 受约束写作

- Writer 只能引用冻结 `PaperClaims` 中可追溯的数字、图和结论，不得读取原始
  执行历史、散落 stdout 或未登记文件来补造事实。
- 方法、结果和讨论章节基于冻结 claims 生成；摘要和结论在其他正文及全部 claims
  确定后最后生成，避免与求解并行造成前后不一致。
- 图表只解释已验证的文件和结果，每个引用保持从正文到 claim、package 和文件的
  可追溯链路。
- 论文语义内容与输出格式解耦，由 `format_profile` 提供 Markdown 模板、
  `reference.docx`、图表规范和导出校验规则。

### 一致性与导出校验

- 语义校验覆盖正文数字、图表引用、结论、单位和限制与 `PaperClaims` 的一致性。
- 格式与产物校验覆盖 Markdown、DOCX、图片、公式对象、参考文献与文内引用、
  章节完整性和必需文件存在性。
- 任何必需问题失败、claim 无来源、文件指纹不一致或最终产物缺失，都必须保留
  可定位证据并阻止任务标记成功。

### 阶段产物与完成定义

- 产物包括冻结的 `PaperClaims`、一致性审计结果、语义章节、最终 `res.md`、
  `res.docx` 和导出校验记录。
- Writer 来源约束、摘要与结论时序、claim 到文件的追踪以及失败阻断均须通过
  契约测试。
- 固定 fixture 的阶段验收必须证明最终 Markdown 和 DOCX 内容一致，且图片、
  公式、引用和章节完整性均通过；证据按当时 roadmap 的要求追加记录后才关闭 M3。

## 参考机制与非目标

- HelloAgents 只借鉴 L1 名称/简短 description 与 L2 正文按需加载。M2 不导入
  其 L3 references/scripts 目录和资源系统。
- deer-flow 只借鉴长上下文压缩后保留已加载工具名称与版本短状态的思想，不导入
  LangGraph、checkpoint 或其 Agent 编排运行时。
- MathModeling-skills 只借鉴 solution package、frozen numbers 和一致性审计
  思想，不照搬其工作流、人工 gate 或运行时。
- 项目保持现有 Coordinator -> Modeler -> Coder -> Writer -> Pandoc 主链路和
  本地轻量运行方式，不以外部框架替换现有实现。

## 远期 A2A 边界

标准 A2A 不是 M1.5、M2 或 M3 的前置依赖。只有本地 Agent 之外出现明确的
跨进程或远程 Agent 互操作需求，并能定义身份、传输、失败恢复和验收收益时，
才另立阶段评估；当前路线图不预先引入其协议或运行时。
