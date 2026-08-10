# Frontend Task History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让前端侧边栏「历史任务」列出本地已有 task，点击后进入已有 `/task/:task_id` 页回放对话（产品侧预览），而不是 CLI 日志工具。

**Architecture:** 后端新增纯读盘 `GET /tasks`，扫描 `logs/messages/*.json` 生成任务摘要；前端在 `AppSidebar` 拉取列表并用 `router-link` 跳到现有任务详情页。详情页已有 `loadTaskMessages` + WebSocket，MVP 不新造页面、不改 Agent 主链路。

**Tech Stack:** FastAPI + Pydantic、Vue 3 + Pinia + Vue Router、axios（`frontend/src/utils/request.ts`）、pytest。

## Global Constraints

- 不做 CLI：`preview_logs.py` / `make logs` 不在本计划内（已从错误提交中 revert）。
- 不新建任务详情页：复用 `frontend/src/pages/task/index.vue` 与路由 `/task/:task_id`。
- 不修改 `frontend/src/components/ui/`（shadcn-vue 生成代码）。
- 列表数据源以 `logs/messages/` 为主（有对话才可回放）；不引入 Redis/DB。
- 路径安全：所有 task_id 经 `ensure_safe_task_id`（`TASK_ID_PATTERN = ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`）。
- 后端 cwd 假定为 `backend/`（与现有 `Path("logs/messages")`、`project/work_dir` 一致）。
- 阶段交付：先 MVP（Task 1–4），再可选 polish（Task 5）。
- 提交信息格式：`<type>: <描述>`（feat/fix/refactor/chore/enhance/docs）。

## File Structure

| 文件 | 职责 |
|------|------|
| `backend/app/schemas/task_summary.py` | `TaskStatus` / `TaskSummary` / `TaskListResponse` 响应模型 |
| `backend/app/services/task_list.py` | 扫目录、推状态、组装列表（可单测，不绑 FastAPI） |
| `backend/app/routers/common_router.py` | 暴露 `GET /tasks`，调用 service |
| `backend/app/tests/test_task_list.py` | service + 路由行为单测 |
| `frontend/src/utils/interface.ts` | `TaskSummary` / `TaskListResponse` TS 类型 |
| `frontend/src/apis/commonApi.ts` | `listTasks(limit)` |
| `frontend/src/components/AppSidebar.vue` | 拉取列表、渲染历史入口、「开始新任务」链到 `/chat` |

**刻意不改（MVP）：** `stores/task.ts`、`pages/task/index.vue`（回放已可用）、`router/index.ts`。

**现状锚点（实现时对照）：**

- `AppSidebar` 仅挂在 `frontend/src/pages/chat/index.vue`；任务页全屏无侧栏，MVP 可接受。
- 终端 system 文案在 `modeling_router.py`：`"任务处理完成"`/`success`、`"任务已停止"`/`warning`、`"任务执行失败: ..."`/`error`。中间也有大量 `type=success`（如「代码手求解成功」），**状态必须以终端文案匹配，不能只看 `type`**。

---

### Task 1: TaskSummary 模型 + list_tasks 纯函数

**Files:**
- Create: `backend/app/schemas/task_summary.py`
- Create: `backend/app/services/task_list.py`
- Test: `backend/app/tests/test_task_list.py`

**Interfaces:**
- Consumes: `ensure_safe_task_id` from `app.utils.common_utils`
- Produces:
  - `TaskStatus = Literal["running", "completed", "failed", "cancelled", "unknown"]`
  - `class TaskSummary(BaseModel)` with fields: `task_id: str`, `updated_at: str` (ISO8601), `message_count: int`, `has_work_dir: bool`, `has_res_md: bool`, `status: TaskStatus`
  - `class TaskListResponse(BaseModel)` with `tasks: list[TaskSummary]`
  - `def infer_task_status(messages: list[dict]) -> TaskStatus`
  - `def list_tasks(*, messages_dir: Path, work_dir_root: Path, limit: int = 50) -> list[TaskSummary]`

- [ ] **Step 1: Write the failing tests**

Create `backend/app/tests/test_task_list.py`:

