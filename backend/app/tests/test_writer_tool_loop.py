"""Writer 有界工具循环、章节隔离与可信引用测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

from app.core.agents.writer_agent import WriterAgent
from app.core.llm.llm import LLM
from app.core.llm.providers.anthropic import AnthropicProvider
from app.core.llm.types import StandardResponse, ToolCall
from app.schemas.enums import CompTemplate, FormatOutPut
from app.services.redis_manager import redis_manager
from app.services.trace_recorder import trace_recorder
from app.tools.openalex_scholar import OpenAlexScholar


def _paper(identifier: str, title: str) -> dict:
    """构造一条稳定的 OpenAlex 测试结果。"""
    return {
        "openalex_id": f"https://openalex.org/{identifier}",
        "doi": f"https://doi.org/10.1000/{identifier.lower()}",
        "title": title,
        "authors": [{"name": "Ada Lovelace"}],
        "publication_year": 2026,
        "citation_format": f"Ada Lovelace (2026). {title}. DOI: 10.1000/test",
    }


class _Scholar:
    """记录查询并返回固定论文的检索替身。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.queries: list[str] = []

    async def search_papers(self, query: str):
        self.queries.append(query)
        if self.fail:
            raise RuntimeError("offline")
        return [_paper(f"W{len(self.queries)}", f"Paper {len(self.queries)}")]


def _writer(scholar: _Scholar, **kwargs) -> WriterAgent:
    """构造不连接真实模型的 Writer。"""
    return WriterAgent(
        "task",
        cast(LLM, SimpleNamespace(api_type=None)),
        comp_template=CompTemplate.CHINA,
        format_output=FormatOutPut.Markdown,
        scholar=cast(OpenAlexScholar, scholar),
        **kwargs,
    )


def _disable_side_effects(monkeypatch) -> list[tuple[str, dict]]:
    """关闭 Redis，并捕获 trace。"""
    events: list[tuple[str, dict]] = []

    async def noop(*args, **kwargs):
        return None

    async def emit(task_id: str, event: str, **payload):
        events.append((event, payload))

    monkeypatch.setattr(redis_manager, "publish_message", noop)
    monkeypatch.setattr(trace_recorder, "emit", emit)
    return events


def _tool_results(history: list[dict]) -> dict[str, str]:
    return {
        item["tool_call_id"]: item["content"]
        for item in history
        if item.get("role") == "tool"
    }


def test_writer_handles_all_calls_and_forces_final_after_two_rounds(monkeypatch):
    """连续工具调用达到上限后必须禁用工具生成最终正文。"""
    events = _disable_side_effects(monkeypatch)
    scholar = _Scholar()
    writer = _writer(scholar, max_tool_rounds=2, max_search_calls=4)
    responses = iter(
        [
            StandardResponse(
                tool_calls=[
                    ToolCall("one", "search_papers", '{"query":"first"}'),
                    ToolCall("two", "search_papers", '{"query":"second"}'),
                ]
            ),
            StandardResponse(
                tool_calls=[ToolCall("three", "search_papers", '{"query":"third"}')]
            ),
            StandardResponse(content="正文结论[[REF:R1]]"),
        ]
    )
    calls: list[dict] = []

    async def fake_chat(**kwargs):
        calls.append(kwargs)
        return next(responses)

    monkeypatch.setattr(writer, "_chat", fake_chat)

    result = asyncio.run(writer.run("write", sub_title="ques3"))

    assert result.status == "success"
    assert result.response_content == "正文结论[[REF:R1]]"
    assert [reference.key for reference in result.references] == ["R1"]
    assert scholar.queries == ["first", "second", "third"]
    assert set(_tool_results(writer.chat_history)) == {"one", "two", "three"}
    assert calls[-1]["tools"] is None
    assert calls[-1]["tool_choice"] is None
    assert any(event == "writer.fallback" for event, _ in events)


def test_writer_search_failure_is_warning_when_body_is_generated(monkeypatch):
    """检索失败只记限制，完整无引用正文仍是 success。"""
    _disable_side_effects(monkeypatch)
    scholar = _Scholar(fail=True)
    writer = _writer(scholar, max_tool_rounds=1, max_search_calls=1)
    responses = iter(
        [
            StandardResponse(
                tool_calls=[ToolCall("offline", "search_papers", '{"query":"topic"}')]
            ),
            StandardResponse(
                content="无引用正文[[REF:R9]] {[^9]: Fabricated}\n[^8]: Fake"
            ),
        ]
    )

    async def fake_chat(**kwargs):
        return next(responses)

    monkeypatch.setattr(writer, "_chat", fake_chat)
    result = asyncio.run(writer.run("write", sub_title="ques1"))

    assert "搜索文献失败" not in result.response_content
    assert "REF:R9" not in result.response_content
    assert "Fabricated" not in result.response_content
    assert "Fake" not in result.response_content
    assert result.references == []
    assert result.status == "success"
    assert any("search_failed" in item for item in result.limitations)


def test_writer_empty_forced_response_returns_degraded_placeholder(monkeypatch):
    """最终禁用工具调用仍为空时必须返回可识别的降级占位。"""
    _disable_side_effects(monkeypatch)
    writer = _writer(_Scholar(), max_tool_rounds=0, max_search_calls=0)

    async def fake_chat(**kwargs):
        return StandardResponse(content="")

    monkeypatch.setattr(writer, "_chat", fake_chat)
    result = asyncio.run(writer.run("write", sub_title="ques2"))

    assert result.status == "degraded"
    assert result.response_content.strip()
    assert "本节未能生成完整正文" in result.response_content


def test_writer_resets_history_and_images_between_sections(monkeypatch):
    """不同章节不能共享私有正文、图片或引用。"""
    _disable_side_effects(monkeypatch)
    writer = _writer(_Scholar(), max_tool_rounds=1, max_search_calls=1)
    responses = iter(
        [
            StandardResponse(content="first body"),
            StandardResponse(content="second body"),
        ]
    )
    histories: list[list[dict]] = []

    async def fake_chat(**kwargs):
        histories.append([dict(item) for item in kwargs["history"]])
        return next(responses)

    monkeypatch.setattr(writer, "_chat", fake_chat)
    asyncio.run(writer.run("first prompt", ["q1.png"], "ques1"))
    result = asyncio.run(writer.run("second prompt", None, "ques2"))

    assert result.response_content == "second body"
    assert result.status == "success"
    second_history = "\n".join(str(item.get("content", "")) for item in histories[1])
    assert "first prompt" not in second_history
    assert "q1.png" not in second_history
    assert writer.available_images == []


def test_anthropic_groups_adjacent_tool_results():
    """Anthropic 要求同一 assistant 回合的工具结果位于一个 user block。"""
    history = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "one",
                    "type": "function",
                    "function": {"name": "search_papers", "arguments": "{}"},
                },
                {
                    "id": "two",
                    "type": "function",
                    "function": {"name": "search_papers", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "one", "content": "first"},
        {"role": "tool", "tool_call_id": "two", "content": "second"},
    ]

    _, messages = AnthropicProvider()._convert_messages(history)

    assert messages[1] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "one", "content": "first"},
            {"type": "tool_result", "tool_use_id": "two", "content": "second"},
        ],
    }
