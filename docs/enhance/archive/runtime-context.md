# 运行时：Agent 角色与上下文

上帝视角看一次任务里 **谁活着、历史怎么长、下游吃什么**。改 prompt / 交接 / 压缩时先读本文。

用户侧症状：「不要用方法 A」这类 **过程指令** 被写进论文（「这是不用方法 A 的结果」）。根因不是模型「不听话」，而是 **赛题文本和过程约束从未拆开**，同一段话被 Coordinator 原样塞进 `background`，再被 Coder 总结、Writer 当「问题背景」反复引用。

---

## 1. 一次任务怎么跑

实例：**每个 Agent 各一份**，贯穿整场任务；**对话历史互不相通**（没有共享 memory）。下游只吃上游的 **结构化交接物**，但交接物里经常带着过程话。

```
ques_all（前端一整段，赛题 + 用户旁注）
    │
    ▼
CoordinatorAgent（一次性 JSON 拆题）
    │  questions: {title, background, ques_count, ques1…}
    ▼
CoderAgent 数据准备（若有 csv/xlsx，不含 result*）
    │  cleaned/{主名}__{sheet}.csv
    ▼
后端 build_data_contract → data_contract.json（限长渲染文本给下游）
    ▼
ModelerAgent（一次性 JSON：ques* + sensitivity，无 eda key）
    │  questions_solution
    ▼
┌─── 共享 Jupyter kernel（work_dir 文件持久）───┐
│  CoderAgent  ← 重置 chat_history 后求解 ques*/sensitivity │
│       │ phase_results/{phase}.json                         │
│       ▼                                                    │
│  WriterAgent ← 每章节独立 chat_history                     │
│       │ 先写 eda（表清单+清洗摘要）再写 ques*               │
│       │ 再 write 循环写摘要/重述/假设/评价                  │
└────────────────────────────────────────────────────────────┘
    ▼
UserOutput 拼接 res.md → Pandoc res.docx
```

入口：`backend/app/core/workflow.py` `MathModelWorkFlow.execute`。
前端：`UserStepper.vue` 把输入框整段作为 `ques_all`，无「过程约束」字段。

---

## 2. 各角色：生命周期与上下文

共同基类：`backend/app/core/agents/agent.py`

- `chat_history: list[dict]`，每次 `_chat(history=self.chat_history)` **整表发给模型**。
- 超过 `context_window * 0.75` 时 LLM 总结压缩：保留第一条 system + `[历史对话总结]` + 末尾几条。总结提示是「保留重要结论」——**禁令会被当成重点留下，污染加重**。
- 窗口默认 128000；`MAX_CHAT_TURNS` / `MAX_RETRIES` 可空（不限）。

| Agent | 实例数 | 历史跨度 | 输入 | 输出（下游真吃的） |
|-------|--------|----------|------|-------------------|
| Coordinator | 1，拆完即闲置 | 单次（JSON 失败会追加纠错消息） | 原始 `ques_all` | 公共 `questions` + 独立 `constraints` |
| Modeler | 1，方案出完即闲置 | 单次 | 公共题面 + 执行约束 + contract **限长渲染文本** | `questions_solution`（ques* / sensitivity）+ constraints |
| Coder | 1 | `eda` / 每个 `ques*` / sensitivity 各自独立 history | 准备阶段：文件口径；求解：contract 渲染文本；执行约束 | `PhaseResult`；真实产物；`cleaned/` 表 |
| Writer | 1 | **每章节重置 history** | 公共题面 + 对应 `PhaseResult`，不接收执行约束 | `response_content` 原样进 `res.json` |

解释器保留有限 `section_output` 供结果包提取事实，并以 phase 起始文件快照识别真实新增产物。Writer 的精确数字和图片以 `PhaseResult` 为唯一来源。

---

## 3. 交接物里装了什么（污染管道）

### 3.1 Coordinator：题面与执行约束已分流

`backend/app/core/prompts/coordinator.py`：

- `questions` 仅保存可公开题面事实和数学模型约束。
- `constraints` 保存“不要使用某方法”“必须调用某工具”“保存到指定文件”等执行要求。
- 旧扁平 JSON 继续兼容；仅对有限、明确的中英文指令做句级兜底提取。

### 3.2 Modeler：整包 questions

方案 JSON 的 value 是长字符串，常会把禁令写进「模型选择理由」（「因用户要求不采用 A，故用 B」）。这份字符串随后贴进 Coder prompt。

### 3.3 Coder prompt（每问）

数据准备在建模前单独跑，随后 `reset_for_solve` 清空对话。求解阶段首次消息是 contract 限长文本，不再是文件名列表。

`flows.get_solution_flows`：

