"""Task 7 主工作流接入、trace 与下游阻断集成测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.core.workflow as core_workflow
import app.runtime.tracing as tracing_module
from app.agents.cleaning_planner import CleaningPlannerAgent
from app.agents.modeler import ModelerAgent
from app.core.flows import Flows
from app.data import (
    DataContractIntegrityError,
    M15ArtifactStore,
    build_task_facts,
    inspect_data_catalog,
)
from app.domain.m15 import (
    Deliverable,
    OutlineQuestion,
    TaskFacts,
    TaskOutline,
)
from app.orchestration.m15_workflow import (
    M15StageError,
    M15Workflow,
    validate_contract_boundary,
)
from app.orchestration.task_outline import TaskOutlineWorkflow, build_task_schedule
from app.runtime.tracing import LegacyStageTracer
from app.schemas.A2A import ModelerToCoder
from app.schemas.request import Problem as ApiProblem
from app.services.trace_recorder import get_trace_phase


class FakeTracer:
    """捕获阶段事件并支持唯一终态断言。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def start(self, task_id: str, phase: str) -> None:
        """记录阶段开始。"""
        self.events.append(("start", {"task_id": task_id, "phase": phase}))

    async def event(self, task_id: str, event: str, *, phase: str, **payload) -> None:
        """记录阶段产物或完整性事件。"""
        self.events.append((event, {"task_id": task_id, "phase": phase, **payload}))

    async def end(
        self,
        task_id: str,
        phase: str,
        *,
        success: bool,
        duration_ms: int,
        failure_kind: str | None = None,
    ) -> None:
        """记录唯一阶段终态。"""
        self.events.append(
            (
                "end",
                {
                    "task_id": task_id,
                    "phase": phase,
                    "success": success,
                    "duration_ms": duration_ms,
                    "failure_kind": failure_kind,
                },
            )
        )


