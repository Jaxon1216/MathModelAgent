"""M1.5 QuestionPlan 生成、校验与旧 Coder 桥接测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.agents.modeler import ModelerAgent, ModelerResponseError
from app.core.flows import Flows
from app.data import (
    DataContractIntegrityError,
    M15ArtifactStore,
    build_task_facts,
    execute_cleaning_plan,
    freeze_data_contract,
    inspect_data_catalog,
    inspect_data_profiles,
)
from app.domain.m15 import (
    CleaningPlan,
    DataContract,
    Deliverable,
    OutlineQuestion,
    TaskOutline,
    UpstreamResultReference,
)
from app.domain.model_plan import (
    MAX_CODER_HANDOFF_CHARS,
    QuestionPlanValidation,
    QuestionPlanValidationError,
)
from app.schemas.A2A import ModelerToCoder

OUTLINE_ID = "task-outline:question-plan"


class FakeLLMClient:
    """记录请求并按顺序返回固定响应。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = iter(responses)
        self.messages: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]]) -> str:
        """返回下一条响应。"""
        self.messages.append(list(messages))
        return next(self._responses)


def _deliverable(question_id: str) -> Deliverable:
    """构造与问题绑定的稳定交付物。"""
    return Deliverable(
        deliverable_id=f"deliverable:{question_id}",
        description=f"提交 {question_id} 的模型结果",
    )


def _outline() -> TaskOutline:
    """构造包含传递依赖的三问题骨架。"""
    questions = (
        OutlineQuestion(
            question_id="ques1",
            text="分析历史产量。",
            depends_on=(),
            order_key=1,
            deliverables=(_deliverable("ques1"),),
        ),
        OutlineQuestion(
            question_id="ques2",
            text="预测下一期产量。",
            depends_on=("ques1",),
            order_key=2,
            deliverables=(_deliverable("ques2"),),
        ),
        OutlineQuestion(
            question_id="ques3",
            text="制定优化方案。",
            depends_on=("ques2",),
            order_key=3,
            deliverables=(_deliverable("ques3"),),
        ),
    )
    return TaskOutline(
        schema_version="m1.5",
        artifact_id=OUTLINE_ID,
        source_artifact_ids=("task-facts:question-plan",),
        validation_status="validated",
        artifact_path="m15/task_outline.json",
        task_facts_id="task-facts:question-plan",
        questions=questions,
        deliverables=tuple(
            deliverable
            for question in questions
            for deliverable in question.deliverables
        ),
    )


def _prepare_contract(tmp_path: Path) -> tuple[TaskOutline, DataContract]:
    """从真实 CSV 构造可持续复验的冻结契约。"""
    (tmp_path / "input.csv").write_text(
        "year,yield\n2024,10\n2025,12\n",
        encoding="utf-8",
    )
    (tmp_path / "result.csv").write_text(
        "year,prediction\n",
        encoding="utf-8",
    )
    outline = _outline()
    facts = build_task_facts(
        task_id="question-plan",
        problem_text="分析历史产量。预测下一期产量。制定优化方案。",
        work_dir=tmp_path,
        data_catalog=inspect_data_catalog(tmp_path),
    ).model_copy(
        update={"artifact_id": "task-facts:question-plan"},
    )
    store = M15ArtifactStore(tmp_path)
    store.write_json(facts)
    store.write_json(outline)

    profiles = inspect_data_profiles(tmp_path, outline.artifact_id)
    plans = tuple(
        CleaningPlan(
            schema_version="m1.5",
            artifact_id=(
                "cleaning-plan:" + profile.artifact_id.removeprefix("data-profile:")
            ),
            source_artifact_ids=(outline.artifact_id, profile.artifact_id),
            validation_status="validated",
            artifact_path=(
                "m15/cleaning_plans/"
                + profile.artifact_id.removeprefix("data-profile:")
                + ".json"
            ),
            task_outline_id=outline.artifact_id,
            data_profile_id=profile.artifact_id,
            table_id=profile.table_id,
            operations=(),
            no_op_reason="画像已满足计划所需的数据约束。",
        )
        for profile in profiles
    )
    for plan in plans:
        store.write_json(plan)
    cleaned_tables = tuple(
        execute_cleaning_plan(
            tmp_path,
            profile,
            plan,
            source_profiles=profiles,
        )
        for profile, plan in zip(profiles, plans, strict=True)
    )
    contract = freeze_data_contract(
        tmp_path,
        task_facts=facts,
        profiles=profiles,
        plans=plans,
        cleaned_tables=cleaned_tables,
    )
    return outline, contract