```python
"""任务列表服务单元测试。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.services.task_list import infer_task_status, list_tasks


def _write_messages(path: Path, messages: list[dict], mtime: float | None = None) -> None:
    path.write_text(json.dumps(messages, ensure_ascii=False), encoding="utf-8")
    if mtime is not None:
        import os

        os.utime(path, (mtime, mtime))


def test_infer_status_completed():
    messages = [
        {"msg_type": "system", "type": "info", "content": "任务开始处理"},
        {"msg_type": "system", "type": "success", "content": "代码手求解成功ques1"},
        {"msg_type": "system", "type": "success", "content": "任务处理完成"},
    ]
    assert infer_task_status(messages) == "completed"


def test_infer_status_prefers_terminal_over_mid_success():
    """中间 success 不应把未结束任务判成 completed。"""
    messages = [
        {"msg_type": "system", "type": "success", "content": "代码手求解成功ques1"},
        {"msg_type": "system", "type": "info", "content": "开始执行代码"},
    ]
    assert infer_task_status(messages) == "running"


def test_infer_status_cancelled_and_failed():
    assert (
        infer_task_status(
            [{"msg_type": "system", "type": "warning", "content": "任务已停止"}]
        )
        == "cancelled"
    )
    assert (
        infer_task_status(
            [{"msg_type": "system", "type": "error", "content": "任务执行失败: boom"}]
        )
        == "failed"
    )


def test_list_tasks_sorted_by_mtime_and_limit(tmp_path: Path):
    messages_dir = tmp_path / "messages"
    work_root = tmp_path / "work_dir"
    messages_dir.mkdir()
    work_root.mkdir()

    older = messages_dir / "20260101-100000-aaaaaaaa.json"
    newer = messages_dir / "20260810-230647-ae480f0b.json"
    _write_messages(
        older,
        [{"msg_type": "system", "type": "success", "content": "任务处理完成"}],
        mtime=1_700_000_000,
    )
    _write_messages(
        newer,
        [{"msg_type": "system", "type": "info", "content": "任务开始处理"}],
        mtime=1_800_000_000,
    )

    task_work = work_root / "20260810-230647-ae480f0b"
    task_work.mkdir()
    (task_work / "res.md").write_text("# ok", encoding="utf-8")

    tasks = list_tasks(messages_dir=messages_dir, work_dir_root=work_root, limit=1)
    assert len(tasks) == 1
    assert tasks[0].task_id == "20260810-230647-ae480f0b"
    assert tasks[0].has_work_dir is True
    assert tasks[0].has_res_md is True
    assert tasks[0].status == "running"
    assert tasks[0].message_count == 1
    # updated_at 可解析
    datetime.fromisoformat(tasks[0].updated_at)


def test_list_tasks_skips_invalid_filenames(tmp_path: Path):
    messages_dir = tmp_path / "messages"
    work_root = tmp_path / "work_dir"
    messages_dir.mkdir()
    work_root.mkdir()
    (messages_dir / "../evil.json").write_text("[]", encoding="utf-8")  # 不会出现在 glob stem 正常名外
    (messages_dir / "not a id.json").write_text("[]", encoding="utf-8")
    _write_messages(messages_dir / "ok-task-1.json", [])

    tasks = list_tasks(messages_dir=messages_dir, work_dir_root=work_root, limit=50)
    assert [t.task_id for t in tasks] == ["ok-task-1"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && uv run pytest app/tests/test_task_list.py -v`

Expected: FAIL with `ModuleNotFoundError` for `app.services.task_list`（或 import 错误）。

- [ ] **Step 3: Write minimal schemas + service**

Create `backend/app/schemas/task_summary.py`:

```python
"""任务列表摘要响应模型。"""

from typing import Literal

from pydantic import BaseModel, Field

TaskStatus = Literal["running", "completed", "failed", "cancelled", "unknown"]


class TaskSummary(BaseModel):
    """单个历史任务摘要。"""

    task_id: str
    updated_at: str
    message_count: int = 0
    has_work_dir: bool = False
    has_res_md: bool = False
    status: TaskStatus = "unknown"


class TaskListResponse(BaseModel):
    """任务列表响应。"""

    tasks: list[TaskSummary] = Field(default_factory=list)
```

Create `backend/app/services/task_list.py`:

```python
"""本地历史任务列表：扫描 messages 目录生成摘要。"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app.schemas.task_summary import TaskStatus, TaskSummary
from app.utils.common_utils import ensure_safe_task_id
from app.utils.log_util import logger

_TERMINAL_COMPLETED = "任务处理完成"
_TERMINAL_CANCELLED = "任务已停止"
_TERMINAL_FAILED_PREFIX = "任务执行失败"


def infer_task_status(messages: list[dict]) -> TaskStatus:
    """从消息列表推断任务终态。

    只认 modeling_router 写入的终端 system 文案；忽略中间阶段的 success。

    Args:
        messages: messages JSON 数组。

    Returns:
        任务状态。
    """
    for msg in reversed(messages):
        if msg.get("msg_type") != "system":
            continue
        content = (msg.get("content") or "").strip()
        if content == _TERMINAL_COMPLETED:
            return "completed"
        if content == _TERMINAL_CANCELLED:
            return "cancelled"
        if content.startswith(_TERMINAL_FAILED_PREFIX):
            return "failed"
    return "running" if messages else "unknown"


def _load_messages(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as exc:  # noqa: BLE001 — 单文件损坏不应拖垮列表
        logger.error(f"读取任务消息失败 {path}: {exc}")
        return []


def list_tasks(
    *,
    messages_dir: Path,
    work_dir_root: Path,
    limit: int = 50,
) -> list[TaskSummary]:
    """按 mtime 倒序列出历史任务。

    Args:
        messages_dir: `logs/messages` 目录。
        work_dir_root: `project/work_dir` 目录。
        limit: 返回条数上限（<=0 视为 0）。

    Returns:
        任务摘要列表。
    """
    if limit <= 0 or not messages_dir.exists():
        return []

    files = sorted(
        messages_dir.glob("*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    results: list[TaskSummary] = []
    for path in files:
        if len(results) >= limit:
            break
        try:
            task_id = ensure_safe_task_id(path.stem)
        except ValueError:
            continue

        messages = _load_messages(path)
        work_dir = work_dir_root / task_id
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        results.append(
            TaskSummary(
                task_id=task_id,
                updated_at=mtime.isoformat(),
                message_count=len(messages),
                has_work_dir=work_dir.is_dir(),
                has_res_md=(work_dir / "res.md").is_file(),
                status=infer_task_status(messages),
            )
        )
    return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && uv run pytest app/tests/test_task_list.py -v`

Expected: PASS（全部绿）。若 `not a id.json` 的 stem 含空格被 `ensure_safe_task_id` 拒绝则符合预期；若测试文件名写法与 OS 冲突，把非法样例改成 `bad..json` 以外、明确不匹配 `TASK_ID_PATTERN` 的 stem（例如 `bad id.json` 或 `__bad__.json` 视 pattern 而定——空格必挂）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/schemas/task_summary.py backend/app/services/task_list.py backend/app/tests/test_task_list.py
git commit -m "$(cat <<'EOF'
feat: 添加历史任务列表纯函数与单测

为前端侧边栏提供可测的 messages 目录扫描与终态推断。
EOF
)"
```

---

### Task 2: FastAPI `GET /tasks`

**Files:**
- Modify: `backend/app/routers/common_router.py`
- Test: `backend/app/tests/test_task_list.py`（追加路由测试）

**Interfaces:**
- Consumes: `list_tasks`, `TaskListResponse`
- Produces: HTTP `GET /tasks?limit=50` → `{"tasks":[...]}`；非法 limit 用默认；不接受用户传入任意 path

- [ ] **Step 1: Write the failing route test**

Append to `backend/app/tests/test_task_list.py`:

```python
from fastapi.testclient import TestClient

from app.main import app


def test_get_tasks_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    messages_dir = tmp_path / "messages"
    work_root = tmp_path / "work_dir"
    messages_dir.mkdir()
    work_root.mkdir()
    _write_messages(
        messages_dir / "20260810-230647-ae480f0b.json",
        [{"msg_type": "system", "type": "success", "content": "任务处理完成"}],
    )

    # 路由内部使用固定相对路径；测试时把 cwd 指到 tmp，或 monkeypatch list_tasks 所用 Path
    monkeypatch.chdir(tmp_path)
    # 路由实现必须调用 Path("logs/messages") / Path("project/work_dir")
    (tmp_path / "logs").mkdir()
    (tmp_path / "project").mkdir()
    messages_dir.rename(tmp_path / "logs" / "messages")
    work_root.rename(tmp_path / "project" / "work_dir")

    client = TestClient(app)
    res = client.get("/tasks", params={"limit": 10})
    assert res.status_code == 200
    body = res.json()
    assert "tasks" in body
    assert body["tasks"][0]["task_id"] == "20260810-230647-ae480f0b"
    assert body["tasks"][0]["status"] == "completed"