class FakeLLMClient:
    """为 CleaningPlanner 和 Modeler 顺序提供离线 JSON。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = iter(responses)
        self.calls = 0

    async def complete(self, messages: list[dict[str, str]]) -> str:
        """返回下一条固定响应。"""
        assert messages
        self.calls += 1
        return next(self._responses)


class StaticCoordinator:
    """基于本轮 TaskFacts 返回确定的依赖骨架。"""

    async def run(self, task_facts: TaskFacts) -> TaskOutline:
        """让 ques1 依赖 ques2，以验证不按编号或字典顺序执行。"""
        return _outline(task_facts)


def _deliverable(question_id: str) -> Deliverable:
    """构造可作为上游结果声明的交付物。"""
    return Deliverable(
        deliverable_id=f"deliverable:{question_id}",
        description=f"提交 {question_id} 的验证结果",
    )


def _outline(task_facts: TaskFacts) -> TaskOutline:
    """构造稳定排序与拓扑顺序不同的合法骨架。"""
    first = _deliverable("ques1")
    second = _deliverable("ques2")
    return TaskOutline(
        schema_version="m1.5",
        artifact_id="task-outline:root",
        source_artifact_ids=(task_facts.artifact_id,),
        validation_status="validated",
        artifact_path="m15/task_outline.json",
        task_facts_id=task_facts.artifact_id,
        questions=(
            OutlineQuestion(
                question_id="ques1",
                text="基于第二问结果制定方案。",
                depends_on=("ques2",),
                order_key=1,
                deliverables=(first,),
            ),
            OutlineQuestion(
                question_id="ques2",
                text="分析历史数据。",
                depends_on=(),
                order_key=2,
                deliverables=(second,),
            ),
        ),
        deliverables=(first, second),
    )


def _cleaning_payload() -> str:
    """构造覆盖唯一输入表的 no-op CleaningPlan 响应。"""
    return json.dumps(
        {
            "plans": [
                {
                    "table_id": "input.csv",
                    "operations": [],
                    "no_op_reason": "画像已满足建模所需约束。",
                }
            ]
        },
        ensure_ascii=False,
    )


def _question_plan_payload() -> str:
    """构造只引用契约字段和声明上游交付物的计划。"""

    def content(objective: str) -> dict[str, object]:
        return {
            "data": [{"table_id": "input.csv", "columns": ["id", "value"]}],
            "objective": objective,
            "model": "带基线对比的趋势模型",
            "method": "读取 cleaned 数据并计算稳定结果",
            "constraints": ["结果非负"],
            "validation": ["检查留出误差"],
            "figures": ["趋势诊断图"],
            "fallback": "使用历史均值基线",
        }

    return json.dumps(
        {
            "schema_version": "m1.5",
            "question_plans": [
                {
                    "question_id": "ques1",
                    **content("制定最终方案"),
                    "upstream_results": [
                        {
                            "question_id": "ques2",
                            "outputs": ["deliverable:ques2"],
                        }
                    ],
                    "deliverables": [_deliverable("ques1").model_dump(mode="json")],
                },
                {
                    "question_id": "ques2",
                    **content("分析历史数据"),
                    "upstream_results": [],
                    "deliverables": [_deliverable("ques2").model_dump(mode="json")],
                },
            ],
            "sensitivity_analysis": {
                **content("评估参数扰动"),
                "figures": ["参数敏感性图"],
            },
        },
        ensure_ascii=False,
    )


async def _prepare_success(
    tmp_path: Path,
    tracer: FakeTracer,
):
    """执行完整 TaskFacts 到 QuestionPlan 离线成功链路。"""
    problem_text = "基于第二问结果制定方案。分析历史数据。"
    outline_result = await TaskOutlineWorkflow(
        StaticCoordinator(),  # type: ignore[arg-type]
        tracer=tracer,
    ).create_outline(
        task_id="m15-workflow",
        problem_text=problem_text,
        work_dir=tmp_path,
    )
    client = FakeLLMClient([_cleaning_payload(), _question_plan_payload()])
    result = await M15Workflow(
        CleaningPlannerAgent(client, max_json_repair_attempts=0),
        ModelerAgent(client, max_repair_attempts=0),
        tracer=tracer,
    ).run(
        task_id="m15-workflow",
        work_dir=tmp_path,
        task_facts=outline_result.task_facts,
        outline=outline_result.outline,
    )
    return outline_result, result, client


@pytest.mark.asyncio
async def test_successful_m15_orchestration_traces_artifacts_and_uses_topology(
    tmp_path: Path,
):
    """成功链按固定阶段顺序结束，并以 outline 拓扑顺序驱动旧 flow。"""
    source = tmp_path / "input.csv"
    source.write_text("id,value\n1,10\n2,20\n", encoding="utf-8")
    source_before = source.read_bytes()
    tracer = FakeTracer()

    outline_result, result, client = await _prepare_success(tmp_path, tracer)

    assert client.calls == 2
    assert source.read_bytes() == source_before
    assert result.data_contract.status == "frozen"
    assert [plan.question_id for plan in result.question_plans.question_plans] == [
        "ques1",
        "ques2",
    ]
    assert "cleaned_path='cleaned/" in result.coder_handoff["ques1"]
    assert "deliverable:ques2" in result.coder_handoff["ques1"]

    starts = [payload["phase"] for event, payload in tracer.events if event == "start"]
    ends = [payload["phase"] for event, payload in tracer.events if event == "end"]
    expected_phases = [
        "task_facts",
        "task_outline",
        "data_profile",
        "data_cleaning",
        "data_contract",
        "question_plan",
    ]
    assert starts == expected_phases
    assert ends == expected_phases
    assert all(
        payload["success"] is True for event, payload in tracer.events if event == "end"
    )
    for phase in expected_phases:
        assert (
            sum(
                event == "end" and payload["phase"] == phase
                for event, payload in tracer.events
            )
            == 1
        )
        assert any(
            event.endswith(".artifact") and payload["phase"] == phase
            for event, payload in tracer.events
        )

    cleaning_step_events = [
        (event, payload)
        for event, payload in tracer.events
        if event.startswith("data_cleaning.")
        and event.rsplit(".", maxsplit=1)[-1] in {"start", "artifact", "issue", "end"}
    ]
    assert [event for event, _ in cleaning_step_events] == [
        "data_cleaning.clean.start",
        "data_cleaning.clean.artifact",
        "data_cleaning.clean.end",
        "data_cleaning.verify.start",
        "data_cleaning.verify.artifact",
        "data_cleaning.verify.end",
        "data_cleaning.artifact",
        "data_cleaning.artifact",
    ]
    assert all(
        payload["phase"] == "data_cleaning" for _, payload in cleaning_step_events
    )
    step_ids = {
        payload["step_id"]
        for event, payload in cleaning_step_events
        if event.endswith(".start")
    }
    assert len(step_ids) == 2
    for step_id in step_ids:
        assert (
            sum(
                event.endswith(".end") and payload.get("step_id") == step_id
                for event, payload in cleaning_step_events
            )
            == 1
        )
    assert (
        sum(
            event == "end" and payload["phase"] == "data_cleaning"
            for event, payload in tracer.events
        )
        == 1
    )

    questions = outline_result.to_legacy_questions()
    questions["ques_count"] = 99
    schedule = build_task_schedule(outline_result.outline)
    flows = Flows(questions, schedule=schedule).get_solution_flows(
        questions,
        ModelerToCoder(questions_solution=result.coder_handoff),
        result.data_contract,
    )
    assert schedule.execution_order == ("ques2", "ques1")
    assert list(flows) == ["eda", "ques2", "ques1", "sensitivity_analysis"]
    assert "严禁修改原始附件或 cleaned 文件" in flows["eda"]["coder_prompt"]
    assert "数据清洗,可视化" not in flows["eda"]["coder_prompt"]


@pytest.mark.asyncio
async def test_cleaning_step_events_preserve_legacy_phase_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """清洗子事件不嵌套旧 phase 生命周期，结束后正常清理上下文。"""
    (tmp_path / "input.csv").write_text(
        "id,value\n1,10\n2,20\n",
        encoding="utf-8",
    )
    records: list[tuple[str, str | None, str | None]] = []

    async def capture_emit(
        task_id: str,
        event: str,
        *,
        phase: str | None = None,
        **payload,
    ) -> None:
        del task_id, payload
        records.append((event, phase, get_trace_phase()))

    monkeypatch.setattr(tracing_module.trace_recorder, "emit", capture_emit)
    tracer = LegacyStageTracer()
    outline_result = await TaskOutlineWorkflow(
        StaticCoordinator(),  # type: ignore[arg-type]
        tracer=tracer,
    ).create_outline(
        task_id="legacy-trace",
        problem_text="基于第二问结果制定方案。分析历史数据。",
        work_dir=tmp_path,
    )
    client = FakeLLMClient([_cleaning_payload(), _question_plan_payload()])

    await M15Workflow(
        CleaningPlannerAgent(client, max_json_repair_attempts=0),
        ModelerAgent(client, max_repair_attempts=0),
        tracer=tracer,
    ).run(
        task_id="legacy-trace",
        work_dir=tmp_path,
        task_facts=outline_result.task_facts,
        outline=outline_result.outline,
    )

    substeps = [
        record
        for record in records
        if record[0].startswith("data_cleaning.")
        and record[0] != "data_cleaning.artifact"
    ]
    assert substeps
    assert all(
        phase == "data_cleaning" and context == "data_cleaning"
        for _, phase, context in substeps
    )
    assert (
        sum(
            event == "phase.end" and phase == "data_cleaning"
            for event, phase, _ in records
        )
        == 1
    )
    assert get_trace_phase() is None


@pytest.mark.asyncio
async def test_invalid_question_plan_blocks_handoff_with_unique_schema_terminal(
    tmp_path: Path,
):
    """非法计划在 QuestionPlan 阶段停止，不产生 Coder handoff。"""
    (tmp_path / "input.csv").write_text(
        "id,value\n1,10\n",
        encoding="utf-8",
    )
    facts = build_task_facts(
        task_id="invalid-plan",
        problem_text="基于第二问结果制定方案。分析历史数据。",
        work_dir=tmp_path,
        data_catalog=inspect_data_catalog(tmp_path),
    )
    store = M15ArtifactStore(tmp_path)
    store.write_json(facts)
    outline = _outline(facts)
    store.write_json(outline)
    client = FakeLLMClient([_cleaning_payload(), "{}"])
    tracer = FakeTracer()

    with pytest.raises(M15StageError) as exc_info:
        await M15Workflow(
            CleaningPlannerAgent(client, max_json_repair_attempts=0),
            ModelerAgent(client, max_repair_attempts=0),
            tracer=tracer,
        ).run(
            task_id="invalid-plan",
            work_dir=tmp_path,
            task_facts=facts,
            outline=outline,
        )

    assert exc_info.value.phase == "question_plan"
    assert exc_info.value.failure_kind == "schema"
    question_ends = [
        payload
        for event, payload in tracer.events
        if event == "end" and payload["phase"] == "question_plan"
    ]
    assert len(question_ends) == 1
    assert question_ends[0]["success"] is False
    assert question_ends[0]["failure_kind"] == "schema"


@pytest.mark.asyncio
async def test_profile_failure_blocks_cleaning_and_planning(
    tmp_path: Path,
):
    """画像失败只留下 profile 终态，不调用任何下游 LLM。"""
    source = tmp_path / "input.csv"
    source.write_text("id,value\n1,10\n", encoding="utf-8")
    facts = build_task_facts(
        task_id="profile-block",
        problem_text="基于第二问结果制定方案。分析历史数据。",
        work_dir=tmp_path,
        data_catalog=inspect_data_catalog(tmp_path),
    )
    M15ArtifactStore(tmp_path).write_json(facts)
    outline = _outline(facts)
    source.write_text("", encoding="utf-8")
    client = FakeLLMClient([])
    tracer = FakeTracer()

    with pytest.raises(M15StageError) as exc_info:
        await M15Workflow(
            CleaningPlannerAgent(client, max_json_repair_attempts=0),
            ModelerAgent(client, max_repair_attempts=0),
            tracer=tracer,
        ).run(
            task_id="profile-block",
            work_dir=tmp_path,
            task_facts=facts,
            outline=outline,
        )

    assert exc_info.value.failure_kind == "profile"
    assert client.calls == 0
    assert [
        payload["phase"] for event, payload in tracer.events if event == "start"
    ] == ["data_profile"]
    ends = [payload for event, payload in tracer.events if event == "end"]
    assert len(ends) == 1
    assert ends[0]["failure_kind"] == "profile"


@pytest.mark.asyncio
async def test_cancellation_preserves_cancelled_error_and_unique_terminal(
    tmp_path: Path,
):
    """取消不包装为普通失败，且当前阶段只记录一个 cancelled 终态。"""
    (tmp_path / "input.csv").write_text(
        "id,value\n1,10\n",
        encoding="utf-8",
    )
    facts = build_task_facts(
        task_id="cancelled",
        problem_text="基于第二问结果制定方案。分析历史数据。",
        work_dir=tmp_path,
        data_catalog=inspect_data_catalog(tmp_path),
    )
    M15ArtifactStore(tmp_path).write_json(facts)
    tracer = FakeTracer()
    checks = 0

    async def cancel() -> None:
        nonlocal checks
        checks += 1
        raise asyncio.CancelledError("stop")

    client = FakeLLMClient([])
    with pytest.raises(asyncio.CancelledError):
        await M15Workflow(
            CleaningPlannerAgent(client, max_json_repair_attempts=0),
            ModelerAgent(client, max_repair_attempts=0),
            tracer=tracer,
            cancel_check=cancel,
        ).run(
            task_id="cancelled",
            work_dir=tmp_path,
            task_facts=facts,
            outline=_outline(facts),
        )

    assert checks == 1
    assert client.calls == 0
    assert tracer.events[0][0] == "start"
    assert tracer.events[-1][0] == "end"
    assert tracer.events[-1][1]["failure_kind"] == "cancelled"
    assert sum(event == "end" for event, _ in tracer.events) == 1


@pytest.mark.asyncio
async def test_contract_boundary_blocks_fingerprint_drift_before_downstream(
    tmp_path: Path,
):
    """冻结后 cleaned 漂移时，任一下游 phase 边界立即阻断。"""
    (tmp_path / "input.csv").write_text(
        "id,value\n1,10\n2,20\n",
        encoding="utf-8",
    )
    tracer = FakeTracer()
    _, result, _ = await _prepare_success(tmp_path, tracer)
    cleaned = tmp_path / result.data_contract.tables[0].cleaned_path
    cleaned.write_text("id,value\n1,999\n", encoding="utf-8")

    with pytest.raises(DataContractIntegrityError):
        await validate_contract_boundary(
            task_id="m15-workflow",
            work_dir=tmp_path,
            contract=result.data_contract,
            phase="ques2",
            boundary="before",
            tracer=tracer,
        )


@pytest.mark.asyncio
async def test_math_model_workflow_does_not_initialize_coder_after_m15_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """主 workflow 在 M1.5 失败后不得创建解释器、Coder、Writer或发布。"""
    (tmp_path / "input.csv").write_text(
        "id,value\n1,10\n",
        encoding="utf-8",
    )
    facts = build_task_facts(
        task_id="main-block",
        problem_text="基于第二问结果制定方案。分析历史数据。",
        work_dir=tmp_path,
        data_catalog=inspect_data_catalog(tmp_path),
    )
    outline = _outline(facts)
    schedule = build_task_schedule(outline)

    class FakeOutlineResult:
        task_facts = facts
        data_catalog = inspect_data_catalog(tmp_path)

        def __init__(self) -> None:
            self.outline = outline
            self.schedule = schedule

        def to_legacy_questions(self) -> dict[str, str | int]:
            return {
                "background": facts.problem_text,
                "ques_count": outline.ques_count,
                **{
                    question.question_id: question.text
                    for question in outline.questions
                },
            }

    class FakeTaskOutlineWorkflow:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        async def create_outline(self, **kwargs):
            del kwargs
            return FakeOutlineResult()

    expected = M15StageError(
        "data_cleaning",
        "verify",
        "unresolved issue",
        issue_ids=("data-issue:block",),
    )

    class BlockingM15Workflow:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs

        async def run(self, **kwargs):
            del kwargs
            raise expected

    fake_llm = SimpleNamespace(model="fake-model")

    class FakeLLMFactory:
        def __init__(self, task_id: str) -> None:
            assert task_id == "main-block"

        def get_all_llms(self):
            return (fake_llm, fake_llm, fake_llm, fake_llm)

    for name in (
        "COORDINATOR_MODEL",
        "COORDINATOR_API_KEY",
        "MODELER_MODEL",
        "MODELER_API_KEY",
        "CODER_MODEL",
        "CODER_API_KEY",
        "WRITER_MODEL",
        "WRITER_API_KEY",
    ):
        monkeypatch.setattr(core_workflow.settings, name, "configured")
    monkeypatch.setattr(
        core_workflow,
        "create_work_dir",
        lambda task_id: str(tmp_path),
    )
    monkeypatch.setattr(core_workflow, "LLMFactory", FakeLLMFactory)
    monkeypatch.setattr(
        core_workflow,
        "TaskOutlineWorkflow",
        FakeTaskOutlineWorkflow,
    )
    monkeypatch.setattr(core_workflow, "M15Workflow", BlockingM15Workflow)
    monkeypatch.setattr(
        core_workflow.redis_manager,
        "publish_message",
        AsyncMock(),
    )
    monkeypatch.setattr(core_workflow.trace_recorder, "emit", AsyncMock())
    create_interpreter = AsyncMock()
    monkeypatch.setattr(core_workflow, "create_interpreter", create_interpreter)

    with pytest.raises(M15StageError) as exc_info:
        await core_workflow.MathModelWorkFlow().execute(
            ApiProblem(
                task_id="main-block",
                ques_all=facts.problem_text,
            )
        )

    assert exc_info.value is expected
    create_interpreter.assert_not_awaited()
