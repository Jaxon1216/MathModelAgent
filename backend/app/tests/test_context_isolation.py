"""阶段和章节 history 隔离的定向测试。"""

import asyncio
from types import SimpleNamespace
from typing import cast

from app.core.agents.agent import Agent
from app.core.agents.coder_agent import CoderAgent
from app.core.agents.coordinator_agent import CoordinatorAgent
from app.core.agents.modeler_agent import ModelerAgent
from app.core.agents.writer_agent import WriterAgent
from app.core.llm.llm import LLM
from app.core.llm.types import StandardResponse, ToolCall, Usage
from app.schemas.A2A import CoordinatorToModeler
from app.schemas.enums import CompTemplate, FormatOutPut
from app.services.redis_manager import redis_manager
from app.services.trace_recorder import trace_recorder
from app.tools.base_interpreter import BaseCodeInterpreter


def _disable_external_side_effects(monkeypatch):
    """让 history 单测只验证内存状态，不依赖 Redis 或 trace 文件推送。"""
    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(redis_manager, "publish_message", noop)
    monkeypatch.setattr(trace_recorder, "emit", noop)


def test_fact_summary_drops_user_prompt_and_process_constraints():
    agent = Agent("task", cast(LLM, object()))
    summary = agent._format_history_for_summary(
        [
            {"role": "user", "content": "原始任务：不要使用方法 A"},
            {
                "role": "assistant",
                "content": (
                    "cleaned/data.csv rows=12\n"
                    "accuracy=0.93\n"
                    "不要使用方法 A"
                ),
            },
            {"role": "tool", "content": "结论：结果稳定，限制：样本有限"},
        ]
    )

    assert "原始任务" not in summary
    assert "不要使用方法 A" not in summary
    assert "cleaned/data.csv" in summary
    assert "accuracy=0.93" in summary
    assert "样本有限" in summary


def test_compression_filters_mock_summary_before_history_persistence(
    monkeypatch,
):
    _disable_external_side_effects(monkeypatch)
    agent = Agent(
        "task",
        cast(LLM, object()),
        context_window=10,
        token_threshold_ratio=0.5,
    )
    agent.chat_history = [
        {"role": "system", "content": "system prompt with original task"},
        {"role": "assistant", "content": "cleaned/data.csv rows=12"},
        {"role": "assistant", "content": "accuracy=0.93"},
        {"role": "assistant", "content": "结论：结果稳定"},
        {"role": "assistant", "content": "保留最后一条"},
        {"role": "assistant", "content": "当前阶段仍在执行"},
    ]
    agent.current_token_count = 100

    async def fake_simple_chat(*args, **kwargs):
        return (
            "原始任务：不要使用方法 A\n"
            "inspection-entry: source.csv rows=12 source_inspection.json\n"
            "accuracy=0.93\n"
            "必须使用方法 B\n"
            "过程说明：已完成重试"
        )

    monkeypatch.setattr("app.core.agents.agent.simple_chat", fake_simple_chat)
    asyncio.run(agent.compress_if_needed())

    summary_messages = [
        str(message.get("content") or "")
        for message in agent.chat_history
        if message.get("role") == "assistant"
        and "[历史对话总结]" in str(message.get("content") or "")
    ]
    assert summary_messages
    summary = "\n".join(summary_messages)
    assert "accuracy=0.93" in summary
    assert "原始任务" not in summary
    assert "不要使用方法 A" not in summary
    assert "必须使用方法 B" not in summary
    assert "过程说明" not in summary
    assert "inspection-entry" not in summary
    assert "source_inspection.json" not in summary


def test_record_response_uses_actual_prompt_tokens(monkeypatch):
    _disable_external_side_effects(monkeypatch)
    agent = Agent("task", cast(LLM, object()))
    asyncio.run(
        agent.record_response(
            StandardResponse(
                content="accuracy=0.9",
                usage=Usage(prompt_tokens=120, completion_tokens=8),
            )
        )
    )

    assert agent.last_prompt_tokens == 120
    assert agent.last_completion_tokens == 8
    assert agent.current_token_count >= 120
    assert len(agent.chat_history) == 1