def _declarations() -> tuple[UpstreamResultReference, ...]:
    """构造可供依赖问题消费的结果声明。"""
    return (
        UpstreamResultReference(
            question_id="ques1",
            outputs=("output:history-summary",),
        ),
        UpstreamResultReference(
            question_id="ques2",
            outputs=("output:forecast",),
        ),
    )


def _section(
    question_id: str,
    *,
    upstream_results: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """构造一个完整计划提案。"""
    return {
        "question_id": question_id,
        "data": [
            {
                "table_id": "input.csv",
                "columns": ["year", "yield"],
            }
        ],
        "upstream_results": upstream_results or [],
        "objective": f"完成 {question_id} 的可验证目标",
        "model": "使用带基线对比的趋势模型",
        "method": "读取 cleaned 数据，拟合模型并检查结果",
        "constraints": ["预测值非负"],
        "validation": ["滚动留出误差"],
        "figures": ["拟合与预测对比图"],
        "deliverables": [
            _deliverable(question_id).model_dump(mode="json"),
        ],
        "fallback": "退化为历史均值基线",
    }


def _payload() -> dict[str, object]:
    """构造覆盖三问及敏感性分析的合法响应。"""
    sensitivity = _section("ques1")
    sensitivity.pop("question_id")
    sensitivity.pop("upstream_results")
    sensitivity.pop("deliverables")
    sensitivity["objective"] = "评估关键参数扰动"
    sensitivity["figures"] = ["敏感性曲线"]
    return {
        "schema_version": "m1.5",
        "question_plans": [
            _section("ques1"),
            _section(
                "ques2",
                upstream_results=[
                    {
                        "question_id": "ques1",
                        "outputs": ["output:history-summary"],
                    }
                ],
            ),
            _section(
                "ques3",
                upstream_results=[
                    {
                        "question_id": "ques1",
                        "outputs": ["output:history-summary"],
                    },
                    {
                        "question_id": "ques2",
                        "outputs": ["output:forecast"],
                    },
                ],
            ),
        ],
        "sensitivity_analysis": sensitivity,
    }


def _validator(
    outline: TaskOutline,
    contract: DataContract,
) -> QuestionPlanValidation:
    """构造标准按题计划校验器。"""
    return QuestionPlanValidation.for_inputs(
        outline,
        contract,
        _declarations(),
    )


def test_modeler_generates_complete_plans_and_persists_cleaned_handoff(
    tmp_path: Path,
):
    """完整计划覆盖 outline，并只向旧 Coder 渲染 cleaned 数据定位。"""
    outline, contract = _prepare_contract(tmp_path)
    client = FakeLLMClient([json.dumps(_payload(), ensure_ascii=False)])

    collection = asyncio.run(
        ModelerAgent(client, max_repair_attempts=0).run(
            outline,
            contract,
            work_dir=tmp_path,
            upstream_result_declarations=_declarations(),
        )
    )
    handoff = collection.to_coder_handoff(tmp_path, contract)

    assert [plan.question_id for plan in collection.question_plans] == [
        "ques1",
        "ques2",
        "ques3",
    ]
    assert set(handoff) == {
        "ques1",
        "ques2",
        "ques3",
        "sensitivity_analysis",
    }
    assert "cleaned_path='cleaned/" in handoff["ques1"]
    assert "verified_columns=year:integer, yield:integer" in handoff["ques1"]
    assert "source_path" not in handoff["ques1"]
    assert "input.csv" not in handoff["ques1"]
    assert "source_sha256" not in client.messages[0][1]["content"]
    assert '"output_templates"' not in client.messages[0][1]["content"]
    assert len(client.messages) == 1
    legacy_questions = {
        "background": "产量建模",
        "ques_count": 3,
        **{question.question_id: question.text for question in outline.questions},
    }
    flows = Flows(legacy_questions).get_solution_flows(
        legacy_questions,
        ModelerToCoder(questions_solution=handoff),
    )
    assert "cleaned_path='cleaned/" in flows["ques1"]["coder_prompt"]
    assert "output:history-summary" in flows["ques3"]["coder_prompt"]
    for plan in collection.question_plans:
        assert (
            M15ArtifactStore(tmp_path).read_json(
                plan.artifact_path,
                type(plan),
            )
            == plan
        )


def test_question_plan_requires_exact_outline_coverage(tmp_path: Path):
    """缺失任一 outline 问题时集合校验失败。"""
    outline, contract = _prepare_contract(tmp_path)
    payload = _payload()
    payload["question_plans"] = payload["question_plans"][:-1]

    with pytest.raises(
        QuestionPlanValidationError,
        match="question_keys_mismatch",
    ):
        _validator(outline, contract).parse_json(
            json.dumps(payload, ensure_ascii=False)
        )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("unknown_column", "unknown_data_columns"),
        ("raw_path", "Extra inputs are not permitted"),
        ("output_template", "unknown_data_table"),
    ],
)
def test_question_plan_rejects_contract_external_or_path_inputs(
    tmp_path: Path,
    mutation: str,
    reason: str,
):
    """字段、原始路径字段和输出模板都不能伪装成数据输入。"""
    outline, contract = _prepare_contract(tmp_path)
    payload = _payload()
    reference = payload["question_plans"][0]["data"][0]
    if mutation == "unknown_column":
        reference["columns"] = ["invented"]
    elif mutation == "raw_path":
        reference["source_path"] = "input.csv"
    else:
        reference["table_id"] = "result.csv"

    with pytest.raises(QuestionPlanValidationError, match=reason):
        _validator(outline, contract).parse_json(
            json.dumps(payload, ensure_ascii=False)
        )


