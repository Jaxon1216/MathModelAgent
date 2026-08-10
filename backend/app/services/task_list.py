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
    """读取 messages JSON 文件。"""
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
