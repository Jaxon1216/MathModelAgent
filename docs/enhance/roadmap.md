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

M1.5 的完成定义全部满足后才开始 M2。Coder 只消费已验证的
`QuestionPlan`、`DataContract` 和已声明的上游 `ResultPackage`。本阶段目标是
可验证、可终止、可追溯的执行，不扩展为泛化或无界 ReAct，也不引入通用 Agent
编排框架。

### 第一个独立小提交：渐进式 SkillsLoader

- 工具 schema 中保留 catalog skill 的名称和简短 description，作为 L1 能力
  索引；不在 phase 开始时传入 skill 正文。
- 删除 `_ensure_phase_skills` 和用户消息中的正文预注入路径。skill 正文只能由
  模型显式调用 L2 `load_skill` 按需获取。
- skill 名称、版本标识、加载结果和使用 phase 必须进入 trace。M2 验收从此要求
  实际需要相关技能的 phase 产生真实 `load_skill` tool call。
- Repair 建立新短上下文时，只保留已加载 skill 的名称和版本标识，不复制已加载
  正文；需要正文时由模型按 L2 重新取得。
- 当 `QuestionPlan` 或待修复问题要求绘图，而当前 phase 没有真实加载相关绘图
  skill 时，Verifier 输出结构化问题并打回 Repair。
- 该提交独立通过 loader、上下文和 trace 聚焦测试后，才开始执行状态机改造。

M2 不实现 HelloAgents L3 references、scripts 或其他资源读取。未来如确有需求，
必须另立变更，并先定义路径白名单、单次与累计大小预算以及可追溯读取记录。

### Phase 隔离与受限状态机

- 每个问题 phase 创建独立短 history，不继承其他 phase 或自由对话历史；不同
  phase 继续共享同一个受控工作目录和解释器，以复用声明的文件产物。
- 状态固定为 `Inspect -> Execute -> Verify -> Repair -> Verify`。Verifier
  输出机器可读的问题，Repair 只能接收并处理这些问题，不能自由扩展目标。
- 每个 phase 最多进入两次 Repair。第二次 Repair 后仍未通过 Verify 时，终止
  phase 并输出失败结果和证据，不继续自由反思。
- 同一模型响应包含一个或多个 tool call 时，执行循环按响应顺序处理全部调用；
  每个 `tool_call_id` 恰好回填一个成功或失败结果。全部结果回填前不得发起下一次
  模型请求。

### ResultPackage

`ResultPackage` 是每个求解 phase 的唯一终态产物，并替代 `CoderToWriter`
自由文本交接。下游不得以 Coder 对话历史、散落 stdout 或未登记文件代替它。

每个版本化 package 至少包含：

- 成功或失败状态，以及对应问题和输入契约版本。
- 代码或 Notebook 路径、指标与单位、图表 manifest、限制和适用范围。
- 结构化校验结果；失败时还包括失败阶段、问题、已完成产物和失败证据。
- 所有登记文件的路径与指纹，以及数值、图表和候选结论的稳定来源定位。

失败 package 仍须完整落盘。依赖失败 phase 的后续问题不得被标记成功，也不得
通过自由文本绕过阻断。

### 验证与完成定义

- SkillsLoader 小提交证明 phase 起始上下文不含正文、L2 调用可追踪、Repair
  不重复携带正文，并覆盖需要绘图但未加载技能的失败路径。
- 状态机测试覆盖 phase history 隔离、受控目录与解释器共享、多个 tool call
  顺序执行和逐一回填、两次 Repair 上限及失败终止。
- `ResultPackage` schema、文件指纹、来源定位、成功交接和依赖失败阻断均通过
  契约测试；运行证据能从 package 定位到实际文件和校验结果。
- 只有全部问题得到终态 `ResultPackage`，且没有把失败伪装为成功时，才关闭
  M2 并进入论文证据冻结阶段。

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
