"""M1.5 白名单 CleaningPlan 执行与验证测试。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest
from pydantic import ValidationError

from app.agents.cleaning_planner import (
    CleaningPlanResponseError,
    CleaningPlannerAgent,
    validate_cleaning_plan_scope,
)
from app.data import (
    CleanedTable,
    TableCleaningError,
    cleaned_table_path,
    compute_file_sha256,
    execute_cleaning_plan,
    inspect_data_profiles,
    verify_cleaned_table,
)
from app.domain.m15 import (
    CleaningOperation,
    CleaningPlan,
    DataIssue,
    Deliverable,
    OutlineQuestion,
    TaskOutline,
    ValidationExpectation,
)

OUTLINE_ID = "task-outline:clean-test"


class FakeLLMClient:
    """记录无状态请求并返回预设 JSON。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = iter(responses)
        self.messages: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]]) -> str:
        """返回下一条响应。"""
        self.messages.append(list(messages))
        return next(self._responses)


def _outline() -> TaskOutline:
    """构造清洗规划使用的最小 TaskOutline。"""
    deliverable = Deliverable(
        deliverable_id="deliverable:report",
        description="提交分析结论",
    )
    return TaskOutline(
        schema_version="m1.5",
        artifact_id=OUTLINE_ID,
        source_artifact_ids=("task-facts:clean-test",),
        validation_status="validated",
        artifact_path="m15/task_outline.json",
        task_facts_id="task-facts:clean-test",
        questions=(
            OutlineQuestion(
                question_id="ques1",
                text="清洗数据后分析。",
                order_key=1,
                deliverables=(deliverable,),
            ),
        ),
        deliverables=(deliverable,),
    )


def _expectation(
    rule_id: str,
    rule_type: str,
    *,
    columns: tuple[str, ...] = (),
    operator: str = "eq",
    expected: object,
) -> ValidationExpectation:
    """构造测试后置条件。"""
    return ValidationExpectation.model_validate(
        {
            "rule_id": rule_id,
            "rule_type": rule_type,
            "columns": columns,
            "operator": operator,
            "expected": expected,
        }
    )


def _plan(profile, operations=(), *, no_op_reason=None) -> CleaningPlan:
    """构造当前画像的初始 CleaningPlan。"""
    digest = profile.artifact_id.removeprefix("data-profile:")
    return CleaningPlan(
        schema_version="m1.5",
        artifact_id=f"cleaning-plan:{digest}",
        source_artifact_ids=(OUTLINE_ID, profile.artifact_id),
        validation_status="validated",
        artifact_path=f"m15/cleaning_plans/{digest}.json",
        task_outline_id=OUTLINE_ID,
        data_profile_id=profile.artifact_id,
        table_id=profile.table_id,
        operations=operations,
        no_op_reason=no_op_reason,
    )


