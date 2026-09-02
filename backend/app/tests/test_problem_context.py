"""公共题面与执行约束分流测试。"""

import asyncio
from types import SimpleNamespace
from typing import cast

from app.core.agents.coordinator_agent import CoordinatorAgent
from app.core.flows import Flows
from app.core.llm.llm import LLM
from app.core.llm.types import StandardResponse
from app.schemas.A2A import CoordinatorToModeler, ModelerToCoder
from app.services.trace_recorder import trace_recorder
from app.tools.base_interpreter import BaseCodeInterpreter
from app.utils.problem_context import (
    extract_embedded_constraints,
    find_constraint_echoes,
    normalize_constraints,
    normalize_coordinator_payload,
    redact_execution_constraints,
    render_public_problem_context,
)
from app.models.user_output import UserOutput


def test_old_coordinator_json_without_constraints_remains_compatible():
    payload = {
        "title": "题目",
        "background": "研究背景",
        "ques_count": 1,
        "ques1": "求最优方案",
    }

    normalized = normalize_coordinator_payload(payload)
    result = CoordinatorToModeler.model_validate(normalized)

    assert result.constraints == []
    assert result.questions["ques1"] == "求最优方案"


def test_malformed_constraints_are_ignored_without_stringification():
    malformed = object()

    assert normalize_constraints(
        [
            "正常约束",
            123,
            True,
            malformed,
            {"text": {"unexpected": "value"}},
            {"items": ["嵌套约束", 0]},
        ]
    ) == ["正常约束", "嵌套约束"]


def test_embedded_process_instruction_moves_to_constraints():
    payload = {
        "title": "题目",
        "background": "研究背景。不要使用方法 A。",
        "ques_count": 1,
        "ques1": "求最优方案",
    }

    normalized = normalize_coordinator_payload(payload)

    assert normalized["constraints"] == ["不要使用方法 A"]
    questions = normalized["questions"]
    assert isinstance(questions, dict)
    assert "不要使用方法 A" not in questions["background"]


def test_embedded_constraint_without_separator_moves_tail_to_constraints():
    assert extract_embedded_constraints("研究对象具有周期性变化不要使用方法 A") == (
        "研究对象具有周期性变化",
        ["不要使用方法 A"],
    )


def test_embedded_constraint_without_separator_is_normalized():
    payload = {
        "title": "题目",
        "background": "研究对象具有周期性变化不要使用方法 A",
        "ques_count": 1,
        "ques1": "求最优方案",
    }

    normalized = normalize_coordinator_payload(payload)

    questions = normalized["questions"]
    assert isinstance(questions, dict)
    assert questions["background"] == "研究对象具有周期性变化"
    assert normalized["constraints"] == ["不要使用方法 A"]


def test_embedded_constraint_with_comma_preserves_public_suffix():
    public, constraints = extract_embedded_constraints(
        "研究对象不要使用方法 A，具有周期性变化"
    )

    assert public == "研究对象，具有周期性变化"
    assert constraints == ["不要使用方法 A"]


def test_embedded_constraint_with_semicolon_preserves_public_suffix():
    public, constraints = extract_embedded_constraints(
        "研究对象不要使用方法 A；具有周期性变化"
    )

    assert public == "研究对象；具有周期性变化"
    assert constraints == ["不要使用方法 A"]


def test_embedded_markers_cover_adopt_and_save_result_boundaries():
    public, constraints = extract_embedded_constraints(
        "研究对象。禁止采用方法 A。请将结果保存为 result.csv。结果准确率为0.91。"
    )

    assert public == "研究对象。结果准确率为0.91。"
    assert constraints == ["禁止采用方法 A", "请将结果保存为 result.csv"]


def test_redact_unknown_process_markers_keeps_other_verifiable_sentences():
    text = (
        "accuracy=0.91。禁止采用方法 A。请将结果保存为 result.csv。"
        "验证结果稳定。"
    )

    cleaned = redact_execution_constraints(text)

    assert "accuracy=0.91" in cleaned
    assert "验证结果稳定" in cleaned
    assert "禁止采用方法 A" not in cleaned
    assert "请将结果保存为 result.csv" not in cleaned


def test_redact_drops_ambiguous_raw_problem_and_process_sentence():
    text = (
        "原始题面：研究三种作物并最小化成本，请将结果保存为 result.csv，"
        "结果准确率为0.91。独立验证结果稳定。"
    )

    cleaned = redact_execution_constraints(text)

    assert "结果准确率为0.91" not in cleaned
    assert "独立验证结果稳定" in cleaned


def test_redact_coder_process_echo_preserves_separate_results():
    text = (
        "原始题面：研究三种作物并最小化成本。"
        "按题目要求改用方法 B。结果准确率为0.91。"
    )

    cleaned = redact_execution_constraints(text)

    assert "原始题面" not in cleaned
    assert "按题目要求改用方法 B" not in cleaned
    assert "结果准确率为0.91" in cleaned


def test_multiple_markers_use_safe_boundaries_and_preserve_public_fragments():
    text = "研究对象不要使用方法 A，具有周期性变化；必须使用方法 B，目标为最小化"

    public, constraints = extract_embedded_constraints(text)

    assert public == "研究对象，具有周期性变化；目标为最小化"
    assert constraints == ["不要使用方法 A", "必须使用方法 B"]
    assert all(constraint not in public for constraint in constraints)


def test_multiple_markers_without_separator_drop_ambiguous_execution_tail():
    text = "不要使用方法 A，研究对象不要使用方法 B"

    assert extract_embedded_constraints(text) == (
        "研究对象",
        ["不要使用方法 A", "不要使用方法 B"],
    )