def test_context_budget_counts_tool_call_arguments(monkeypatch):
    _disable_external_side_effects(monkeypatch)
    agent = Agent("task", cast(LLM, object()))
    code = "print('x')\n" * 100
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "execute_code", "arguments": code},
            }
        ],
    }

    assert agent._estimate_message_tokens(message) > len(code) // 3
    assert agent._history_chars() == 0
    agent.chat_history.append(message)
    assert agent._history_chars() >= len(code)


def test_coordinator_records_successful_response_in_history(monkeypatch):
    _disable_external_side_effects(monkeypatch)
    coordinator = CoordinatorAgent(
        "task",
        cast(LLM, SimpleNamespace()),
    )

    async def fake_chat(**kwargs):
        return StandardResponse(
            content='{"background":"背景","ques_count":1,"ques1":"求解"}'
        )

    monkeypatch.setattr(coordinator, "_chat", fake_chat)
    asyncio.run(coordinator.run("题面"))

    assert any(
        message.get("role") == "assistant"
        and "ques1" in str(message.get("content"))
        for message in coordinator.chat_history
    )


def test_modeler_records_successful_response_in_history(monkeypatch):
    _disable_external_side_effects(monkeypatch)
    modeler = ModelerAgent("task", cast(LLM, SimpleNamespace()))
    coordinator = CoordinatorToModeler(
        questions={"background": "背景", "ques_count": 1, "ques1": "求解"},
        ques_count=1,
    )

    async def fake_chat(**kwargs):
        return StandardResponse(content='{"ques1":"方案"}')

    monkeypatch.setattr(modeler, "_chat", fake_chat)
    asyncio.run(modeler.run(coordinator))

    assert any(
        message.get("role") == "assistant"
        and "方案" in str(message.get("content"))
        for message in modeler.chat_history
    )


def test_writer_run_starts_each_section_with_fresh_history(monkeypatch):
    _disable_external_side_effects(monkeypatch)
    model = cast(LLM, SimpleNamespace(api_type=None))
    writer = WriterAgent(
        "task",
        model,
        comp_template=CompTemplate.CHINA,
        format_output=FormatOutPut.Markdown,
    )
    responses = iter(
        [
            StandardResponse(content="第一章结果"),
            StandardResponse(content="第二章结果"),
        ]
    )

    async def fake_chat(**kwargs):
        return next(responses)

    monkeypatch.setattr(writer, "_chat", fake_chat)

    async def run_sections():
        await writer.run("第一章私有材料", sub_title="ques1")
        await writer.run("第二章私有材料", sub_title="ques2")

    asyncio.run(run_sections())

    contents = [
        str(message.get("content") or "") for message in writer.chat_history
    ]
    assert writer.history_scope == "section:ques2"
    assert "第一章私有材料" not in "\n".join(contents)
    assert "第二章私有材料" in "\n".join(contents)


def test_coder_phase_reset_keeps_interpreter_but_drops_history():
    interpreter = cast(BaseCodeInterpreter, object())
    coder = CoderAgent(
        "task",
        cast(LLM, SimpleNamespace(api_type=None)),
        work_dir="/tmp",
        code_interpreter=interpreter,
    )
    coder._begin_phase("ques1")
    coder.chat_history.append({"role": "tool", "content": "ques1 code"})
    coder._begin_phase("ques2")

    assert coder.code_interpreter is interpreter
    assert coder.history_scope == "ques2"
    assert coder.chat_history == []
    assert coder.current_chat_turns == 0


def test_coder_phase_preload_uses_skill_metadata_and_load_skill_can_read_body(
    monkeypatch,
):
    _disable_external_side_effects(monkeypatch)
    coder = CoderAgent(
        "task",
        cast(LLM, SimpleNamespace(api_type=None)),
        work_dir="/tmp",
        code_interpreter=cast(BaseCodeInterpreter, object()),
    )
    coder._begin_phase("ques1")
    asyncio.run(coder._ensure_phase_skills("ques1"))

    history_text = "\n".join(
        str(message.get("content") or "") for message in coder.chat_history
    )
    for skill_name in ("mathematical-modeling", "visualization", "figure-reporting"):
        skill = coder._skill_loader.get_skill(skill_name)
        assert skill is not None
        assert skill.body not in history_text
        assert f'load_skill("{skill_name}")' in history_text
        loaded_skill = coder._skill_loader.get_skill(skill_name)
        assert loaded_skill is not None
        assert loaded_skill.body == skill.body


