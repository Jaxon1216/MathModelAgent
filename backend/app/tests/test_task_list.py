"""任务列表服务单元测试。"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.task_list import infer_task_status, list_tasks


def _write_messages(
    path: Path, messages: list[dict], mtime: float | None = None
) -> None:
    path.write_text(json.dumps(messages, ensure_ascii=False), encoding="utf-8")
    if mtime is not None:
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
    datetime.fromisoformat(tasks[0].updated_at)


def test_list_tasks_skips_invalid_filenames(tmp_path: Path):
    messages_dir = tmp_path / "messages"
    work_root = tmp_path / "work_dir"
    messages_dir.mkdir()
    work_root.mkdir()
    _write_messages(messages_dir / "bad id.json", [])
    _write_messages(messages_dir / "ok-task-1.json", [])

    tasks = list_tasks(messages_dir=messages_dir, work_dir_root=work_root, limit=50)
    assert [t.task_id for t in tasks] == ["ok-task-1"]


def test_get_tasks_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    messages_dir = tmp_path / "messages"
    work_root = tmp_path / "work_dir"
    messages_dir.mkdir()
    work_root.mkdir()
    _write_messages(
        messages_dir / "20260810-230647-ae480f0b.json",
        [{"msg_type": "system", "type": "success", "content": "任务处理完成"}],
    )

    monkeypatch.setattr("app.routers.common_router.MESSAGES_DIR", messages_dir)
    monkeypatch.setattr("app.routers.common_router.WORK_DIR_ROOT", work_root)

    client = TestClient(app)
    res = client.get("/tasks", params={"limit": 10})
    assert res.status_code == 200
    body = res.json()
    assert body["tasks"][0]["task_id"] == "20260810-230647-ae480f0b"
    assert body["tasks"][0]["status"] == "completed"
