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
│       │ code_response + created_images                     │
│       ▼                                                    │
│  WriterAgent ← 同一 chat_history 跨所有章节                │
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
| Coordinator | 1，拆完即闲置 | 单次（JSON 失败会再塞一条 system） | 原始 `ques_all` | `questions` dict |
| Modeler | 1，方案出完即闲置 | 单次 | 拆题 JSON + contract **限长渲染文本** | `questions_solution`（ques* / sensitivity） |
| Coder | 1 | 数据准备一段历史；**reset 后**再跨 ques*/sensitivity | 准备阶段：原始文件名+命名口径；求解：contract 渲染文本 | `code_response`；图文件名；`cleaned/` 表 |
| Writer | 1 | **全程**：solution 各章 + 后置写作各章 | eda 章只拿表清单+清洗摘要，不要整份 JSON | `response_content` 原样进 `res.json` |

解释器 stdout **不**写入 `section_output`（本地 Jupyter 从未 `add_content`）。`flows.get_writer_prompt` 里的 `code_output` 对本地运行几乎是空串。Writer 主要吃 Coder 的 **收工总结句**。

---

## 3. 交接物里装了什么（污染管道）

### 3.1 Coordinator：指令被写进 background

`backend/app/core/prompts/coordinator.py`：

- 「**不要更改题目信息，完整将用户输入的内容**」
- 不在 title / quesN 里的一切 → `background`

因此「不要用方法 A」几乎一定进 `questions["background"]`。没有 `constraints` 字段。

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

求解阶段 Coder 历史仍跨 ques1→quesN，但不再带着清洗全程。

### 3.4 Writer prompt（论文入口）

solution 阶段 `get_writer_prompt`：

```
问题背景{background},不需要编写代码,代码手得到的结果{code_response},{code_output},按照如下模板撰写：{md_template}
```

后置写作 `get_write_flows` 再灌 **整段 `ques_all`**：

```
问题背景{bg_ques_all} … 根据模型的求解的信息{ques1-全文,ques2-全文,…}
```

`get_model_build_solve()` 是把 **已写好的各问论文正文** 拼成一串给摘要/重述用。ques1 里若已有「不用方法 A」，摘要会再抄一遍。

Writer system（`writer.py`）要求「输出纯 Markdown」「**保持与用户输入一致的语言**」，**没有**「禁止复述过程指令 / 方法禁令」。

---

## 4. 「不要用方法 A」如何进答题卡

```
用户 ques_all 含「不要用方法 A」
  → Coordinator 原样放入 background（且禁止改写）
  → Modeler 方案写「不采用 A」
  → Coder user prompt + completion_check 再强调
  → Coder 收工总结：「这是不用方法 A 的结果」
  → Writer user prompt = background + 该总结 + 写作模板
  → 论文出现元评论
  → 同一 Writer 历史写摘要时再次看见这句
  → 压缩若触发，总结会把禁令标成「重要结论」
```

这是 **指令回声（instruction echoing）**：过程约束与可发表内容共用一条上下文通道。只改 Writer 一句「不要提方法 A」不够，因为 background / code_response / 前序章节仍在喂它。

---

## 5. 治理方案（按 ROI）

原则：**过程约束只给会执行的 Agent；论文通道只给赛题与结果数字。**

| 优先级 | 做法 | 改哪里 | 作用 |
|--------|------|--------|------|
| **P0** | Coordinator 拆两路：`background`/`quesN` 只含赛题；另输出 `constraints`（过程指令、禁方法、语气要求）。禁止把约束拷进 background。 | `coordinator.py` + `A2A.CoordinatorToModeler` | 从源头切断 |
| **P0** | Writer **永不**看见 `constraints` 和原始 `ques_all`。`get_write_flows` 改用清洗后的 background，不要 `problem.ques_all`。 | `flows.py` `workflow.py` | 摘要/重述不再回声 |
| **P0** | Writer system 加硬规则：正文禁止提及用户指令、方法禁令、任务过程（「按要求」「未使用 XX」）。只写选用了什么模型、结果是什么。 | `prompts/writer.py` | 廉价，需与 P0 拆字段一起才稳 |
| **P1** | Coder→Writer 交接改为 `run_summary`：模型名、指标、图文件、结论句。不要把 completion 闲聊当 `code_response`。约束可留在 Coder system/sidecar。 | `coder_agent.py` `flows.py` | 与冻数字同一方向 |
| **P1** | completion_check 改为「只列产物与数值，不要复述任务限制」。 | `prompts/shared.py` | 减少总结污染 |
| **P2** | Writer 按章隔离历史（每章 system + 本章材料），或至少后置写作不要带上 Coder 收工原文。 | `writer_agent.py` | 防跨章传染 |
| **P2** | 压缩提示改为：总结 **结果与文件**，丢弃过程指令与禁令。 | `agent.py` `compress_if_needed` | 长任务才触发 |
| **P3** | 保存前扫元评论（「按要求不使用」「这是不用…的结果」）删掉或打回重写。 | `user_output.py` / eval | 兜底，不治本 |

**不要**：指望再加一段 Writer prompt 单独解决问题；不要把 `constraints` 写进 `md_template`。

与 [sibling-references.md](./sibling-references.md) 的衔接：MathModeling-skills 的 `solution_package_for_writer` + `frozen_numbers` 就是 P1 交接该长成的样子——Writer 只读结果包，不读任务闲聊。

---

## 6. 改代码时对照

| 文件 | 角色 |
|------|------|
| `backend/app/core/workflow.py` | 编排；Writer 后置阶段传入完整 `ques_all` |
| `backend/app/core/flows.py` | Coder/Writer prompt 拼接（污染组装处） |
| `backend/app/core/prompts/coordinator.py` | 「完整保留用户输入」→ background |
| `backend/app/core/agents/coder_agent.py` | 跨 phase 历史；`code_response` 定义 |
| `backend/app/core/agents/writer_agent.py` | 跨章节历史；首轮才写 system |
| `backend/app/core/agents/agent.py` | 压缩会强化「重要禁令」 |
| `backend/app/core/prompts/shared.py` | completion_check 回贴 Original task |
| `backend/app/models/user_output.py` | 各问正文再拼进摘要材料 |
| `backend/app/utils/data_contract.py` | 清洗口径、contract 构建/校验/限长渲染 |
| `backend/app/config/md_template.toml` | 章节该写什么（不含约束通道） |
