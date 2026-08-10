# 历史任务入口与日志预览

> 范围：**前端历史任务列表 + 任务详情回放**；CLI 日志预览（`preview_logs.py`）作为开发侧补充。  
> 日期：2026-08-10  
> 关联分支：`feat/quality-harness`

---

## 1. 现状分析

### 1.1 用户期望 vs 实际

| 期望 | 实际 |
|------|------|
| 侧边栏「历史任务」可点进每个 task_id | `AppSidebar.vue` 中 `items: []`，**空壳** |
| 任务跑着能看实时进度 | 仅当打开 `/task/{id}` 且 WebSocket 连对 backend 时可用 |
| 跑完后能回看对话与 trace | **能力已有**，但无入口，需手拼 URL |
| CLI 能看任务进度 | `preview_logs.py` 为**手动快照**，非热更新 |

### 1.2 前端（已有 / 缺失）

**已有：**

| 组件 | 路径 | 能力 |
|------|------|------|
| 任务详情页 | `frontend/src/pages/task/index.vue` | Chat + Agent 编辑器 + 文件 Tab |
| 路由 | `/task/:task_id`（`router/index.ts`） | `props: true` 传 task_id |
| 历史消息 | `taskStore.loadTaskMessages()` | `GET /messages?task_id=` |
| 实时推送 | `taskStore.connectWebSocket()` | `ws://{host}/task/{task_id}` |
| 消息持久化 | 后端 `logs/messages/{id}.json` | 与前端展示同源 |

**缺失：**

| 组件 | 说明 |
|------|------|
| 历史任务列表 UI | 侧边栏无数据、无链接 |
| 列表 API | 后端无 `GET /tasks` |
| 任务状态展示 | 进行中 / 成功 / 失败无统一字段 |
| 已完成任务只读模式 | 详情页一律连 WS + 显示「停止」，历史任务体验略怪 |

### 1.3 后端（已有 / 缺失）

**已有：**

```
logs/traces/{task_id}.jsonl     ← 结构化 trace（append）
logs/messages/{task_id}.json    ← WebSocket 消息备份（append）
project/work_dir/{task_id}/     ← 产物（res.md、png、xlsx…）
GET /messages?task_id=          ← 单任务历史
```

**缺失：**

```
GET /tasks                      ← 枚举本地 task_id + 元数据
```

列表逻辑已在开发脚本中验证：`backend/scripts/preview_logs.py --list`（扫 `logs/traces/*.jsonl`）。

### 1.4 三种「看进度」方式对比

```
任务运行中
    │
    ├─→ Redis pub/sub ──→ WebSocket ──→ /task/{id} 页面     【热更新 · 产品路径】
    │
    ├─→ 追加写 traces/*.jsonl、messages/*.json              【磁盘实时写】
    │
    └─→ preview_logs.py / make logs                         【CLI 手动快照 · 开发路径】
```

| 方式 | 热更新 | 适用场景 |
|------|--------|----------|
| 前端 `/task/{id}` | ✅ | 跑任务时盯进度、回看对话 |
| `make logs` | ❌ 敲一次读一次 | 本地 debug、写 enhance 文档 |
| `tail -f logs/traces/{id}.jsonl` | ✅ 原始 JSONL | 深度排查 |

### 1.5 环境陷阱（非功能缺失，但常误判）

1. **双 backend**：Docker `:8000` vs 本机 uvicorn `:8001` 是**两个进程**，`save-api-config` 写入的内存配置不共享。
2. **前端默认连 Docker**：`frontend/.env.development` → `VITE_API_BASE_URL=http://localhost:8000`。任务若在 `:8001` 启动，页面 WebSocket 可能**无实时消息**（历史 `GET /messages` 仍可读，若文件在本机磁盘上）。
3. **API Key 落盘**：`.env.dev` 可能只有部分 Agent；运行时 key 来自前端保存 → **重启 backend 后需重新 save 或补全 .env.dev**。