```

注意：`TestClient(app)` 会触发 `lifespan`（创建 `./project`）。`monkeypatch.chdir(tmp_path)` 后应仍可用。若 lifespan 与 chdir 打架，改为在路由里注入可 patch 的默认目录常量，并在测试中：

```python
monkeypatch.setattr(
    "app.routers.common_router.MESSAGES_DIR",
    tmp_path / "logs" / "messages",
)
monkeypatch.setattr(
    "app.routers.common_router.WORK_DIR_ROOT",
    tmp_path / "project" / "work_dir",
)
```

**推荐路由写法（实现时采用可 patch 常量，避免 chdir 脆弱）：** 见 Step 3。若选常量方案，相应改写本测试为 monkeypatch 常量，不要 chdir。

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest app/tests/test_task_list.py::test_get_tasks_endpoint -v`

Expected: FAIL（404 或无 `/tasks`）。

- [ ] **Step 3: Implement route**

在 `backend/app/routers/common_router.py` 顶部追加 import 与常量，并新增 endpoint（保持现有 `/messages` 不动）：

```python
from app.schemas.task_summary import TaskListResponse
from app.services.task_list import list_tasks

MESSAGES_DIR = Path("logs/messages")
WORK_DIR_ROOT = Path("project/work_dir")


@router.get("/tasks", response_model=TaskListResponse)
async def get_tasks(limit: int = 50) -> TaskListResponse:
    """列出本地历史任务摘要（按消息文件 mtime 倒序）。

    Args:
        limit: 返回条数，默认 50；小于 1 时返回空列表。

    Returns:
        任务摘要列表。
    """
    capped = min(limit, 200)
    tasks = list_tasks(
        messages_dir=MESSAGES_DIR,
        work_dir_root=WORK_DIR_ROOT,
        limit=capped,
    )
    return TaskListResponse(tasks=tasks)
```

把 Step 1 的测试改成 monkeypatch `MESSAGES_DIR` / `WORK_DIR_ROOT`（推荐），删除 chdir 方案以免踩 lifespan。

- [ ] **Step 4: Run tests**

Run: `cd backend && uv run pytest app/tests/test_task_list.py -v && make lint`

Expected: 全部 PASS；ruff 无新增问题。

- [ ] **Step 5: Manual smoke（有本地 messages 时）**

```bash
cd backend
ENV=DEV uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
curl -s 'http://127.0.0.1:8000/tasks?limit=5' | jq .
```

Expected: `tasks` 数组，id 与 `ls logs/messages` 一致。

- [ ] **Step 6: Commit**

```bash
git add backend/app/routers/common_router.py backend/app/tests/test_task_list.py
git commit -m "$(cat <<'EOF'
feat: 新增 GET /tasks 历史任务列表 API

供前端侧边栏枚举可回放的本地任务。
EOF
)"
```

---

### Task 3: 前端 API 与类型

**Files:**
- Modify: `frontend/src/utils/interface.ts`
- Modify: `frontend/src/apis/commonApi.ts`

**Interfaces:**
- Consumes: `GET /tasks`（Task 2）
- Produces:
  - `export type TaskStatus = "running" | "completed" | "failed" | "cancelled" | "unknown"`
  - `export interface TaskSummary { ... }`（字段与后端一致）
  - `export interface TaskListResponse { tasks: TaskSummary[] }`
  - `export function listTasks(limit = 50)` → `request.get<TaskListResponse>("/tasks", { params: { limit } })`

- [ ] **Step 1: Add types to `interface.ts`**

在 `frontend/src/utils/interface.ts` 末尾追加：

```ts
/** 历史任务状态（与后端 TaskStatus 一致） */
export type TaskStatus =
	| "running"
	| "completed"
	| "failed"
	| "cancelled"
	| "unknown";

/** 历史任务摘要 */
export interface TaskSummary {
	task_id: string;
	updated_at: string;
	message_count: number;
	has_work_dir: boolean;
	has_res_md: boolean;
	status: TaskStatus;
}

/** 任务列表响应 */
export interface TaskListResponse {
	tasks: TaskSummary[];
}
```