def test_whitelisted_operations_execute_deterministically(tmp_path: Path):
    """空行、forward-fill、键规范化、类型转换和按键去重可组合执行。"""
    (tmp_path / "input.csv").write_text(
        "组,关联键,数值,说明\nA, X ,1,\n,x,1,\n,,,\nB,Y,2,保留\n",
        encoding="utf-8",
    )
    (profile,) = inspect_data_profiles(tmp_path, OUTLINE_ID)
    operations = (
        CleaningOperation(
            operation_id="clean-op:empty",
            operation_type="drop_empty_rows",
            target_columns=("组", "关联键", "数值", "说明"),
            strategy="strict",
            reason="删除所有业务字段均为空的占位行。",
            postconditions=(
                _expectation(
                    "rule:rows",
                    "row_count",
                    operator="le",
                    expected=3,
                ),
            ),
        ),
        CleaningOperation(
            operation_id="clean-op:group-fill",
            operation_type="fill_missing",
            target_columns=("组",),
            strategy="forward_fill",
            reason="恢复合并单元格导入后缺失的分组键。",
            postconditions=(
                _expectation(
                    "rule:group-present",
                    "missing_count",
                    columns=("组",),
                    expected=0,
                ),
            ),
        ),
        CleaningOperation(
            operation_id="clean-op:join-key",
            operation_type="normalize_join_key",
            target_columns=("关联键",),
            normalizations=("unicode_nfkc", "strip", "casefold"),
            reason="统一关联键的 Unicode、空白和大小写。",
            postconditions=(
                _expectation(
                    "rule:key-string",
                    "canonical_type",
                    columns=("关联键",),
                    expected="string",
                ),
            ),
        ),
        CleaningOperation(
            operation_id="clean-op:value-type",
            operation_type="cast_type",
            target_columns=("数值",),
            target_type="integer",
            strategy="strict",
            reason="画像值均可无损转换为整数。",
            postconditions=(
                _expectation(
                    "rule:value-integer",
                    "canonical_type",
                    columns=("数值",),
                    expected="integer",
                ),
            ),
        ),
        CleaningOperation(
            operation_id="clean-op:dedupe",
            operation_type="drop_duplicates",
            target_columns=("组", "关联键", "数值"),
            keep="first",
            reason="规范化后按明确业务键保留首条。",
            postconditions=(
                _expectation(
                    "rule:key-unique",
                    "key_unique",
                    columns=("组", "关联键", "数值"),
                    expected=True,
                ),
            ),
        ),
    )
    plan = _plan(profile, operations)

    cleaned = execute_cleaning_plan(tmp_path, profile, plan)
    failures = verify_cleaned_table(tmp_path, profile, plan, cleaned)

    assert failures == ()
    assert cleaned.relative_path.startswith("cleaned/")
    assert cleaned.relative_path.endswith(".csv")
    frame = pd.read_csv(tmp_path / cleaned.relative_path, encoding="utf-8")
    assert frame[["组", "关联键", "数值"]].to_dict("records") == [
        {"组": "A", "关联键": "x", "数值": 1},
        {"组": "B", "关联键": "y", "数值": 2},
    ]


def test_no_op_still_publishes_independent_cleaned_file(tmp_path: Path):
    """显式 no-op 仍执行完整复制、重载和验证。"""
    source = tmp_path / "input.csv"
    source.write_text("id,value\n1,a\n2,b\n", encoding="utf-8")
    (profile,) = inspect_data_profiles(tmp_path, OUTLINE_ID)
    plan = _plan(profile, no_op_reason="画像已经满足全部约束。")

    cleaned = execute_cleaning_plan(tmp_path, profile, plan)

    assert verify_cleaned_table(tmp_path, profile, plan, cleaned) == ()
    assert (tmp_path / cleaned.relative_path).read_text(encoding="utf-8") == (
        "id,value\n1,a\n2,b\n"
    )
    assert source.read_text(encoding="utf-8") == "id,value\n1,a\n2,b\n"


@pytest.mark.parametrize(
    "payload_update",
    [
        pytest.param(
            {"operation_type": "python", "code": "open('/tmp/x','w')"},
            id="unsupported-code",
        ),
        pytest.param(
            {"external_path": "../../outside.csv"},
            id="external-path",
        ),
        pytest.param(
            {"expression": "df.query('value > 0')"},
            id="expression",
        ),
    ],
)
def test_cleaning_operation_rejects_unsupported_capabilities(payload_update):
    """未知操作、代码、表达式和路径在 schema 层即被拒绝。"""
    payload = {
        "operation_id": "clean-op:unsafe",
        "operation_type": "cast_type",
        "target_columns": ["value"],
        "target_type": "integer",
        "strategy": "strict",
        "reason": "测试非法能力。",
        "postconditions": [
            {
                "rule_id": "rule:value-type",
                "rule_type": "canonical_type",
                "columns": ["value"],
                "operator": "eq",
                "expected": "integer",
            }
        ],
        **payload_update,
    }

    with pytest.raises(ValidationError):
        CleaningOperation.model_validate(payload)