### 1.6 与质量基建的关系

| 基建 | 状态 | 与历史任务的关系 |
|------|------|------------------|
| `eval_task.py` | ✅ 已有 | 跑完后 CLI 打分；未来可在详情页链入 scorecard |
| `preview_logs.py` | ✅ 已有 | 开发侧预览；**不替代**前端历史入口 |
| `fixtures/problems/` | ✅ 已有 | 基线赛题；与历史列表独立 |
| 侧边栏历史 | ❌ 未做 | **本文档要补的缺口** |

---

## 2. 改动计划

### 2.1 原则

- **复用**已有 `/task/:task_id` 详情页，不新造页面（除非后续要表格搜索）。
- **列表数据源**以 `logs/messages/` 为主（有对话才可回放）；`logs/traces/` 作补充（仅有 trace 无 messages 的 task 可标「无对话」或隐藏）。
- **改动集中在 backend 1 个路由 + frontend 2~3 个文件**，不动 Agent 主链路。
- **阶段交付**：先 MVP（能点进去），再 polish（状态标签、只读模式）。

### 2.2 阶段 A · MVP（预估 1~2h）

#### A1. 后端：任务列表 API

**文件**：`backend/app/routers/common_router.py`

```http
GET /tasks?limit=50
```

**响应示例：**

```json
{
  "tasks": [
    {
      "task_id": "20260810-230647-ae480f0b",
      "updated_at": "2026-08-10T23:34:52+08:00",
      "message_count": 7696,
      "has_work_dir": true,
      "has_res_md": false,
      "status": "running"
    }
  ]
}
```

**字段来源：**

| 字段 | 来源 |
|------|------|
| `task_id` | `logs/messages/{id}.json` 文件名 |
| `updated_at` | 文件 mtime |
| `message_count` | JSON 数组长度 |
| `has_work_dir` / `has_res_md` | `project/work_dir/{id}/` 是否存在 |
| `status` | 解析 messages 最后几条 system：`success` / `error` / `warning` → 映射为 `completed` / `failed` / `cancelled`；否则 `running` 或 `unknown` |

**实现要点：**

- 复用 `ensure_safe_task_id` 防路径穿越。
- 按 mtime 倒序，默认 `limit=50`。
- 纯读磁盘，无 DB。

#### A2. 前端：API 封装

**文件**：`frontend/src/apis/commonApi.ts`

```ts
export function listTasks(limit = 50) {
  return request.get<{ tasks: TaskSummary[] }>("/tasks", { params: { limit } });
}
```

新增类型可放 `frontend/src/utils/interface.ts` 或 API 文件内。

#### A3. 前端：侧边栏填充

**文件**：`frontend/src/components/AppSidebar.vue`

- `onMounted` 调用 `listTasks()`。
- 「历史任务」下渲染 `router-link` → `/task/{task_id}`。
- 展示：`task_id`（可截断）+ 相对时间；空列表显示「暂无历史任务」。
- 「开始新任务」链到 `/chat`（可选，现为空 `#`）。

**不改**：`components/ui/` 下 shadcn 组件。

### 2.3 阶段 B · 体验 polish（预估 0.5 天，可选）

| 项 | 文件 | 说明 |
|----|------|------|
| B1 已完成只读 | `task/index.vue` | `status !== running` 时不连 WS、隐藏「停止」、运行时长定格 |
| B2 状态徽章 | `AppSidebar.vue` | 绿/红/灰点：完成 / 失败 / 进行中 |
| B3 当前 task 高亮 | `AppSidebar.vue` | 对比 `route.params.task_id` |
| B4 独立历史页 | 新 `pages/tasks/index.vue` + 路由 | 表格 + 搜索；任务量大时再上 |
| B5 产物快捷入口 | `task/index.vue` 或 FileSheet | 链到 work_dir 已有文件列表（部分已有） |

### 2.4 不在本计划内

