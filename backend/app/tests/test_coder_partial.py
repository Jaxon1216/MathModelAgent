"""Coder partial 交接与阶段产物验证测试。"""

from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from app.core.agents.coder_agent import CoderAgent
from app.core.llm.llm import LLM
from app.core.llm.types import StandardResponse, ToolCall
from app.services.redis_manager import redis_manager
from app.services.trace_recorder import trace_recorder
from app.tools.base_interpreter import BaseCodeInterpreter
from app.tools.notebook_serializer import NotebookSerializer


class _PartialInterpreter:
    """模拟已有成功证据后最后一次执行失败。"""

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir
        self.section = ""

    def add_section(self, section: str) -> None:
        self.section = section

    async def execute_code(self, code: str) -> tuple[str, bool, str]:
        del code
        return "", True, "KeyError: F_plots"

    async def get_created_images(self, section: str) -> list[str]:
        assert section == self.section
        return ["q1.png"]

    def get_code_output(self, section: str) -> str:
        assert section == self.section
        return "objective=123.5\nprofit=88.2"

    def get_section_artifacts(self, section: str) -> list[str]:
        assert section == self.section
        return ["q1.png", "result1_1.xlsx"]


def _disable_side_effects(monkeypatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []

    async def noop(*args, **kwargs):
        return None

    async def emit(task_id: str, event: str, **payload):
        events.append((event, payload))

    monkeypatch.setattr(redis_manager, "publish_message", noop)
    monkeypatch.setattr(trace_recorder, "emit", emit)
    return events


def test_retry_exhaustion_returns_partial_verified_evidence(
    monkeypatch, tmp_path: Path
):
    """重试耗尽时错误单独保存，Writer 只收到可读取产物与成功 stdout。"""
    events = _disable_side_effects(monkeypatch)
    (tmp_path / "q1.png").write_bytes(b"\x89PNG\r\n\x1a\ncontent")
    with zipfile.ZipFile(tmp_path / "result1_1.xlsx", "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
    interpreter = _PartialInterpreter(tmp_path)
    coder = CoderAgent(
        "task",
        cast(LLM, SimpleNamespace(api_type=None)),
        str(tmp_path),
        max_retries=1,
        code_interpreter=cast(BaseCodeInterpreter, interpreter),
    )

    responses = iter(
        [
            StandardResponse(
                tool_calls=[
                    ToolCall("execute", "execute_code", '{"code":"raise KeyError"}')
                ]
            )
        ]
    )

    async def fake_chat(**kwargs):
        return next(responses)

    monkeypatch.setattr(coder, "_chat", fake_chat)

    result = asyncio.run(coder.run("solve", "ques1"))

    assert result.status == "partial"
    assert result.created_images == ["q1.png"]
    assert result.created_artifacts == ["q1.png", "result1_1.xlsx"]
    assert result.verified_metrics == ["objective=123.5", "profit=88.2"]
    assert result.last_error == "KeyError: F_plots"
    assert "KeyError" not in (result.code_response or "")
    summaries = [payload for event, payload in events if event == "subtask.summary"]
    assert summaries[0]["status"] == "partial"
    assert summaries[0]["png_count"] == 1


class _SnapshotInterpreter(BaseCodeInterpreter):
    async def initialize(self):
        return None

    async def _pre_execute_code(self):
        return None

    async def execute_code(self, code: str) -> tuple[str, bool, str]:
        return code, False, ""

    async def cleanup(self):
        return None

    async def get_created_images(self, section: str) -> list[str]:
        return []


def test_section_artifacts_include_modified_existing_template(tmp_path: Path):
    """预置结果模板被修改后必须归属于当前 phase。"""
    template = tmp_path / "result1_1.xlsx"
    template.write_bytes(b"original")
    interpreter = _SnapshotInterpreter(
        "task",
        str(tmp_path),
        NotebookSerializer(str(tmp_path)),
    )
    interpreter.add_section("ques1")

    template.write_bytes(b"modified")

    assert interpreter.get_section_artifacts("ques1") == ["result1_1.xlsx"]