@pytest.mark.parametrize(
    "payload_update",
    [
        pytest.param(
            {
                "operation_type": "cast_type",
                "target_type": "integer",
                "strategy": "strict",
                "keep": "first",
            },
            id="cast-with-deduplicate-parameter",
        ),
        pytest.param(
            {
                "operation_type": "fill_missing",
                "strategy": "mean",
                "fill_value": 0,
            },
            id="non-constant-with-fill-value",
        ),
        pytest.param(
            {
                "operation_type": "drop_duplicates",
                "target_type": "string",
                "keep": "first",
            },
            id="deduplicate-with-cast-parameter",
        ),
        pytest.param(
            {
                "operation_type": "normalize_join_key",
                "normalizations": ["strip"],
                "strategy": "strict",
            },
            id="normalization-with-strategy",
        ),
    ],
)
def test_cleaning_operation_rejects_parameters_from_other_operation_types(
    payload_update,
):
    """各白名单操作不能夹带其他操作类型的参数。"""
    payload = {
        "operation_id": "clean-op:typed",
        "operation_type": "cast_type",
        "target_columns": ["value"],
        "target_type": None,
        "strategy": None,
        "fill_value": None,
        "keep": None,
        "normalizations": [],
        "reason": "验证类型专属参数。",
        "postconditions": [
            {
                "rule_id": "rule:value-type",
                "rule_type": "canonical_type",
                "columns": ["value"],
                "operator": "eq",
                "expected": "integer",
            }
        ],
        **payload_update,
    }

    with pytest.raises(ValidationError):
        CleaningOperation.model_validate(payload)


def test_plan_scope_rejects_non_join_column_normalization(tmp_path: Path):
    """关联键规范化只能作用于画像中已有证据的关联键。"""
    (tmp_path / "input.csv").write_text(
        "id,label\n1,A\n2,B\n",
        encoding="utf-8",
    )
    (profile,) = inspect_data_profiles(tmp_path, OUTLINE_ID)
    operation = CleaningOperation(
        operation_id="clean-op:not-a-join",
        operation_type="normalize_join_key",
        target_columns=("label",),
        normalizations=("strip",),
        reason="尝试规范化非关联字段。",
        postconditions=(
            _expectation(
                "rule:label-string",
                "canonical_type",
                columns=("label",),
                expected="string",
            ),
        ),
    )

    with pytest.raises(ValueError, match="画像声明的关联键"):
        validate_cleaning_plan_scope(_plan(profile, (operation,)), profile)


def test_plan_scope_rejects_incompatible_constant_fill(tmp_path: Path):
    """constant 参数必须与目标列的规范类型一致。"""
    (tmp_path / "input.csv").write_text(
        "id,value\n1,10\n2,\n",
        encoding="utf-8",
    )
    (profile,) = inspect_data_profiles(tmp_path, OUTLINE_ID)
    operation = CleaningOperation(
        operation_id="clean-op:bad-constant",
        operation_type="fill_missing",
        target_columns=("value",),
        strategy="constant",
        fill_value="not-a-number",
        reason="尝试向数值列写入字符串。",
        postconditions=(
            _expectation(
                "rule:value-present",
                "missing_count",
                columns=("value",),
                expected=0,
            ),
        ),
    )

    with pytest.raises(ValueError, match="填充值与目标列类型不兼容"):
        validate_cleaning_plan_scope(_plan(profile, (operation,)), profile)


def test_source_modification_blocks_before_cleaned_publish(tmp_path: Path):
    """画像后修改源文件会形成 source_modified 失败且不发布 cleaned。"""
    source = tmp_path / "input.csv"
    source.write_text("id,value\n1,a\n", encoding="utf-8")
    (profile,) = inspect_data_profiles(tmp_path, OUTLINE_ID)
    plan = _plan(profile, no_op_reason="无需清洗。")
    source.write_text("id,value\n1,changed\n", encoding="utf-8")

    with pytest.raises(TableCleaningError) as exc_info:
        execute_cleaning_plan(tmp_path, profile, plan)

    assert exc_info.value.failure.rule_id == "source_modified"
    assert exc_info.value.failure.repairable is False
    assert not (tmp_path / "cleaned").exists()


