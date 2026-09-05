"""Coordinator TaskOutline 生成、持久化与兼容调度集成测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.core.agents.coordinator_agent import (
    CoordinatorAgent,
    CoordinatorResponseError,
    parse_task_outline,
)
from app.core.flows import Flows
from app.core.llm.types import StandardResponse
from app.data.artifact_store import M15ArtifactStore
from app.domain.m15 import FileFact, TaskFacts, TaskOutline
from app.orchestration.task_outline import TaskOutlineWorkflow, build_task_schedule
from app.schemas.A2A import ModelerToCoder

SOURCE_SHA = "a" * 64
TEMPLATE_SHA = "b" * 64


class FakeCoordinatorLLM:
    """记录 Coordinator 请求并按顺序返回固定文本。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = iter(responses)
        self.histories: list[list[dict]] = []

    async def chat(self, **kwargs) -> StandardResponse:
        """返回下一条文本响应。"""
        self.histories.append([dict(message) for message in kwargs["history"]])
        return StandardResponse(content=next(self._responses))


def _task_facts(
    *,
    problem_text: str = "问题1：预测产量。问题2：基于问题1结果优化方案。",
    expected_question_ids: tuple[str, ...] | None = ("ques1", "ques2"),
) -> TaskFacts:
    """构造包含输入附件和输出模板的只读事实。"""
    return TaskFacts(
        schema_version="m1.5",
        artifact_id="task-facts:root",
        source_artifact_ids=(),
        validation_status="validated",
        artifact_path="m15/task_facts.json",
        task_id="outline-test",
        problem_text=problem_text,
        expected_question_ids=expected_question_ids,
        expected_question_count=(
            len(expected_question_ids) if expected_question_ids is not None else None
        ),
        attachments=(FileFact(path="input.csv", sha256=SOURCE_SHA),),
        output_templates=(FileFact(path="result.csv", sha256=TEMPLATE_SHA),),
    )


def _outline_payload(
    *,
    second_dependency: tuple[str, ...] = ("ques1",),
) -> dict:
    """构造与 TaskFacts 一致的 Coordinator 响应。"""
    file_deliverable = {
        "deliverable_id": "deliverable:result",
        "description": "填写结果模板",
        "path": "result.csv",
    }
    report_deliverable = {
        "deliverable_id": "deliverable:report",
        "description": "给出优化结论",
        "path": None,
    }
    return {
        "schema_version": "m1.5",
        "artifact_id": "task-outline:root",
        "source_artifact_ids": ["task-facts:root"],
        "validation_status": "validated",
        "artifact_path": "m15/task_outline.json",
        "task_facts_id": "task-facts:root",
        "questions": [
            {
                "question_id": "ques1",
                "text": "问题1：预测产量。",
                "depends_on": [],
                "order_key": 1,
                "deliverables": [file_deliverable],
            },
            {
                "question_id": "ques2",
                "text": "问题2：基于问题1结果优化方案。",
                "depends_on": list(second_dependency),
                "order_key": 2,
                "deliverables": [report_deliverable],
            },
        ],
        "deliverables": [file_deliverable, report_deliverable],
    }


def test_coordinator_repairs_invalid_dependency_with_bounded_context():
    """非法依赖进入有限修复，且无效原文不会回灌下一次上下文。"""
    invalid_payload = _outline_payload(second_dependency=("ques9",))
    invalid_text = json.dumps(invalid_payload, ensure_ascii=False)
    valid_text = json.dumps(_outline_payload(), ensure_ascii=False)
    llm = FakeCoordinatorLLM([invalid_text, valid_text])
    agent = CoordinatorAgent(
        "outline-test",
        llm,  # type: ignore[arg-type]
        max_repair_attempts=1,
    )

    outline = asyncio.run(agent.run(_task_facts()))

    assert outline.ques_count == 2
    assert len(llm.histories) == 2
    assert "invalid_schema" in llm.histories[1][-1]["content"]
    assert invalid_text not in str(llm.histories[1])


def test_coordinator_repairs_outline_that_omits_tail_question():
    """模型遗漏尾部问题时必须依据 TaskFacts 拒绝并进入有限修复。"""
    incomplete_payload = _outline_payload()
    incomplete_payload["questions"] = incomplete_payload["questions"][:1]
    incomplete_text = json.dumps(incomplete_payload, ensure_ascii=False)
    llm = FakeCoordinatorLLM(
        [incomplete_text, json.dumps(_outline_payload(), ensure_ascii=False)]
    )
    agent = CoordinatorAgent(
        "outline-test",
        llm,  # type: ignore[arg-type]
        max_repair_attempts=1,
    )

    outline = asyncio.run(agent.run(_task_facts()))

    assert outline.ques_count == 2
    assert len(llm.histories) == 2
    assert "facts_mismatch" in llm.histories[1][-1]["content"]
    assert "ques2" in llm.histories[1][-1]["content"]


def test_coordinator_exhausts_repair_when_tail_question_stays_missing():
    """尾部问题持续遗漏时必须按预算耗尽，不能接受模型自报的一问。"""
    incomplete_payload = _outline_payload()
    incomplete_payload["questions"] = incomplete_payload["questions"][:1]
    incomplete_text = json.dumps(incomplete_payload, ensure_ascii=False)
    llm = FakeCoordinatorLLM([incomplete_text, incomplete_text])
    agent = CoordinatorAgent(
        "outline-test",
        llm,  # type: ignore[arg-type]
        max_repair_attempts=1,
    )

    with pytest.raises(CoordinatorResponseError) as exc_info:
        asyncio.run(agent.run(_task_facts()))

    assert exc_info.value.kind == "facts_mismatch"
    assert exc_info.value.attempts == 2
    assert "ques2" in exc_info.value.detail
    assert len(llm.histories) == 2