- [ ] **Step 2: Add `listTasks` to `commonApi.ts`**

```ts
import type { TaskListResponse } from "@/utils/interface";

/**
 * 获取本地历史任务列表
 * @param limit 返回条数，默认 50
 */
export function listTasks(limit = 50) {
	return request.get<TaskListResponse>("/tasks", {
		params: { limit },
	});
}
```

（保留文件内既有 import；把 `TaskListResponse` 并入现有 import 行即可。）

- [ ] **Step 3: Typecheck / lint touched files**

```bash
source ~/.zshrc
cd frontend && npx biome check src/utils/interface.ts src/apis/commonApi.ts
```

Expected: 无 error（warning 可按仓库惯例处理）。

- [ ] **Step 4: Commit**

```bash
git add frontend/src/utils/interface.ts frontend/src/apis/commonApi.ts
git commit -m "$(cat <<'EOF'
feat: 前端添加 listTasks API 与 TaskSummary 类型
EOF
)"
```

---

### Task 4: AppSidebar 填充历史任务（MVP 交付）

**Files:**
- Modify: `frontend/src/components/AppSidebar.vue`

**Interfaces:**
- Consumes: `listTasks()` from Task 3；路由 `/task/:task_id`、`/chat`
- Produces: 侧边栏可点击历史项；空态文案；「开始新任务」→ `/chat`

- [ ] **Step 1: Replace static `data.navMain` with reactive list**

将 `AppSidebar.vue` 的 `<script setup>` 改为（保留 socialMedia 与 ui import；按仓库风格用 tab 缩进）：

```ts
import { listTasks } from "@/apis/commonApi";
import type { TaskSummary } from "@/utils/interface";
import { computed, onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import NavUser from "./NavUser.vue";
// ... 保留现有 sidebar UI imports ...

const props = defineProps<SidebarProps>();
const route = useRoute();

const historyTasks = ref<TaskSummary[]>([]);
const historyLoading = ref(false);
const historyError = ref<string | null>(null);

async function refreshHistory() {
	historyLoading.value = true;
	historyError.value = null;
	try {
		const res = await listTasks(50);
		historyTasks.value = res.data?.tasks ?? [];
	} catch (e) {
		console.error("加载历史任务失败:", e);
		historyError.value = "加载失败";
		historyTasks.value = [];
	} finally {
		historyLoading.value = false;
	}
}

function shortTaskId(taskId: string): string {
	return taskId.length > 20 ? `${taskId.slice(0, 18)}…` : taskId;
}

function formatUpdatedAt(iso: string): string {
	try {
		const d = new Date(iso);
		return d.toLocaleString();
	} catch {
		return iso;
	}
}

const activeTaskId = computed(() => {
	const id = route.params.task_id;
	return typeof id === "string" ? id : null;
});

onMounted(() => {
	void refreshHistory();
});

// socialMedia 数组保持不变
```

- [ ] **Step 2: Update template**

把「开始 / 历史任务」两段改成（不要用 `<a href>` 做站内跳转）：

```vue
<SidebarContent>
  <SidebarGroup>
    <SidebarGroupLabel>开始</SidebarGroupLabel>
    <SidebarGroupContent>
      <SidebarMenu>
        <SidebarMenuItem>
          <SidebarMenuButton as-child :is-active="route.path === '/chat'">
            <router-link to="/chat">开始新任务</router-link>
          </SidebarMenuButton>
        </SidebarMenuItem>
      </SidebarMenu>
    </SidebarGroupContent>
  </SidebarGroup>

  <SidebarGroup>
    <SidebarGroupLabel>历史任务</SidebarGroupLabel>
    <SidebarGroupContent>
      <SidebarMenu>
        <SidebarMenuItem v-if="historyLoading">
          <span class="px-2 text-sm text-muted-foreground">加载中…</span>
        </SidebarMenuItem>
        <SidebarMenuItem v-else-if="historyError">
          <span class="px-2 text-sm text-muted-foreground">{{ historyError }}</span>
        </SidebarMenuItem>
        <SidebarMenuItem v-else-if="historyTasks.length === 0">
          <span class="px-2 text-sm text-muted-foreground">暂无历史任务</span>
        </SidebarMenuItem>
        <SidebarMenuItem
          v-for="task in historyTasks"
          v-else
          :key="task.task_id"
        >
          <SidebarMenuButton
            as-child
            :is-active="activeTaskId === task.task_id"
          >
            <router-link
              :to="`/task/${task.task_id}`"
              :title="`${task.task_id}\n${formatUpdatedAt(task.updated_at)}`"
            >
              <span class="truncate">{{ shortTaskId(task.task_id) }}</span>
            </router-link>
          </SidebarMenuButton>
        </SidebarMenuItem>
      </SidebarMenu>
    </SidebarGroupContent>
  </SidebarGroup>
</SidebarContent>
```