def test_source_modification_during_publish_removes_cleaned_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """任一源文件在发布窗口漂移时，当前 cleaned 产物必须被移除。"""
    first = tmp_path / "a.csv"
    second = tmp_path / "b.csv"
    first.write_text("id,value\n1,a\n", encoding="utf-8")
    second.write_text("id,value\n2,b\n", encoding="utf-8")
    profiles = inspect_data_profiles(tmp_path, OUTLINE_ID)
    profile = profiles[0]
    plan = _plan(profile, no_op_reason="无需清洗。")

    from app.data import cleaning as cleaning_module

    original_write = cleaning_module.write_utf8_atomic

    def write_then_modify_source(work_dir, relative_path, content):
        target = original_write(work_dir, relative_path, content)
        second.write_text("id,value\n2,changed\n", encoding="utf-8")
        return target

    monkeypatch.setattr(
        cleaning_module,
        "write_utf8_atomic",
        write_then_modify_source,
    )

    with pytest.raises(TableCleaningError) as exc_info:
        execute_cleaning_plan(
            tmp_path,
            profile,
            plan,
            source_profiles=profiles,
        )

    assert exc_info.value.failure.rule_id == "source_modified"
    assert not (tmp_path / cleaned_table_path(profile.table_id)).exists()


def test_verifier_rejects_forged_cleaned_metadata(tmp_path: Path):
    """实际文件正确时也不能接受伪造的行数或类型元数据。"""
    (tmp_path / "input.csv").write_text(
        "id,value\n1,10\n2,20\n",
        encoding="utf-8",
    )
    (profile,) = inspect_data_profiles(tmp_path, OUTLINE_ID)
    plan = _plan(profile, no_op_reason="无需清洗。")
    cleaned = execute_cleaning_plan(tmp_path, profile, plan)
    forged = CleanedTable(
        table_id=cleaned.table_id,
        data_profile_id=cleaned.data_profile_id,
        cleaning_plan_id=cleaned.cleaning_plan_id,
        relative_path=cleaned.relative_path,
        sha256=cleaned.sha256,
        row_count=999,
        canonical_types=(("id", "string"), ("value", "string")),
    )

    failures = verify_cleaned_table(tmp_path, profile, plan, forged)

    assert {failure.rule_id for failure in failures} >= {
        "verify:canonical-type-metadata",
        "verify:row-count-metadata",
    }
    assert all(not failure.repairable for failure in failures)


@pytest.mark.asyncio
async def test_planner_rejects_cross_table_column_and_does_not_receive_work_dir(
    tmp_path: Path,
):
    """Modeler 只能引用各自画像字段，提示中不包含调用方工作目录。"""
    (tmp_path / "a.csv").write_text("id,value\n1,10\n", encoding="utf-8")
    (tmp_path / "b.csv").write_text("other,label\n1,x\n", encoding="utf-8")
    profiles = inspect_data_profiles(tmp_path, OUTLINE_ID)
    response = {
        "plans": [
            {
                "table_id": profiles[0].table_id,
                "operations": [
                    {
                        "operation_id": "clean-op:cross-table",
                        "operation_type": "cast_type",
                        "target_columns": ["other"],
                        "target_type": "integer",
                        "strategy": "strict",
                        "reason": "非法引用另一张表字段。",
                        "postconditions": [
                            {
                                "rule_id": "rule:other-type",
                                "rule_type": "canonical_type",
                                "columns": ["other"],
                                "operator": "eq",
                                "expected": "integer",
                            }
                        ],
                    }
                ],
                "no_op_reason": None,
            },
            {
                "table_id": profiles[1].table_id,
                "operations": [],
                "no_op_reason": "无需清洗。",
            },
        ]
    }
    client = FakeLLMClient([json.dumps(response, ensure_ascii=False)])

    with pytest.raises(CleaningPlanResponseError, match="契约外字段"):
        await CleaningPlannerAgent(
            client,
            max_json_repair_attempts=0,
        ).plan(_outline(), profiles)

    assert str(tmp_path) not in str(client.messages)