def test_coordinator_stops_after_repair_budget_is_exhausted():
    """有限修复耗尽后必须失败，不能继续无界请求。"""
    invalid_text = json.dumps(
        _outline_payload(second_dependency=("ques9",)),
        ensure_ascii=False,
    )
    llm = FakeCoordinatorLLM([invalid_text, invalid_text])
    agent = CoordinatorAgent(
        "outline-test",
        llm,  # type: ignore[arg-type]
        max_repair_attempts=1,
    )

    with pytest.raises(CoordinatorResponseError) as exc_info:
        asyncio.run(agent.run(_task_facts()))

    assert exc_info.value.attempts == 2
    assert len(llm.histories) == 2


def test_coordinator_rejects_unavailable_question_boundary_before_model_call():
    """无可靠编号题头时 fail closed，不让模型自行声明问题数量。"""
    llm = FakeCoordinatorLLM([])
    agent = CoordinatorAgent(
        "outline-test",
        llm,  # type: ignore[arg-type]
        max_repair_attempts=1,
    )
    task_facts = _task_facts(
        problem_text="请根据附件建立数学模型并给出完整方案。",
        expected_question_ids=None,
    )

    with pytest.raises(
        CoordinatorResponseError,
        match="question_boundary_unavailable",
    ) as exc_info:
        asyncio.run(agent.run(task_facts))

    assert exc_info.value.attempts == 0
    assert llm.histories == []


@pytest.mark.parametrize("invalid_kind", ["rewritten_question", "unknown_template"])
def test_coordinator_parser_rejects_content_not_traceable_to_facts(
    invalid_kind: str,
):
    """问题原文和文件交付物都必须可追溯到 TaskFacts。"""
    payload = _outline_payload()
    if invalid_kind == "rewritten_question":
        payload["questions"][0]["text"] = "预测未来产量。"
    else:
        payload["deliverables"][0]["path"] = "invented.csv"
        payload["questions"][0]["deliverables"][0]["path"] = "invented.csv"

    with pytest.raises(CoordinatorResponseError, match="facts_mismatch"):
        parse_task_outline(
            json.dumps(payload, ensure_ascii=False),
            _task_facts(),
        )


def test_coordinator_parser_rejects_duplicate_template_deliverable_path():
    """同一输出模板不能用不同交付物标识重复声明。"""
    payload = _outline_payload()
    payload["deliverables"].append(
        {
            "deliverable_id": "deliverable:result-copy",
            "description": "重复填写结果模板",
            "path": "result.csv",
        }
    )

    with pytest.raises(CoordinatorResponseError, match="文件交付物路径不能重复"):
        parse_task_outline(
            json.dumps(payload, ensure_ascii=False),
            _task_facts(),
        )


def test_outline_workflow_builds_read_only_facts_and_persists_artifacts(
    tmp_path: Path,
):
    """编排层从现有附件发现结果冻结事实并落盘两个领域产物。"""
    input_path = tmp_path / "input.csv"
    template_path = tmp_path / "result.csv"
    input_path.write_text("year,yield\n2024,10\n", encoding="utf-8")
    template_path.write_text("year,prediction\n", encoding="utf-8")
    input_before = input_path.read_bytes()
    template_before = template_path.read_bytes()
    llm = FakeCoordinatorLLM([json.dumps(_outline_payload(), ensure_ascii=False)])
    agent = CoordinatorAgent(
        "outline-test",
        llm,  # type: ignore[arg-type]
        max_repair_attempts=0,
    )

    result = asyncio.run(
        TaskOutlineWorkflow(agent).create_outline(
            task_id="outline-test",
            problem_text=_task_facts().problem_text,
            work_dir=tmp_path,
        )
    )

    assert [fact.path for fact in result.task_facts.attachments] == ["input.csv"]
    assert [fact.path for fact in result.task_facts.output_templates] == ["result.csv"]
    assert result.task_facts.attachments[0].sha256 != SOURCE_SHA
    assert result.task_facts.expected_question_ids == ("ques1", "ques2")
    assert result.task_facts.expected_question_count == 2
    assert result.outline.ques_count == result.schedule.ques_count == 2
    assert input_path.read_bytes() == input_before
    assert template_path.read_bytes() == template_before
    store = M15ArtifactStore(tmp_path)
    assert store.read_json("m15/task_facts.json", TaskFacts) == result.task_facts
    assert store.read_json("m15/task_outline.json", TaskOutline) == result.outline


def test_legacy_flows_use_topological_order_but_remain_serial():
    """依赖调度只改变稳定迭代顺序，不创建并发执行分支。"""
    payload = _outline_payload(second_dependency=())
    payload["questions"][0]["depends_on"] = ["ques2"]
    outline = TaskOutline.model_validate_json(json.dumps(payload, ensure_ascii=False))
    schedule = build_task_schedule(outline)
    questions = {
        "background": "测试背景",
        "ques_count": 99,
        "ques1": "问题1：预测产量。",
        "ques2": "问题2：基于问题1结果优化方案。",
    }

    flows = Flows(questions, schedule=schedule).get_solution_flows(
        questions,
        ModelerToCoder(
            questions_solution={
                "ques1": "方案一",
                "ques2": "方案二",
                "sensitivity_analysis": "敏感性方案",
            }
        ),
    )

    assert schedule.execution_order == ("ques2", "ques1")
    assert list(flows) == ["eda", "ques2", "ques1", "sensitivity_analysis"]