```
只读取 cleaned/ 路径，先校验行数与列名再求解。
参考建模手给出的解决方案{solutions[key]}
完成如下问题{quesN 原文}
```

再叠加：首次 system `CODER_PROMPT`、ques 阶段 visualization 等 **全文 skill**、ReAct 的 reflection / completion_check。

completion_check **再次粘贴 Original task**（含禁令）。Coder 被要求「brief summary of what was accomplished」——模型习惯写成「按要求未使用方法 A，改用 B，R²=…」。
**这句话就是 `CoderToWriter.code_response`。**

求解阶段每个 `ques*` 都重新建立 history，但共享同一解释器、work_dir 和 data contract。

### 3.4 Writer prompt（论文入口）

solution 阶段 `get_writer_prompt`：

```
问题背景{background},不需要编写代码,代码手得到的结果{code_response},{code_output},按照如下模板撰写：{md_template}
```

后置写作 `get_write_flows` 使用清洗后的公共题面和阶段结果包：

```
问题背景{public_context} … 根据模型的求解信息{phase_results}
```

Writer 不再读取原始 `ques_all` 或已写章节全文，每章使用独立 history；system prompt 同时禁止复述用户指令、执行约束和工作过程。

---

## 4. 指令回声的旧链路与当前阻断点

```
用户 ques_all 含「不要用方法 A」
  → Coordinator 将其放入 constraints，不复制到公共题面
  → Modeler/Coder 在独立执行通道中遵守
  → Coder phase result 删除明确的约束和过程回声
  → Writer 只读取公共题面与可验证结果包
  → 每章 history 独立，避免跨章再次传播
```

当前结构从源头和交接两处阻断 **指令回声（instruction echoing）**。有限句级规则只做兼容兜底，不能替代 Coordinator 的结构化输出。

---

## 5. 治理方案（按 ROI）

原则：**过程约束只给会执行的 Agent；论文通道只给赛题与结果数字。**

| 优先级 | 做法 | 改哪里 | 作用 |
|--------|------|--------|------|
| **已完成** | Coordinator 拆两路：`background`/`quesN` 只含赛题；另输出 `constraints`。 | `coordinator.py` + `A2A.CoordinatorToModeler` | 从源头切断 |
| **已完成** | Writer 不接收 `constraints` 和原始 `ques_all`，只使用公共题面。 | `flows.py` `workflow.py` | 摘要/重述不再回声 |
| **已完成** | Writer system 禁止复述用户指令、方法禁令和任务过程。 | `prompts/writer.py` | 写作侧硬约束 |
| **已完成** | Coder→Writer 使用 `PhaseResult`：状态、指标、图文件、限制和有限摘要。 | `phase_results.py` `flows.py` | 结果交接可验证 |
| **P1** | completion_check 改为「只列产物与数值，不要复述任务限制」。 | `prompts/shared.py` | 减少总结污染 |
| **已完成** | Writer 按章隔离历史（每章 system + 本章材料）。 | `writer_agent.py` | 防跨章传染 |
| **P2** | 压缩提示改为：总结 **结果与文件**，丢弃过程指令与禁令。 | `agent.py` `compress_if_needed` | 长任务才触发 |
| **P3** | 保存前扫元评论（「按要求不使用」「这是不用…的结果」）删掉或打回重写。 | `user_output.py` / eval | 兜底，不治本 |

**不要**：指望再加一段 Writer prompt 单独解决问题；不要把 `constraints` 写进 `md_template`。

与 [sibling-references.md](./sibling-references.md) 的衔接：MathModeling-skills 的 `solution_package_for_writer` + `frozen_numbers` 就是 P1 交接该长成的样子——Writer 只读结果包，不读任务闲聊。

---

## 6. 改代码时对照

| 文件 | 角色 |
|------|------|
| `backend/app/core/workflow.py` | 编排并保存每个 Coder phase 的结果包 |
| `backend/app/core/flows.py` | 组装公共题面、执行约束和 Writer 结果材料 |
| `backend/app/core/prompts/coordinator.py` | 定义 `questions` / `constraints` 双通道 |
| `backend/app/core/agents/coder_agent.py` | 每 phase 独立 history；共享解释器 |
| `backend/app/core/agents/writer_agent.py` | 每章节独立 history |
| `backend/app/core/agents/agent.py` | 统一响应记账、token 预算和安全压缩 |
| `backend/app/core/prompts/shared.py` | completion_check 回贴 Original task |
| `backend/app/models/user_output.py` | 各问正文再拼进摘要材料 |
| `backend/app/utils/data_contract.py` | 清洗口径、contract 构建/校验/限长渲染 |
| `backend/app/utils/phase_results.py` | 阶段事实、产物指纹与 Writer 渲染 |
| `backend/app/config/md_template.toml` | 章节该写什么（不含约束通道） |