@pytest.mark.asyncio
async def test_planner_json_repair_is_bounded_and_stateless(tmp_path: Path):
    """格式修复只重用可信输入和错误摘要，不回灌无效模型正文。"""
    (tmp_path / "input.csv").write_text("id,value\n1,10\n", encoding="utf-8")
    profiles = inspect_data_profiles(tmp_path, OUTLINE_ID)
    valid = {
        "plans": [
            {
                "table_id": profiles[0].table_id,
                "operations": [],
                "no_op_reason": "画像满足约束。",
            }
        ]
    }
    invalid = "```python\nopen('/tmp/outside', 'w')\n```"
    client = FakeLLMClient([invalid, json.dumps(valid, ensure_ascii=False)])

    plans = await CleaningPlannerAgent(
        client,
        max_json_repair_attempts=1,
    ).plan(_outline(), profiles)

    assert len(plans) == 1
    assert len(client.messages) == 2
    assert len(client.messages[1]) == 3
    assert "json_invalid" in client.messages[1][-1]["content"]
    assert invalid not in str(client.messages[1])


@pytest.mark.asyncio
async def test_repair_rejects_other_table_plan(tmp_path: Path):
    """局部 Repair 不能通过返回另一张表的计划扩大授权范围。"""
    (tmp_path / "a.csv").write_text("id,value\n1,10\n", encoding="utf-8")
    (tmp_path / "b.csv").write_text("id,label\n1,x\n", encoding="utf-8")
    first, second = inspect_data_profiles(tmp_path, OUTLINE_ID)
    current = _plan(first, no_op_reason="初始计划。")
    issue = DataIssue(
        schema_version="m1.5",
        artifact_id="data-issue:repair-scope",
        source_artifact_ids=(current.artifact_id,),
        validation_status="validated",
        artifact_path="m15/issues/repair-scope.json",
        issue_id="data-issue:repair-scope",
        table_id=first.table_id,
        rule_id="rule:missing",
        expected=0,
        actual=1,
        evidence_summary="键列仍有一个缺失值。",
        repair_attempt=0,
        status="unresolved",
    )
    client = FakeLLMClient(
        [
            json.dumps(
                {
                    "table_id": second.table_id,
                    "operations": [],
                    "no_op_reason": "越权改写另一张表。",
                },
                ensure_ascii=False,
            )
        ]
    )

    with pytest.raises(CleaningPlanResponseError, match="未授权 table_id"):
        await CleaningPlannerAgent(
            client,
            max_json_repair_attempts=0,
        ).repair(first, current, (issue,), repair_attempt=1)


def test_verifier_rechecks_cross_table_relation_values(tmp_path: Path):
    """关联目标 cleaned 值漂移后，即使更新文件指纹也不能通过关联验证。"""
    (tmp_path / "customers.csv").write_text(
        "customer_id,name\n1,A\n2,B\n",
        encoding="utf-8",
    )
    (tmp_path / "orders.csv").write_text(
        "order_id,customer_id\n10,1\n11,2\n",
        encoding="utf-8",
    )
    profiles = inspect_data_profiles(tmp_path, OUTLINE_ID)
    plans = tuple(_plan(profile, no_op_reason="画像满足约束。") for profile in profiles)
    cleaned = tuple(
        execute_cleaning_plan(
            tmp_path,
            profile,
            plan,
            source_profiles=profiles,
        )
        for profile, plan in zip(profiles, plans, strict=True)
    )
    profiles_by_table = {profile.table_id: profile for profile in profiles}
    cleaned_by_table = {table.table_id: table for table in cleaned}
    customers = profiles_by_table["customers.csv"]
    customer_plan = plans[0]

    assert (
        verify_cleaned_table(
            tmp_path,
            customers,
            customer_plan,
            cleaned_by_table["customers.csv"],
            profiles_by_table=profiles_by_table,
            cleaned_by_table=cleaned_by_table,
        )
        == ()
    )

    orders = cleaned_by_table["orders.csv"]
    orders_path = tmp_path / orders.relative_path
    orders_path.write_text(
        "order_id,customer_id\n10,90\n11,91\n",
        encoding="utf-8",
    )
    cleaned_by_table["orders.csv"] = replace(
        orders,
        sha256=compute_file_sha256(orders_path),
    )

    failures = verify_cleaned_table(
        tmp_path,
        customers,
        customer_plan,
        cleaned_by_table["customers.csv"],
        profiles_by_table=profiles_by_table,
        cleaned_by_table=cleaned_by_table,
    )

    assert any(
        failure.rule_id.startswith("rule:verify-join-compatible")
        for failure in failures
    )
