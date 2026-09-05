"""Coder 渐进式 skill 加载与 Repair 上下文测试。"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from app.core.agents.coder_agent import CoderAgent
from app.core.functions import get_coder_tools, get_coder_tools_anthropic
from app.core.llm.llm import LLM
from app.core.llm.types import StandardResponse, ToolCall
from app.core.skills.registry import SkillRegistry
from app.services.redis_manager import redis_manager
from app.services.trace_recorder import trace_recorder
from app.tools.base_interpreter import BaseCodeInterpreter


class _Interpreter:
    """提供 Coder 测试所需的最小解释器行为。"""

    def __init__(self, *, fail_code: bool = False) -> None:
        self.fail_code = fail_code
        self.section = ""
        self.codes: list[str] = []

    def add_section(self, section: str) -> None:
        self.section = section

    async def execute_code(self, code: str) -> tuple[str, bool, str]:
        self.codes.append(code)
        if self.fail_code and code == "bad":
            return "", True, "NameError: failed"
        return f"code output: {code}", False, ""

    async def get_created_images(self, section: str) -> list[str]:
        assert section == self.section
        if section.startswith("ques"):
            return ["one.png", "two.png"]
        if section == "sensitivity_analysis":
            return ["one.png"]
        return ["one.png", "two.png"]

    def get_code_output(self, section: str) -> str:
        assert section == self.section
        return ""

    def get_section_artifacts(self, section: str) -> list[str]:
        assert section == self.section
        return []


def _write_skill(path: Path, body: str) -> None:
    """写入供 Coder 显式加载的最小 skill。"""
    path.write_text(
        "---\n"
        "name: visualization\n"
        "description: test visual skill\n"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )


def _coder(
    tmp_path: Path,
    *,
    fail_code: bool = False,
    max_chat_turns: int = 12,
    max_retries: int = 3,
) -> CoderAgent:
    """构造使用临时 L1 registry 的 Coder。"""
    coder = CoderAgent(
        "task",
        cast(LLM, SimpleNamespace(api_type=None)),
        str(tmp_path),
        max_chat_turns=max_chat_turns,
        max_retries=max_retries,
        code_interpreter=cast(BaseCodeInterpreter, _Interpreter(fail_code=fail_code)),
    )
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir / "visualization.md", "UNIQUE SKILL BODY")
    coder._skill_registry = SkillRegistry(skills_dir)
    coder._tools = get_coder_tools(coder._skill_registry)
    return coder


def _interpreter(coder: CoderAgent) -> _Interpreter:
    """取回测试 Coder 共享的解释器替身。"""
    return cast(_Interpreter, coder.code_interpreter)


def _disable_side_effects(monkeypatch) -> list[tuple[str, dict]]:
    """捕获 trace，避免依赖 Redis 或 trace 文件。"""
    events: list[tuple[str, dict]] = []

    async def noop(*args, **kwargs) -> None:
        return None

    async def emit(task_id: str, event: str, **payload) -> None:
        events.append((event, payload))

    monkeypatch.setattr(redis_manager, "publish_message", noop)
    monkeypatch.setattr(trace_recorder, "emit", emit)
    return events


def _history_text(history: list[dict]) -> str:
    """将消息内容连接为便于断言的文本。"""
    return "\n".join(str(message.get("content") or "") for message in history)


def test_phase_starts_with_l1_schema_not_preloaded_body(monkeypatch, tmp_path: Path):
    """首次 Coder 请求只包含任务上下文和 L1 schema。"""
    _disable_side_effects(monkeypatch)
    coder = _coder(tmp_path)
    responses = iter(
        [
            StandardResponse(
                tool_calls=[
                    ToolCall(
                        "load",
                        "load_skill",
                        '{"skill_name":"visualization"}',
                    )
                ]
            ),
            StandardResponse(content="completion"),
            StandardResponse(content="final"),
        ]
    )
    calls: list[dict] = []

    async def fake_chat(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        return next(responses)

    monkeypatch.setattr(coder, "_chat", fake_chat)
    result = asyncio.run(coder.run("current task", "ques1"))

    assert result.status == "success"
    assert "UNIQUE SKILL BODY" not in _history_text(calls[0]["history"])
    assert "skill-preloaded" not in _history_text(calls[0]["history"])
    skill_schema = calls[0]["tools"][1]["function"]["description"]
    assert "visualization: test visual skill" in skill_schema
    assert "UNIQUE SKILL BODY" not in skill_schema
    assert "UNIQUE SKILL BODY" in _history_text(calls[1]["history"])


def test_explicit_load_records_tool_and_skill_trace(monkeypatch, tmp_path: Path):
    """成功 L2 调用必须有一一对应的 tool result 与版本化 trace。"""
    events = _disable_side_effects(monkeypatch)
    coder = _coder(tmp_path)
    responses = iter(
        [
            StandardResponse(
                tool_calls=[
                    ToolCall(
                        "load-call",
                        "load_skill",
                        '{"skill_name":"visualization"}',
                    )
                ]
            ),
            StandardResponse(content="completion"),
            StandardResponse(content="final"),
        ]
    )

    async def fake_chat(**kwargs):
        return next(responses)

    monkeypatch.setattr(coder, "_chat", fake_chat)
    asyncio.run(coder.run("solve", "ques1"))

    tools = [
        message
        for message in coder.chat_history
        if message.get("role") == "tool" and message.get("name") == "load_skill"
    ]
    assert len(tools) == 1
    assert tools[0]["tool_call_id"] == "load-call"
    skill_events = [payload for event, payload in events if event == "skill.load"]
    assert skill_events == [
        {
            "agent": "CoderAgent",
            "phase": "ques1",
            "tool_call_id": "load-call",
            "skill_name": "visualization",
            "found": True,
            "version": coder._loaded_skill_versions["visualization"],
            "body_chars": len("UNIQUE SKILL BODY"),
            "load_source": "tool_call",
            "loaded_skill_count": 1,
        }
    ]
    tool_events = [payload for event, payload in events if event == "tool.call"]
    assert tool_events[0]["tool_call_id"] == "load-call"


def test_same_response_loads_each_skill_call(monkeypatch, tmp_path: Path):
    """同一模型响应中的多个 L2 调用必须逐一回填。"""
    events = _disable_side_effects(monkeypatch)
    coder = _coder(tmp_path)
    responses = iter(
        [
            StandardResponse(
                tool_calls=[
                    ToolCall(
                        "first-load",
                        "load_skill",
                        '{"skill_name":"visualization"}',
                    ),
                    ToolCall(
                        "second-load",
                        "load_skill",
                        '{"skill_name":"visualization"}',
                    ),
                ]
            ),
            StandardResponse(content="completion"),
            StandardResponse(content="final"),
        ]
    )

    async def fake_chat(**kwargs):
        return next(responses)

    monkeypatch.setattr(coder, "_chat", fake_chat)
    asyncio.run(coder.run("solve", "ques1"))

    tool_results = [
        message
        for message in coder.chat_history
        if message.get("role") == "tool" and message.get("name") == "load_skill"
    ]
    assert [message["tool_call_id"] for message in tool_results] == [
        "first-load",
        "second-load",
    ]
    assert len([event for event, _ in events if event == "skill.load"]) == 2


def test_mixed_skill_and_code_calls_are_executed_and_replied_in_order(
    monkeypatch,
    tmp_path: Path,
):
    """同一响应混合 L2 与多次 execute_code 时，所有 call 都要按顺序回填。"""
    _disable_side_effects(monkeypatch)
    coder = _coder(tmp_path)
    responses = iter(
        [
            StandardResponse(
                tool_calls=[
                    ToolCall(
                        "load",
                        "load_skill",
                        '{"skill_name":"visualization"}',
                    ),
                    ToolCall("one", "execute_code", '{"code":"first"}'),
                    ToolCall("two", "execute_code", '{"code":"second"}'),
                ]
            ),
            StandardResponse(content="completion"),
            StandardResponse(content="final"),
        ]
    )
    async def fake_chat(**kwargs):
        return next(responses)

    monkeypatch.setattr(coder, "_chat", fake_chat)
    result = asyncio.run(coder.run("solve", "ques1"))

    assert result.status == "success"
    assert _interpreter(coder).codes == ["first", "second"]
    tool_results = [
        (message["tool_call_id"], message["content"])
        for message in coder.chat_history
        if message.get("role") == "tool"
    ]
    assert [call_id for call_id, _ in tool_results] == ["load", "one", "two"]
    assert "UNIQUE SKILL BODY" in tool_results[0][1]
    assert tool_results[1][1] == "code output: first"
    assert tool_results[2][1] == "code output: second"


def test_multiple_execute_calls_all_reply_before_partial_package(
    monkeypatch,
    tmp_path: Path,
):
    """中间代码失败也不能遗漏同轮后续 tool result，随后以 partial 继续交付。"""
    _disable_side_effects(monkeypatch)
    coder = _coder(tmp_path, fail_code=True, max_retries=1)
    responses = iter(
        [
            StandardResponse(
                tool_calls=[
                    ToolCall("one", "execute_code", '{"code":"first"}'),
                    ToolCall("bad", "execute_code", '{"code":"bad"}'),
                    ToolCall("two", "execute_code", '{"code":"second"}'),
                ]
            )
        ]
    )
    tool_results: list[tuple[str, str]] = []
    original_append = coder.append_chat_history

    async def fake_chat(**kwargs):
        return next(responses)

    async def capture_append(message: dict) -> None:
        await original_append(message)
        if message.get("role") == "tool":
            tool_results.append((message["tool_call_id"], message["content"]))

    monkeypatch.setattr(coder, "_chat", fake_chat)
    monkeypatch.setattr(coder, "append_chat_history", capture_append)
    result = asyncio.run(coder.run("solve", "ques1"))

    assert result.status == "partial"
    assert _interpreter(coder).codes == ["first", "bad", "second"]
    assert result.result_package is not None
    assert result.result_package.status == "partial"
    assert [call_id for call_id, _ in tool_results] == ["one", "bad", "two"]
    assert tool_results[0][1] == "code output: first"
    assert tool_results[1][1] == "NameError: failed"
    assert tool_results[2][1] == "code output: second"
    assert (tmp_path / "result_packages" / "ques1.json").is_file()


def test_invalid_or_unknown_loads_return_one_tool_result_each(monkeypatch, tmp_path: Path):
    """无效 L2 请求不能逃逸重试循环，且每个调用都有唯一返回。"""
    events = _disable_side_effects(monkeypatch)
    coder = _coder(tmp_path)
    responses = iter(
        [
            StandardResponse(
                tool_calls=[ToolCall("bad-json", "load_skill", "{")]
            ),
            StandardResponse(
                tool_calls=[ToolCall("empty", "load_skill", '{"skill_name":" "}')]
            ),
            StandardResponse(
                tool_calls=[ToolCall("unknown", "load_skill", '{"skill_name":"missing"}')]
            ),
            StandardResponse(content="completion"),
            StandardResponse(content="final"),
        ]
    )

    async def fake_chat(**kwargs):
        return next(responses)

    monkeypatch.setattr(coder, "_chat", fake_chat)
    result = asyncio.run(coder.run("solve", "ques1"))

    assert result.status == "success"
    results = {
        message["tool_call_id"]: message["content"]
        for message in coder.chat_history
        if message.get("role") == "tool"
    }
    assert set(results) == {"bad-json", "empty", "unknown"}
    assert "参数无效" in results["bad-json"]
    assert "skill_name" in results["empty"]
    assert "missing" in results["unknown"]
    skill_events = [payload for event, payload in events if event == "skill.load"]
    assert [event["found"] for event in skill_events] == [False, False, False]
    assert all(event["version"] is None for event in skill_events)


@pytest.mark.parametrize("phase", ["eda", "ques1", "sensitivity_analysis"])
def test_no_phase_automatically_loads_skill(monkeypatch, tmp_path: Path, phase: str):
    """每种执行 phase 都只能由模型发起 L2 加载。"""
    events = _disable_side_effects(monkeypatch)
    coder = _coder(tmp_path)
    responses = iter(
        [StandardResponse(content="completion"), StandardResponse(content="final")]
    )

    async def fake_chat(**kwargs):
        return next(responses)

    monkeypatch.setattr(coder, "_chat", fake_chat)
    asyncio.run(coder.run("solve", phase))

    assert not [event for event, _ in events if event == "skill.load"]
    assert "skill-preloaded" not in _history_text(coder.chat_history)


def test_repair_history_keeps_only_loaded_skill_identity(monkeypatch, tmp_path: Path):
    """Repair 重建历史时只保留当前 phase 的 L2 名称和版本。"""
    events = _disable_side_effects(monkeypatch)
    coder = _coder(tmp_path, fail_code=True)
    responses = iter(
        [
            StandardResponse(content="previous completion"),
            StandardResponse(content="previous final"),
            StandardResponse(
                tool_calls=[
                    ToolCall(
                        "first-load",
                        "load_skill",
                        '{"skill_name":"visualization"}',
                    )
                ]
            ),
            StandardResponse(
                tool_calls=[ToolCall("bad-code", "execute_code", '{"code":"bad"}')]
            ),
            StandardResponse(
                tool_calls=[
                    ToolCall(
                        "second-load",
                        "load_skill",
                        '{"skill_name":"visualization"}',
                    )
                ]
            ),
            StandardResponse(content="current completion"),
            StandardResponse(content="current final"),
        ]
    )
    calls: list[dict] = []

    async def fake_chat(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        return next(responses)

    monkeypatch.setattr(coder, "_chat", fake_chat)
    asyncio.run(coder.run("previous phase prompt", "eda"))
    asyncio.run(coder.run("current phase prompt", "ques1"))

    repair_history = calls[4]["history"]
    repair_text = _history_text(repair_history)
    version = coder._loaded_skill_versions["visualization"]
    assert "previous phase prompt" not in repair_text
    assert "current phase prompt" in repair_text
    assert f"visualization@{version}" in repair_text
    assert "UNIQUE SKILL BODY" not in repair_text
    assert any(
        event == "react.reflect"
        and payload["repair_context"] == "short"
        and payload["retained_skill_versions"] == {"visualization": version}
        for event, payload in events
    )
    tool_results = {
        message["tool_call_id"]: message["content"]
        for message in coder.chat_history
        if message.get("role") == "tool"
    }
    assert set(tool_results) == {"second-load"}
    assert "UNIQUE SKILL BODY" in tool_results["second-load"]


def test_phase_start_resets_history_and_turn_counter(monkeypatch, tmp_path: Path):
    """phase 切换隔离对话与 turn 计数，但复用同一解释器和工作目录。"""
    events = _disable_side_effects(monkeypatch)
    coder = _coder(tmp_path)
    interpreter = _interpreter(coder)
    responses = iter(
        [
            StandardResponse(content="eda completion"),
            StandardResponse(content="eda final"),
            StandardResponse(content="ques completion"),
            StandardResponse(content="ques final"),
        ]
    )
    histories: list[list[dict]] = []

    async def fake_chat(**kwargs):
        histories.append(copy.deepcopy(kwargs["history"]))
        return next(responses)

    monkeypatch.setattr(coder, "_chat", fake_chat)
    asyncio.run(coder.run("eda private prompt", "eda"))
    asyncio.run(coder.run("ques private prompt", "ques1"))

    assert coder.code_interpreter is interpreter
    assert "eda private prompt" not in _history_text(histories[2])
    assert "ques private prompt" in _history_text(histories[2])
    react_turns = [
        payload
        for event, payload in events
        if event == "react.turn" and payload["phase"] in {"eda", "ques1"}
    ]
    assert [payload["turn"] for payload in react_turns] == [1, 2, 1, 2]


def test_openai_and_anthropic_schemas_expose_only_l1_descriptions(tmp_path: Path):
    """不同 provider 的 tool schema 不能泄露 L2 正文。"""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir / "visualization.md", "UNIQUE SKILL BODY")
    registry = SkillRegistry(skills_dir)

    openai_description = get_coder_tools(registry)[1]["function"]["description"]
    anthropic_description = get_coder_tools_anthropic(registry)[1]["description"]

    assert "visualization: test visual skill" in openai_description
    assert "visualization: test visual skill" in anthropic_description
    assert "UNIQUE SKILL BODY" not in openai_description
    assert "UNIQUE SKILL BODY" not in anthropic_description