@pytest.mark.parametrize(
    ("question_id", "upstream_id", "outputs", "reason"),
    [
        (
            "ques1",
            "ques2",
            ["output:forecast"],
            "invalid_upstream_question",
        ),
        (
            "ques2",
            "ques1",
            ["output:invented"],
            "unknown_upstream_outputs",
        ),
    ],
)
def test_question_plan_rejects_illegal_upstream_results(
    tmp_path: Path,
    question_id: str,
    upstream_id: str,
    outputs: list[str],
    reason: str,
):
    """上游结果必须同时属于传递依赖和允许的输出声明。"""
    outline, contract = _prepare_contract(tmp_path)
    payload = _payload()
    plan = next(
        item for item in payload["question_plans"] if item["question_id"] == question_id
    )
    plan["upstream_results"] = [{"question_id": upstream_id, "outputs": outputs}]

    with pytest.raises(QuestionPlanValidationError, match=reason):
        _validator(outline, contract).parse_json(
            json.dumps(payload, ensure_ascii=False)
        )


def test_question_plan_requires_outline_deliverables(tmp_path: Path):
    """模型不能增删或改写单题交付物。"""
    outline, contract = _prepare_contract(tmp_path)
    payload = _payload()
    payload["question_plans"][1]["deliverables"][0]["description"] = "改写交付物"

    with pytest.raises(
        QuestionPlanValidationError,
        match="deliverables_mismatch",
    ):
        _validator(outline, contract).parse_json(
            json.dumps(payload, ensure_ascii=False)
        )


@pytest.mark.parametrize("failure", ["not_frozen", "cleaned_drift"])
def test_modeler_blocks_invalid_contract_before_llm(
    tmp_path: Path,
    failure: str,
):
    """未冻结或冻结后漂移的契约必须在调用 Modeler 前阻断。"""
    outline, contract = _prepare_contract(tmp_path)
    if failure == "not_frozen":
        contract = contract.model_copy(
            update={"status": "invalid", "validation_status": "failed"}
        )
        M15ArtifactStore(tmp_path).write_json(contract)
    else:
        cleaned_path = tmp_path / contract.tables[0].cleaned_path
        cleaned_path.write_text("year,yield\n2024,999\n", encoding="utf-8")
    client = FakeLLMClient([json.dumps(_payload(), ensure_ascii=False)])

    with pytest.raises(DataContractIntegrityError):
        asyncio.run(
            ModelerAgent(client, max_repair_attempts=0).run(
                outline,
                contract,
                work_dir=tmp_path,
                upstream_result_declarations=_declarations(),
            )
        )

    assert client.messages == []