- 任务列表存 Redis / DB（本地开源项目，扫目录够用）。
- `preview_logs.py --follow` 热更新（CLI 增强，与前端入口独立）。
- 修改 Docker 架构或统一端口（另文档 / 运维约定）。

### 2.5 文件改动清单（MVP）

| 操作 | 路径 |
|------|------|
| 修改 | `backend/app/routers/common_router.py` |
| 修改 | `frontend/src/apis/commonApi.ts` |
| 修改 | `frontend/src/components/AppSidebar.vue` |
| 可选 | `frontend/src/utils/interface.ts` |
| 不新建页面 | `/task/:task_id` 复用 |

---

## 3. 验证

### 3.1 后端 API

```bash
cd backend

# 启动 backend（与前端 env 一致，建议 :8000 或统一改 frontend env）
ENV=DEV uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

# 列表
curl -s 'http://127.0.0.1:8000/tasks?limit=10' | jq .

# 与 CLI 交叉验证 task_id 集合一致
uv run python scripts/preview_logs.py --list
```

**通过标准：**

- [ ] 返回 JSON，`tasks` 为数组，按时间新→旧。
- [ ] 每个 `task_id` 在 `logs/messages/{id}.json` 存在。
- [ ] 非法 path（如 `../etc`）返回 400。

### 3.2 前端历史入口

```bash
cd frontend && pnpm run dev
# 确认 VITE_API_BASE_URL 指向正在跑任务的 backend
```

**通过标准：**

- [ ] 打开 `/chat`，侧边栏「历史任务」出现已有 task_id 列表。
- [ ] 点击某项 → 跳转 `/task/{task_id}`。
- [ ] 详情页 Chat 区加载历史 system / agent / trace 消息（与 `logs/messages` 一致）。
- [ ] 对新任务：提交后进入 `/task/{id}`，侧边栏刷新后可见新 id（可在进入 chat 时 re-fetch 列表）。

### 3.3 回放 vs 实时（回归）

| 场景 | 操作 | 期望 |
|------|------|------|
| 已完成任务 | 侧栏点旧 task | 历史消息完整；WS 可连可无新消息 |
| 运行中任务 | 侧栏点当前 task 或从 chat 跳入 | WS 连接，trace pill 持续增加 |
| 端口一致 | 任务与前端同一 backend | 实时 trace 与侧栏列表同源 |
| 端口不一致 | 8001 跑任务、前端连 8000 | 列表可能空或历史不全 → **文档/运维说明**，非 MVP bug |

### 3.4 与 eval / preview 联动（开发验收）

```bash
cd backend
TASK=20260810-230647-ae480f0b  # 换成列表中任一 id

uv run python scripts/preview_logs.py --task-id $TASK --summary
uv run python scripts/eval_task.py --task-id $TASK
```

**通过标准：**

- [ ] `preview_logs` 摘要中的 `message_count`、阶段进度与前端所见一致。
- [ ] 任务跑完后 `eval_task` scorecard 可出；`has_res_md` 与 scorecard 中 `docx_smoke` 一致。

### 3.5 Lint

```bash
cd backend && make lint
cd frontend && npx biome check src/components/AppSidebar.vue src/apis/commonApi.ts
```

---

## 4. 推荐实施顺序

```
A1 后端 GET /tasks
  → A2 前端 API
  → A3 侧边栏列表 + router-link
  → 3.1 ~ 3.3 验证
  →（可选）B1~B3 polish
```

与 [iteration-plan.md](./iteration-plan.md) 的关系：**不阻塞**阶段 0~3 后端质量工作；可与阶段 3（E2E eval）并行，提升基线跑完后的**人工 review 效率**。

---

## 5. 参考命令速查

```bash
# 开发侧：最新任务摘要
cd backend && make logs

# 列出所有 task_id
uv run python scripts/preview_logs.py --list

# 前端任务页（需知 task_id）
open http://localhost:5173/task/{task_id}

# 实时盯 trace 文件
tail -f backend/logs/traces/{task_id}.jsonl
```
