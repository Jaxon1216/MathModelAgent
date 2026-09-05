"""M1.5 版本化领域契约测试。"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.domain.m15 import (
    CandidateKey,
    CleaningPlan,
    ContractColumn,
    ContractDataReference,
    ContractKey,
    ContractTable,
    DataContract,
    DataIssue,
    DataProfile,
    Deliverable,
    FileFact,
    OutlineQuestion,
    ProfileColumn,
    QuestionPlan,
    TaskFacts,
    TaskOutline,
    UpstreamResultReference,
)

SOURCE_SHA = "a" * 64
CLEANED_SHA = "b" * 64


def _task_facts(**updates: object) -> TaskFacts:
    """构造合法 TaskFacts。"""
    values = {
        "schema_version": "m1.5",
        "artifact_id": "task-facts:fixture",
        "source_artifact_ids": (),
        "validation_status": "validated",
        "artifact_path": "m15/task_facts.json",
        "task_id": "fixture-task",
        "problem_text": "根据附件建立模型并填写结果模板。",
        "attachments": (FileFact(path="附件1.xlsx", sha256=SOURCE_SHA),),
        "output_templates": (FileFact(path="result1.xlsx", sha256=CLEANED_SHA),),
    }
    values.update(updates)
    return TaskFacts.model_validate(values)


def _deliverable() -> Deliverable:
    """构造问题交付物。"""
    return Deliverable(
        deliverable_id="deliverable:result1",
        description="填写结果表",
        path="result1.xlsx",
    )


def _task_outline() -> TaskOutline:
    """构造合法 TaskOutline。"""
    return TaskOutline(
        schema_version="m1.5",
        artifact_id="task-outline:fixture",
        source_artifact_ids=("task-facts:fixture",),
        validation_status="validated",
        artifact_path="m15/task_outline.json",
        task_facts_id="task-facts:fixture",
        questions=(
            OutlineQuestion(
                question_id="ques1",
                text="预测总产量。",
                depends_on=(),
                order_key=1,
                deliverables=(_deliverable(),),
            ),
        ),
        deliverables=(_deliverable(),),
    )


def _data_profile() -> DataProfile:
    """构造合法 DataProfile。"""
    return DataProfile(
        schema_version="m1.5",
        artifact_id="data-profile:crop",
        source_artifact_ids=("task-outline:fixture",),
        validation_status="validated",
        artifact_path="m15/profiles/crop.json",
        task_outline_id="task-outline:fixture",
        table_id="附件1.xlsx::农作物",
        source_path="附件1.xlsx",
        source_sheet="农作物",
        source_sha256=SOURCE_SHA,
        row_count=2,
        column_count=2,
        columns=(
            ProfileColumn(
                name="作物编号",
                source_name="作物编号",
                raw_dtype="int64",
                canonical_type="integer",
                missing_count=0,
                missing_ratio=0.0,
                unique_count=2,
            ),
            ProfileColumn(
                name="亩产量",
                source_name="亩产量",
                raw_dtype="float64",
                canonical_type="number",
                missing_count=0,
                missing_ratio=0.0,
                unique_count=2,
            ),
        ),
        duplicate_row_count=0,
        candidate_keys=(
            CandidateKey(
                columns=("作物编号",),
                uniqueness_ratio=1.0,
                non_null_ratio=1.0,
            ),
        ),
    )


def _cleaning_plan() -> CleaningPlan:
    """构造无需修改数据的合法 CleaningPlan。"""
    return CleaningPlan(
        schema_version="m1.5",
        artifact_id="cleaning-plan:crop",
        source_artifact_ids=(
            "task-outline:fixture",
            "data-profile:crop",
        ),
        validation_status="validated",
        artifact_path="m15/cleaning_plans/crop.json",
        task_outline_id="task-outline:fixture",
        data_profile_id="data-profile:crop",
        table_id="附件1.xlsx::农作物",
        operations=(),
        no_op_reason="画像满足全部声明约束，仍生成独立 cleaned 文件。",
    )


def _data_issue() -> DataIssue:
    """构造合法结构化 DataIssue。"""
    return DataIssue(
        schema_version="m1.5",
        artifact_id="data-issue:crop-key",
        source_artifact_ids=("cleaning-plan:crop",),
        validation_status="validated",
        artifact_path="m15/issues/crop-key.json",
        issue_id="data-issue:crop-key",
        table_id="附件1.xlsx::农作物",
        rule_id="rule:key-unique",
        expected=True,
        actual=False,
        evidence_summary="作物编号存在重复值。",
        repair_attempt=0,
        status="unresolved",
    )


def _data_contract() -> DataContract:
    """构造合法冻结 DataContract。"""
    return DataContract(
        schema_version="m1.5",
        artifact_id="data-contract:fixture",
        source_artifact_ids=(
            "data-profile:crop",
            "cleaning-plan:crop",
        ),
        validation_status="validated",
        artifact_path="m15/data_contract.json",
        status="frozen",
        tables=(
            ContractTable(
                table_id="附件1.xlsx::农作物",
                data_profile_id="data-profile:crop",
                data_profile_path="m15/profiles/crop.json",
                cleaning_plan_id="cleaning-plan:crop",
                cleaning_plan_path="m15/cleaning_plans/crop.json",
                source_path="附件1.xlsx",
                source_sheet="农作物",
                source_sha256=SOURCE_SHA,
                cleaned_path="cleaned/0f7bb48cc8947464.csv",
                cleaned_sha256=CLEANED_SHA,
                row_count=2,
                columns=(
                    ContractColumn(
                        name="作物编号",
                        source_name="作物编号",
                        canonical_type="integer",
                        nullable=False,
                        statistic_definition="附件中的作物唯一编号",
                    ),
                ),
                keys=(
                    ContractKey(
                        key_id="contract-key:crop-id",
                        columns=("作物编号",),
                        kind="candidate",
                        uniqueness_verified=True,
                        non_null_verified=True,
                        validation_rule_ids=(
                            "rule:key-unique",
                            "rule:key-non-null",
                        ),
                    ),
                ),
            ),
        ),
        scanned_attachments=("附件1.xlsx",),
        output_templates=("result1.xlsx",),
    )


def _question_plan() -> QuestionPlan:
    """构造合法 QuestionPlan。"""
    return QuestionPlan(
        schema_version="m1.5",
        artifact_id="question-plan:ques1",
        source_artifact_ids=(
            "task-outline:fixture",
            "data-contract:fixture",
        ),
        validation_status="validated",
        artifact_path="m15/question_plans/ques1.json",
        question_id="ques1",
        task_outline_id="task-outline:fixture",
        data_contract_id="data-contract:fixture",
        objective="预测总产量。",
        data=(
            ContractDataReference(
                table_id="附件1.xlsx::农作物",
                columns=("作物编号",),
            ),
        ),
        upstream_results=(),
        model="回归模型",
        method="拟合并验证预测误差。",
        constraints=("产量非负",),
        validation=("交叉验证",),
        figures=("预测值与真实值对比图",),
        deliverables=(_deliverable(),),
        fallback="使用历史均值基线。",
    )


@pytest.mark.parametrize(
    "artifact",
    [
        pytest.param(_task_facts(), id="task-facts"),
        pytest.param(_task_outline(), id="task-outline"),
        pytest.param(_data_profile(), id="data-profile"),
        pytest.param(_cleaning_plan(), id="cleaning-plan"),
        pytest.param(_data_issue(), id="data-issue"),
        pytest.param(_data_contract(), id="data-contract"),
        pytest.param(_question_plan(), id="question-plan"),
    ],
)
def test_m15_schema_json_round_trip(artifact):
    """七类领域产物都能通过同一 schema 无损 JSON 往返。"""
    restored = type(artifact).model_validate_json(artifact.model_dump_json())

    assert restored == artifact
    assert restored.schema_version == "m1.5"


def test_contracts_reject_unknown_fields():
    """严格契约必须拒绝模型或调用方注入的未知字段。"""
    payload = _task_facts().model_dump(mode="json")
    payload["unexpected"] = "not allowed"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        TaskFacts.model_validate_json(json.dumps(payload, ensure_ascii=False))


@pytest.mark.parametrize(
    ("expected_question_ids", "expected_question_count"),
    [
        (("ques1", "ques3"), 2),
        (("ques1", "ques2"), 1),
        (("ques1",), None),
    ],
)
def test_task_facts_rejects_inconsistent_expected_question_boundary(
    expected_question_ids: tuple[str, ...],
    expected_question_count: int | None,
):
    """独立问题标识必须连续，且数量不能由调用方另行声明。"""
    payload = _task_facts().model_dump(mode="json")
    payload["expected_question_ids"] = expected_question_ids
    payload["expected_question_count"] = expected_question_count

    with pytest.raises(ValidationError, match="预期问题"):
        TaskFacts.model_validate_json(json.dumps(payload, ensure_ascii=False))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", "m1", "Input should be 'm1.5'"),
        ("validation_status", "pending", "Input should be"),
        ("artifact_id", "Task Facts", "稳定标识"),
        ("artifact_path", "../task_facts.json", "相对路径"),
        ("artifact_path", "/tmp/task_facts.json", "相对路径"),
        ("artifact_path", "other/task_facts.json", "必须位于 m15"),
        ("artifact_path", r"m15\\task_facts.json", "POSIX"),
    ],
)
def test_shared_metadata_rejects_invalid_values(
    field: str,
    value: str,
    message: str,
):
    """共享元数据拒绝非法版本、状态、标识和路径。"""
    payload = _task_facts().model_dump(mode="json")
    payload[field] = value

    with pytest.raises(ValidationError, match=message):
        TaskFacts.model_validate_json(json.dumps(payload, ensure_ascii=False))


def test_file_fact_rejects_invalid_sha256():
    """来源文件指纹必须是小写十六进制 SHA-256。"""
    with pytest.raises(ValidationError, match="string_pattern_mismatch"):
        FileFact(path="附件1.xlsx", sha256="ABC123")


def test_artifact_rejects_invalid_source_relationship():
    """显式来源字段必须同步出现在共享来源元数据中。"""
    payload = _cleaning_plan().model_dump(mode="json")
    payload["source_artifact_ids"] = ["task-outline:fixture"]

    with pytest.raises(ValidationError, match="data-profile:crop"):
        CleaningPlan.model_validate_json(json.dumps(payload, ensure_ascii=False))


def test_data_contract_rejects_illegal_status_shape():
    """冻结状态不能伪装空契约，no-data 状态也不能携带表。"""
    frozen_payload = _data_contract().model_dump(mode="json")
    frozen_payload["tables"] = []
    with pytest.raises(ValidationError, match="至少包含一张表"):
        DataContract.model_validate_json(json.dumps(frozen_payload, ensure_ascii=False))

    no_data_payload = _data_contract().model_dump(mode="json")
    no_data_payload["status"] = "no_data"
    with pytest.raises(ValidationError, match="不能包含表"):
        DataContract.model_validate_json(
            json.dumps(no_data_payload, ensure_ascii=False)
        )

    invalid_payload = _data_contract().model_dump(mode="json")
    invalid_payload["status"] = "invalid"
    with pytest.raises(ValidationError, match="validation_status=failed"):
        DataContract.model_validate_json(
            json.dumps(invalid_payload, ensure_ascii=False)
        )


def test_question_plan_rejects_self_upstream_reference():
    """按题计划不能绕过后续依赖校验直接引用自身结果。"""
    payload = _question_plan().model_dump(mode="json")
    payload["upstream_results"] = [
        UpstreamResultReference(
            question_id="ques1",
            outputs=("output:prediction",),
        ).model_dump(mode="json")
    ]

    with pytest.raises(ValidationError, match="不能引用自身"):
        QuestionPlan.model_validate_json(json.dumps(payload, ensure_ascii=False))


def test_profile_preserves_original_header_whitespace():
    """原始表头不能在 schema 层被静默改写。"""
    column = ProfileColumn(
        name="说明",
        source_name="说明 ",
        raw_dtype="object",
        canonical_type="string",
        missing_count=0,
        missing_ratio=0.0,
        unique_count=1,
    )

    assert column.source_name == "说明 "


@pytest.mark.parametrize(
    ("invalid_kind", "message"),
    [
        pytest.param(
            "table_id",
            "必须等于源文件与 Sheet 组合",
            id="table-id",
        ),
        pytest.param(
            "xlsx_without_sheet",
            "XLSX DataProfile 必须声明 source_sheet",
            id="xlsx-without-sheet",
        ),
        pytest.param(
            "csv_with_sheet",
            "CSV DataProfile 不能声明 source_sheet",
            id="csv-with-sheet",
        ),
        pytest.param(
            "missing_count",
            "缺失数不能超过总行数",
            id="missing-count",
        ),
        pytest.param(
            "missing_ratio",
            "缺失比例与行数不一致",
            id="missing-ratio",
        ),
        pytest.param(
            "candidate_key_column",
            "候选键包含未知列",
            id="candidate-key-column",
        ),
    ],
)
def test_data_profile_rejects_internally_inconsistent_statistics(
    invalid_kind: str,
    message: str,
):
    """DataProfile 不能接受无法由自身行列事实支持的统计或引用。"""
    payload = _data_profile().model_dump(mode="json")
    if invalid_kind == "table_id":
        payload["table_id"] = "错误表名"
    elif invalid_kind == "xlsx_without_sheet":
        payload["source_sheet"] = None
    elif invalid_kind == "csv_with_sheet":
        payload["source_path"] = "附件1.csv"
    elif invalid_kind == "missing_count":
        payload["columns"][0]["missing_count"] = 3
    elif invalid_kind == "missing_ratio":
        payload["columns"][0]["missing_ratio"] = 0.5
    else:
        payload["candidate_keys"][0]["columns"] = ["不存在"]

    with pytest.raises(ValidationError, match=message):
        DataProfile.model_validate_json(json.dumps(payload, ensure_ascii=False))