def test_question_plan_repair_is_bounded_and_stateless(tmp_path: Path):
    """修复请求不携带无效正文，并严格遵守配置次数。"""
    outline, contract = _prepare_contract(tmp_path)
    invalid = "not JSON"
    client = FakeLLMClient([invalid, invalid])

    with pytest.raises(ModelerResponseError) as exc_info:
        asyncio.run(
            ModelerAgent(client, max_repair_attempts=1).run(
                outline,
                contract,
                work_dir=tmp_path,
                upstream_result_declarations=_declarations(),
            )
        )

    assert exc_info.value.attempts == 2
    assert len(client.messages) == 2
    assert len(client.messages[0]) == 2
    assert len(client.messages[1]) == 3
    assert invalid not in str(client.messages[1])


def test_no_data_contract_requires_empty_data_references(tmp_path: Path):
    """显式 no-data 契约可规划，但所有问题和敏感性数据引用必须为空。"""
    outline = _outline()
    facts = build_task_facts(
        task_id="question-plan",
        problem_text="分析历史产量。预测下一期产量。制定优化方案。",
        work_dir=tmp_path,
        data_catalog=inspect_data_catalog(tmp_path),
    ).model_copy(update={"artifact_id": "task-facts:question-plan"})
    store = M15ArtifactStore(tmp_path)
    store.write_json(facts)
    store.write_json(outline)
    contract = freeze_data_contract(
        tmp_path,
        task_facts=facts,
        profiles=(),
        plans=(),
        cleaned_tables=(),
    )
    payload = _payload()
    for section in payload["question_plans"]:
        section["data"] = []
    payload["sensitivity_analysis"]["data"] = []

    collection = asyncio.run(
        ModelerAgent(
            FakeLLMClient([json.dumps(payload, ensure_ascii=False)]),
            max_repair_attempts=0,
        ).run(
            outline,
            contract,
            work_dir=tmp_path,
            upstream_result_declarations=_declarations(),
        )
    )
    assert all(not plan.data for plan in collection.question_plans)

    payload["question_plans"][0]["data"] = [
        {"table_id": "input.csv", "columns": ["yield"]}
    ]
    with pytest.raises(QuestionPlanValidationError, match="unknown_data_table"):
        _validator(outline, contract).parse_json(
            json.dumps(payload, ensure_ascii=False)
        )


def test_coder_handoff_rechecks_contract_integrity(tmp_path: Path):
    """计划生成后 cleaned 文件漂移时，旧 Coder 桥接必须再次阻断。"""
    outline, contract = _prepare_contract(tmp_path)
    collection = _validator(outline, contract).parse_json(
        json.dumps(_payload(), ensure_ascii=False)
    )
    cleaned_path = tmp_path / contract.tables[0].cleaned_path
    cleaned_path.write_text("year,yield\n2024,999\n", encoding="utf-8")

    with pytest.raises(DataContractIntegrityError):
        collection.to_coder_handoff(tmp_path, contract)


def test_question_plan_rejects_oversized_coder_handoff(tmp_path: Path):
    """各字段虽独立合法，聚合文本超限时仍不能通过计划契约。"""
    outline, contract = _prepare_contract(tmp_path)
    payload = _payload()
    section = payload["question_plans"][0]
    for key in ("objective", "model", "method", "fallback"):
        section[key] = "x" * 1200
    section["constraints"] = ["x" * 300] * 12
    section["validation"] = ["x" * 300] * 8
    section["figures"] = ["x" * 300] * 8

    with pytest.raises(
        QuestionPlanValidationError,
        match="coder_handoff_too_long",
    ):
        _validator(outline, contract).parse_json(
            json.dumps(payload, ensure_ascii=False)
        )

    valid = _validator(outline, contract).parse_json(
        json.dumps(_payload(), ensure_ascii=False)
    )
    assert all(
        len(text) <= MAX_CODER_HANDOFF_CHARS
        for text in valid.to_coder_handoff(tmp_path, contract).values()
    )