def test_sensitivity_phase_preload_uses_metadata_and_load_skill_prompt(
    monkeypatch,
):
    _disable_external_side_effects(monkeypatch)
    coder = CoderAgent(
        "task",
        cast(LLM, SimpleNamespace(api_type=None)),
        work_dir="/tmp",
        code_interpreter=cast(BaseCodeInterpreter, object()),
    )
    coder._begin_phase("sensitivity_analysis")
    asyncio.run(coder._ensure_phase_skills("sensitivity_analysis"))

    history_text = "\n".join(
        str(message.get("content") or "") for message in coder.chat_history
    )
    for skill_name in (
        "sensitivity-analysis",
        "visualization",
        "figure-reporting",
    ):
        skill = coder._skill_loader.get_skill(skill_name)
        assert skill is not None
        assert f'<skill-metadata name="{skill_name}">' in history_text
        assert f'load_skill("{skill_name}")' in history_text
        assert skill.body not in history_text
        assert coder._skill_loader.get_description(skill_name) == skill.description


def test_coder_eda_history_keeps_inspection_material_but_check_omits_phase_prompt(
    monkeypatch,
):
    """EDA history 保留探查交接，completion check 不回贴原始阶段提示。"""
    _disable_external_side_effects(monkeypatch)

    class HistoryInterpreter:
        def add_section(self, section: str) -> None:
            pass

        async def execute_code(self, code: str):
            return (
                f"inspection-entry: {inspection_body} source_inspection.json\n"
                "cleaned/source__Sheet1.csv rows=10 accuracy=0.91",
                False,
                "",
            )

        async def get_created_images(self, section: str) -> list[str]:
            return []

    coder = CoderAgent(
        "task",
        cast(LLM, SimpleNamespace(api_type=None)),
        work_dir="/tmp",
        code_interpreter=cast(BaseCodeInterpreter, HistoryInterpreter()),
    )
    coder.set_data_brief("源数据探查报告已落盘，首轮提示将提供有限材料。")
    inspection_body = (
        "sheet source.csv::Sheet1 rows=10 cols=2 "
        "cleaned_path=cleaned/source__Sheet1.csv"
    )
    phase_prompt = (
        f"原始 phase prompt：source_inspection.json {inspection_body}；请完成数据清洗。"
    )
    responses = iter(
        [
            StandardResponse(
                content="开始执行清洗",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="execute_code",
                        arguments='{"code":"print(1)"}',
                    )
                ],
            ),
            StandardResponse(content="清洗完成，写出 cleaned/source__Sheet1.csv"),
            StandardResponse(content="清洗完成，写出 cleaned/source__Sheet1.csv"),
        ]
    )

    async def fake_chat(**kwargs):
        return next(responses)

    async def skip_skills(subtask_title: str) -> None:
        pass

    monkeypatch.setattr(coder, "_chat", fake_chat)
    monkeypatch.setattr(coder, "_ensure_phase_skills", skip_skills)
    coder.set_inspection_text(inspection_body)

    asyncio.run(coder.run(phase_prompt, subtask_title="eda"))

    history_contents = [
        str(message.get("content") or "") for message in coder.chat_history
    ]
    history_text = "\n".join(history_contents)
    assert history_text.count(inspection_body) == 1
    assert history_text.count("source_inspection.json") == 1

    completion_checks = [
        content
        for content in history_contents
        if "Please review whether data preparation phase" in content
    ]
    assert len(completion_checks) == 1
    assert phase_prompt not in completion_checks[0]
    assert inspection_body not in completion_checks[0]
    assert "source_inspection.json" not in completion_checks[0]
    assert "cleaned/source__Sheet1.csv" in completion_checks[0]