def test_multiple_markers_without_boundary_keeps_whole_clause():
    text = "不要使用方法 A 研究对象不要使用方法 B"

    public, constraints = extract_embedded_constraints(text)

    assert public == ""
    assert constraints == ["不要使用方法 A 研究对象", "不要使用方法 B"]


def test_public_context_drops_all_multi_marker_constraints():
    flows = Flows(
        {
            "background": (
                "研究对象不要使用方法 A，具有周期性变化；"
                "必须使用方法 B，目标为最小化"
            ),
            "ques_count": 1,
            "ques1": "求解最优方案",
        }
    )

    public_context = flows.get_public_problem_context()

    assert "研究对象" in public_context
    assert "具有周期性变化" in public_context
    assert "目标为最小化" in public_context
    assert "不要使用方法 A" not in public_context
    assert "必须使用方法 B" not in public_context


def test_embedded_constraint_without_public_prefix_keeps_whole_clause():
    assert extract_embedded_constraints("不要使用方法 A") == (
        "",
        ["不要使用方法 A"],
    )


def test_public_context_and_writer_material_do_not_echo_constraint():
    questions = {
        "title": "题目",
        "background": "研究背景。不要使用方法 A。",
        "ques_count": 1,
        "ques1": "求最优方案",
    }
    flows = Flows(questions)
    modeler = ModelerToCoder(
        questions_solution={"ques1": "采用方法 B"},
        constraints=flows.constraints,
    )
    solution_flow = flows.get_solution_flows(questions, modeler)

    assert "不要使用方法 A" in solution_flow["ques1"]["coder_prompt"]
    public_context = render_public_problem_context(flows.questions)
    assert "不要使用方法 A" not in public_context

    user_output = UserOutput(work_dir="/tmp", ques_count=1)
    write_template = {
        key: "模板"
        for key in (
            "firstPage",
            "RepeatQues",
            "analysisQues",
            "modelAssumption",
            "symbol",
            "judge",
            "eda",
            "sensitivity_analysis",
        )
    }
    writer_flows = flows.get_write_flows(
        user_output,
        write_template,
    )
    assert "不要使用方法 A" not in writer_flows["firstPage"]

    writer_template = {"ques1": "模板", "eda": "模板", "sensitivity_analysis": "模板"}
    writer_prompt = flows.get_writer_prompt(
        "ques1",
        "已完成。不要使用方法 A。最终采用方法 B。",
        cast(BaseCodeInterpreter, SimpleNamespace(section_output={})),
        writer_template,
    )
    assert "不要使用方法 A" not in writer_prompt
    assert "方法 B" in writer_prompt


def test_redact_execution_constraints_removes_echo_without_known_list():
    text = "已采用方法 B。不要使用方法 A。结果满足验证要求。"

    cleaned = redact_execution_constraints(text)

    assert "不要使用方法 A" not in cleaned
    assert "方法 B" in cleaned
    assert "验证要求" in cleaned


def test_redact_filters_chinese_and_english_rewritten_constraints():
    constraints = ["不要使用方法 A", "Do not use method X"]
    text = (
        "模型结果达到 0.93。为满足该限制，改用方法 B。"
        "To comply with the restriction, we use method Y instead. "
        "验证结果稳定。"
    )

    cleaned = redact_execution_constraints(text, constraints)

    assert "0.93" in cleaned
    assert "验证结果稳定" in cleaned
    assert "改用方法 B" not in cleaned
    assert "method Y" not in cleaned
    assert find_constraint_echoes(text, constraints)


def test_writer_uses_structured_public_context_when_legacy_background_is_raw():
    flows = Flows(
        {
            "title": "题目",
            "background": "公开背景",
            "ques_count": 1,
            "ques1": "求最优方案",
            "constraints": ["不要使用方法 A"],
        }
    )
    config = {
        key: "模板"
        for key in (
            "firstPage",
            "RepeatQues",
            "analysisQues",
            "modelAssumption",
            "symbol",
            "judge",
        )
    }
    result = flows.get_write_flows(
        UserOutput("/tmp", 1),
        config,
        phase_results={
            "ques1": {
                "phase": "ques1",
                "status": "success",
                "evidence_status": "available",
                "model_reference": "modeler_solution:ques1",
                "metrics": ["accuracy=0.93"],
                "limitations": [],
                "artifacts": [],
                "stdout": "结果：accuracy=0.93",
            }
        },
    )

    assert "公开背景" in result["firstPage"]
    assert "原始题面包含" not in result["firstPage"]
    assert "不要使用方法 A" not in result["firstPage"]
    assert "accuracy=0.93" in result["firstPage"]


def test_coordinator_retries_invalid_json_and_keeps_constraints_separate(monkeypatch):
    async def noop_trace(*args, **kwargs):
        return None

    monkeypatch.setattr(trace_recorder, "emit", noop_trace)

    class FakeCoordinator(CoordinatorAgent):
        def __init__(self):
            super().__init__(task_id="task", model=cast(LLM, object()))
            self.responses = [
                '{"background": "背景", "ques_count": "bad"}',
                (
                    '{"background": "背景", "ques_count": 1, '
                    '"ques1": "目标。不要使用方法 A。"}'
                ),
            ]

        async def _chat(self, **kwargs):
            return StandardResponse(content=self.responses.pop(0))

    async def run_agent():
        return await FakeCoordinator().run("原始题面")

    result = asyncio.run(run_agent())

    assert result.ques_count == 1
    assert result.constraints == ["不要使用方法 A"]
    assert "不要使用方法 A" not in result.questions["ques1"]