注意：`v-for` 与 `v-else` 同节点在 Vue 3 合法；若 Biome/编译器抱怨，拆成 `v-else` 包裹的 `<template>` + 内部 `v-for`。

- [ ] **Step 3: Lint**

```bash
source ~/.zshrc
cd frontend && npx biome check --write src/components/AppSidebar.vue
```

Expected: 通过。

- [ ] **Step 4: Manual verification**

```bash
# terminal 1
cd backend && ENV=DEV uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

# terminal 2
source ~/.zshrc && cd frontend && pnpm run dev
```

确认 `frontend/.env.development` 的 `VITE_API_BASE_URL` 指向正在跑的 backend（默认 `http://localhost:8000`）。

通过标准：

- 打开 `/chat`，侧边栏「历史任务」出现已有 `task_id`。
- 点击一项 → `/task/{task_id}`，Chat 区加载历史消息（`GET /messages`）。
- 无任务时显示「暂无历史任务」。
- 「开始新任务」进入 `/chat`。

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/AppSidebar.vue
git commit -m "$(cat <<'EOF'
feat: 侧边栏展示可回放的历史任务列表

对接 GET /tasks，点击跳转已有任务详情页。
EOF
)"
```

---

### Task 5（可选）: 已完成任务只读 + 状态点

**Files:**
- Modify: `frontend/src/pages/task/index.vue`
- Modify: `frontend/src/components/AppSidebar.vue`

**Interfaces:**
- Consumes: `TaskSummary.status`；详情页可用 `listTasks` 或单次 messages 末态（MVP 后若不想多请求，可在进入页时用已有 messages 推断——但前端无 `infer_task_status`，优先：详情页 props 不变，用 `listTasks` 找当前 id 的 status，或新增轻量 `GET /tasks/{id}`——**YAGNI：直接在详情页根据加载后的 messages 做同样终态匹配**）

- [ ] **Step 1: 在 `task/index.vue` 根据终态跳过 WS**

加载 messages 后扫描是否存在终端文案；若 `completed|failed|cancelled`：

- 不调用 `connectWebSocket`
- 隐藏「停止」按钮
- 停表：`clearInterval(timer)`，时长可显示「已结束」

终态匹配字符串与后端保持一致：`任务处理完成` / `任务已停止` / `任务执行失败`。

- [ ] **Step 2: 侧栏状态色点**

在历史项旁加小圆点：`completed` 绿、`failed` 红、`running` 灰、`cancelled` 黄。仍不改 `components/ui/`。

- [ ] **Step 3: Lint + 手动点进已完成 / 运行中任务各一次**

- [ ] **Step 4: Commit**

```bash
git commit -m "$(cat <<'EOF'
enhance: 历史任务只读模式与侧栏状态点
EOF
)"
```

---

## Self-Review

1. **Spec coverage（对照原 task-history 文档 MVP）：**
   - A1 `GET /tasks` → Task 1–2
   - A2 前端 API → Task 3
   - A3 侧边栏 → Task 4
   - 复用 `/task/:task_id` → 明确不改路由
   - 验证 curl / 侧栏点击 → Task 2 Step 5、Task 4 Step 4
   - 阶段 B polish → Task 5 可选
   - **排除：** CLI `preview_logs` / `make logs`（用户明确不要）

2. **Placeholder scan:** 无 TBD；关键代码与命令已写出。

3. **Type consistency:** `TaskStatus` / `TaskSummary` 前后端字段名一致；`listTasks` → `TaskListResponse.tasks`。

4. **Status bug avoided:** 不用裸 `type=success`，只用终端文案。

---

## Out of Scope

- `backend/scripts/preview_logs.py`、`make logs`
- 独立历史页 `/tasks` 表格搜索（原文档 B4）
- Redis/DB 持久化任务元数据
- 统一 Docker/本机双 backend 端口（运维约定，非本功能）
