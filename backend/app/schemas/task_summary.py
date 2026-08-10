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
