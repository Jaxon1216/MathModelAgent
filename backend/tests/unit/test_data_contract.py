"""M1.5 DataContract 冻结与持续完整性测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.data import (
    DataContractFreezeError,
    DataContractIntegrityError,
    M15ArtifactStore,
    build_task_facts,
    compute_file_sha256,
    execute_cleaning_plan,
    freeze_data_contract,
    inspect_data_catalog,
    inspect_data_profiles,
    validate_data_contract_integrity,
)
from app.domain.m15 import (
    CleaningOperation,
    CleaningPlan,
    DataContract,
    DataIssue,
    ValidationExpectation,
)

OUTLINE_ID = "task-outline:contract-test"


def _task_facts(tmp_path: Path):
    """从真实文件发现结果构造并持久化 TaskFacts。"""
    facts = build_task_facts(
        task_id="contract-test",
        problem_text="分析输入数据并填写结果模板。",
        work_dir=tmp_path,
        data_catalog=inspect_data_catalog(tmp_path),
    )
    M15ArtifactStore(tmp_path).write_json(facts)
    return facts


def _plan(profile, *, declared_key: bool = False) -> CleaningPlan:
    """为画像构造 no-op 或带声明键验证的计划。"""
    digest = profile.artifact_id.removeprefix("data-profile:")
    operations = ()
    no_op_reason = "画像已满足全部约束。"
    if declared_key:
        operations = (
            CleaningOperation(
                operation_id=f"clean-op:{digest}",
                operation_type="drop_duplicates",
                target_columns=("id",),
                keep="first",
                reason="按已验证业务标识保持唯一记录。",
                postconditions=(
                    ValidationExpectation(
                        rule_id=f"rule:key-{digest}",
                        rule_type="key_unique",
                        columns=("id",),
                        operator="eq",
                        expected=True,
                    ),
                ),
            ),
        )
        no_op_reason = None
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


def _prepare_cleaned(
    tmp_path: Path,
    *,
    declared_key: bool = False,
):
    """画像、持久化计划并执行全部 cleaned 表。"""
    facts = _task_facts(tmp_path)
    profiles = inspect_data_profiles(tmp_path, OUTLINE_ID)
    plans = tuple(_plan(profile, declared_key=declared_key) for profile in profiles)
    store = M15ArtifactStore(tmp_path)
    for plan in plans:
        store.write_json(plan)
    cleaned = tuple(
        execute_cleaning_plan(
            tmp_path,
            profile,
            plan,
            source_profiles=profiles,
        )
        for profile, plan in zip(profiles, plans, strict=True)
    )
    return facts, profiles, plans, cleaned


def test_freeze_contract_records_lineage_schema_keys_and_relations(
    tmp_path: Path,
):
    """冻结契约完整记录来源、schema、候选/声明键和已验证关联。"""
    (tmp_path / "left.csv").write_text(
        "id,value\n1,10\n2,20\n",
        encoding="utf-8",
    )
    (tmp_path / "right.csv").write_text(
        "id,label\n1,A\n2,B\n",
        encoding="utf-8",
    )
    (tmp_path / "result.csv").write_text(
        "id,prediction\n",
        encoding="utf-8",
    )
    facts, profiles, plans, cleaned = _prepare_cleaned(
        tmp_path,
        declared_key=True,
    )

    contract = freeze_data_contract(
        tmp_path,
        task_facts=facts,
        profiles=profiles,
        plans=plans,
        cleaned_tables=cleaned,
    )

    assert contract.status == "frozen"
    assert contract.validation_status == "validated"
    assert contract.scanned_attachments == ("left.csv", "right.csv")
    assert contract.output_templates == ("result.csv",)
    assert {table.table_id for table in contract.tables} == {
        "left.csv",
        "right.csv",
    }
    assert "result.csv" not in {table.source_path for table in contract.tables}
    for profile, plan, cleaned_table, table in zip(
        profiles,
        plans,
        cleaned,
        contract.tables,
        strict=True,
    ):
        assert table.data_profile_id == profile.artifact_id
        assert table.data_profile_path == profile.artifact_path
        assert table.cleaning_plan_id == plan.artifact_id
        assert table.cleaning_plan_path == plan.artifact_path
        assert table.source_sha256 == profile.source_sha256
        assert table.cleaned_sha256 == cleaned_table.sha256
        assert [column.name for column in table.columns] == [
            column.name for column in profile.columns
        ]
        assert all(column.statistic_definition for column in table.columns)
        assert {key.kind for key in table.keys} == {"candidate", "declared"}
        assert table.relations
        assert profile.artifact_id in contract.source_artifact_ids
        assert plan.artifact_id in contract.source_artifact_ids

    assert (
        M15ArtifactStore(tmp_path).read_json(
            "m15/data_contract.json",
            DataContract,
        )
        == contract
    )
    validate_data_contract_integrity(tmp_path, contract)


def test_freeze_no_data_contract_keeps_output_templates_out_of_tables(
    tmp_path: Path,
):
    """只有输出模板时发布显式 no-data 契约且不生成表条目。"""
    (tmp_path / "result1.xlsx").write_bytes(b"output template")
    facts = _task_facts(tmp_path)

    contract = freeze_data_contract(
        tmp_path,
        task_facts=facts,
        profiles=(),
        plans=(),
        cleaned_tables=(),
    )

    assert contract.status == "no_data"
    assert contract.tables == ()
    assert contract.scanned_attachments == ()
    assert contract.output_templates == ("result1.xlsx",)
    assert contract.source_artifact_ids == (facts.artifact_id,)
    validate_data_contract_integrity(tmp_path, contract)


@pytest.mark.parametrize(
    ("target", "action"),
    [
        ("cleaned", "modify"),
        ("cleaned", "delete"),
        ("cleaned", "replace"),
        ("source", "modify"),
    ],
)
def test_integrity_blocks_file_fingerprint_drift(
    tmp_path: Path,
    target: str,
    action: str,
):
    """来源或 cleaned 文件修改、删除、替换后不能继续交接。"""
    source = tmp_path / "input.csv"
    source.write_text("id,value\n1,10\n2,20\n", encoding="utf-8")
    facts, profiles, plans, cleaned = _prepare_cleaned(tmp_path)
    contract = freeze_data_contract(
        tmp_path,
        task_facts=facts,
        profiles=profiles,
        plans=plans,
        cleaned_tables=cleaned,
    )
    path = source if target == "source" else tmp_path / contract.tables[0].cleaned_path
    if action == "delete":
        path.unlink()
    elif action == "replace":
        path.unlink()
        path.write_text("id,value\n9,90\n", encoding="utf-8")
    else:
        path.write_text("id,value\n1,999\n2,20\n", encoding="utf-8")

    with pytest.raises(DataContractIntegrityError) as exc_info:
        validate_data_contract_integrity(tmp_path, contract)

    assert exc_info.value.issues
    assert all(issue.status == "unresolved" for issue in exc_info.value.issues)
    assert any(
        issue.rule_id
        == f"integrity:{'source' if target == 'source' else 'cleaned'}-file"
        for issue in exc_info.value.issues
    )


@pytest.mark.parametrize("mutation", ["row_count", "columns", "canonical_type"])
def test_integrity_rechecks_frozen_row_and_column_schema(
    tmp_path: Path,
    mutation: str,
):
    """即使文件指纹未变，伪造的行列 schema 也不能通过交接校验。"""
    (tmp_path / "input.csv").write_text(
        "id,value\n1,10\n2,10\n",
        encoding="utf-8",
    )
    facts, profiles, plans, cleaned = _prepare_cleaned(tmp_path)
    contract = freeze_data_contract(
        tmp_path,
        task_facts=facts,
        profiles=profiles,
        plans=plans,
        cleaned_tables=cleaned,
    )
    table = contract.tables[0]
    if mutation == "row_count":
        changed_table = table.model_copy(update={"row_count": 99})
        expected_rule = "integrity:row-count"
    elif mutation == "columns":
        changed_column = table.columns[1].model_copy(update={"name": "unknown"})
        changed_table = table.model_copy(
            update={"columns": (table.columns[0], changed_column)}
        )
        expected_rule = "integrity:columns"
    else:
        changed_column = table.columns[1].model_copy(
            update={"canonical_type": "boolean"}
        )
        changed_table = table.model_copy(
            update={"columns": (table.columns[0], changed_column)}
        )
        expected_rule = None
    changed_contract = contract.model_copy(update={"tables": (changed_table,)})
    M15ArtifactStore(tmp_path).write_json(changed_contract)

    with pytest.raises(DataContractIntegrityError) as exc_info:
        validate_data_contract_integrity(tmp_path, changed_contract)

    if expected_rule is not None:
        assert expected_rule in {issue.rule_id for issue in exc_info.value.issues}
    else:
        assert any(
            issue.rule_id.startswith("rule:integrity-type-")
            for issue in exc_info.value.issues
        )


def test_freeze_rejects_unresolved_issue(tmp_path: Path):
    """任何未解决 issue 都阻止契约冻结。"""
    (tmp_path / "input.csv").write_text(
        "id,value\n1,10\n2,20\n",
        encoding="utf-8",
    )
    facts, profiles, plans, cleaned = _prepare_cleaned(tmp_path)
    issue = DataIssue(
        schema_version="m1.5",
        artifact_id="data-issue:contract-block",
        source_artifact_ids=(
            profiles[0].artifact_id,
            plans[0].artifact_id,
        ),
        validation_status="validated",
        artifact_path="m15/issues/contract-block.json",
        issue_id="data-issue:contract-block",
        table_id=profiles[0].table_id,
        rule_id="rule:contract-block",
        expected=True,
        actual=False,
        evidence_summary="验证规则仍未通过。",
        repair_attempt=0,
        status="unresolved",
    )
    M15ArtifactStore(tmp_path).write_json(issue)

    with pytest.raises(DataContractIntegrityError) as exc_info:
        freeze_data_contract(
            tmp_path,
            task_facts=facts,
            profiles=profiles,
            plans=plans,
            cleaned_tables=cleaned,
            issues=(issue,),
        )

    assert exc_info.value.issues == (issue,)
    assert not (tmp_path / "m15" / "data_contract.json").exists()


def test_freeze_rejects_resolved_issue_without_unresolved_lineage(tmp_path: Path):
    """同表同规则的伪造 resolved issue 不能掩盖真实 unresolved issue。"""
    (tmp_path / "input.csv").write_text(
        "id,value\n1,10\n2,20\n",
        encoding="utf-8",
    )
    facts, profiles, plans, cleaned = _prepare_cleaned(tmp_path)
    unresolved = DataIssue(
        schema_version="m1.5",
        artifact_id="data-issue:real-unresolved",
        source_artifact_ids=(
            profiles[0].artifact_id,
            plans[0].artifact_id,
        ),
        validation_status="validated",
        artifact_path="m15/issues/real-unresolved.json",
        issue_id="data-issue:real-unresolved",
        table_id=profiles[0].table_id,
        rule_id="rule:lineage-required",
        expected=True,
        actual=False,
        evidence_summary="真实验证失败。",
        repair_attempt=0,
        status="unresolved",
    )
    forged_resolved = DataIssue(
        schema_version="m1.5",
        artifact_id="data-issue:forged-resolved",
        source_artifact_ids=(
            profiles[0].artifact_id,
            plans[0].artifact_id,
        ),
        validation_status="validated",
        artifact_path="m15/issues/forged-resolved.json",
        issue_id="data-issue:forged-resolved",
        table_id=profiles[0].table_id,
        rule_id=unresolved.rule_id,
        expected=unresolved.expected,
        actual="passed",
        evidence_summary="未引用真实失败却声称已经解决。",
        repair_attempt=1,
        status="resolved",
    )
    store = M15ArtifactStore(tmp_path)
    store.write_json(unresolved)
    store.write_json(forged_resolved)

    with pytest.raises(DataContractIntegrityError) as exc_info:
        freeze_data_contract(
            tmp_path,
            task_facts=facts,
            profiles=profiles,
            plans=plans,
            cleaned_tables=cleaned,
            issues=(unresolved, forged_resolved),
        )

    assert unresolved in exc_info.value.issues
    assert not (tmp_path / "m15" / "data_contract.json").exists()


def test_freeze_accepts_resolved_issue_with_direct_unresolved_lineage(
    tmp_path: Path,
):
    """后续 Repair 的 resolved 直接引用真实失败时可以解除阻断。"""
    (tmp_path / "input.csv").write_text(
        "id,value\n1,10\n2,20\n",
        encoding="utf-8",
    )
    facts, profiles, plans, _ = _prepare_cleaned(tmp_path)
    profile = profiles[0]
    initial_plan = plans[0]
    unresolved = DataIssue(
        schema_version="m1.5",
        artifact_id="data-issue:direct-unresolved",
        source_artifact_ids=(profile.artifact_id, initial_plan.artifact_id),
        validation_status="validated",
        artifact_path="m15/issues/direct-unresolved.json",
        issue_id="data-issue:direct-unresolved",
        table_id=profile.table_id,
        rule_id="rule:direct-lineage",
        expected=True,
        actual=False,
        evidence_summary="初始计划验证失败。",
        repair_attempt=0,
        status="unresolved",
    )
    repaired_plan = CleaningPlan(
        schema_version="m1.5",
        artifact_id=f"{initial_plan.artifact_id}-repair1",
        source_artifact_ids=(
            OUTLINE_ID,
            profile.artifact_id,
            initial_plan.artifact_id,
            unresolved.issue_id,
        ),
        validation_status="validated",
        artifact_path=initial_plan.artifact_path.replace(".json", ".repair1.json"),
        task_outline_id=OUTLINE_ID,
        data_profile_id=profile.artifact_id,
        table_id=profile.table_id,
        operations=(),
        no_op_reason="修复后数据无需额外转换。",
        repair_attempt=1,
        repaired_issue_ids=(unresolved.issue_id,),
    )
    resolved = DataIssue(
        schema_version="m1.5",
        artifact_id="data-issue:direct-resolved",
        source_artifact_ids=(
            profile.artifact_id,
            repaired_plan.artifact_id,
            unresolved.issue_id,
        ),
        validation_status="validated",
        artifact_path="m15/issues/direct-resolved.json",
        issue_id="data-issue:direct-resolved",
        table_id=profile.table_id,
        rule_id=unresolved.rule_id,
        expected=unresolved.expected,
        actual="passed",
        evidence_summary="后续 Repair 已通过同一规则。",
        repair_attempt=1,
        status="resolved",
    )
    store = M15ArtifactStore(tmp_path)
    store.write_json(unresolved)
    store.write_json(repaired_plan)
    store.write_json(resolved)
    cleaned = execute_cleaning_plan(
        tmp_path,
        profile,
        repaired_plan,
        source_profiles=profiles,
    )

    contract = freeze_data_contract(
        tmp_path,
        task_facts=facts,
        profiles=profiles,
        plans=(repaired_plan,),
        cleaned_tables=(cleaned,),
        issues=(unresolved, resolved),
    )

    assert contract.resolved_issue_ids == (resolved.issue_id,)
    validate_data_contract_integrity(tmp_path, contract)


@pytest.mark.parametrize("semantic", ["keys", "relations"])
def test_integrity_rebuilds_schema_valid_contract_semantics(
    tmp_path: Path,
    semantic: str,
):
    """重载对象即使 schema 合法，也必须与可信来源重建语义一致。"""
    (tmp_path / "left.csv").write_text(
        "id,value\n1,10\n2,20\n",
        encoding="utf-8",
    )
    (tmp_path / "right.csv").write_text(
        "id,label\n1,A\n2,B\n",
        encoding="utf-8",
    )
    facts, profiles, plans, cleaned = _prepare_cleaned(
        tmp_path,
        declared_key=True,
    )
    contract = freeze_data_contract(
        tmp_path,
        task_facts=facts,
        profiles=profiles,
        plans=plans,
        cleaned_tables=cleaned,
    )
    table = contract.tables[0]
    assert table.keys
    assert table.relations
    changed_table = table.model_copy(update={semantic: ()})
    replaced = contract.model_copy(
        update={"tables": (changed_table, *contract.tables[1:])}
    )
    store = M15ArtifactStore(tmp_path)
    store.write_json(replaced)
    reloaded = store.read_json(replaced.artifact_path, DataContract)

    with pytest.raises(DataContractIntegrityError) as exc_info:
        validate_data_contract_integrity(tmp_path, reloaded)

    assert "integrity:contract-table-semantics" in {
        issue.rule_id for issue in exc_info.value.issues
    }


def test_integrity_reverifies_replaced_cleaned_content_with_updated_hash(
    tmp_path: Path,
):
    """同步篡改 cleaned 内容和契约指纹也不能绕过关联语义复验。"""
    (tmp_path / "left.csv").write_text(
        "id,value\n1,10\n2,20\n",
        encoding="utf-8",
    )
    (tmp_path / "right.csv").write_text(
        "id,label\n1,A\n2,B\n",
        encoding="utf-8",
    )
    facts, profiles, plans, cleaned = _prepare_cleaned(tmp_path)
    contract = freeze_data_contract(
        tmp_path,
        task_facts=facts,
        profiles=profiles,
        plans=plans,
        cleaned_tables=cleaned,
    )
    right_table = contract.tables[1]
    cleaned_path = tmp_path / right_table.cleaned_path
    cleaned_path.write_text(
        "id,label\n8,A\n9,B\n",
        encoding="utf-8",
    )
    replaced_table = right_table.model_copy(
        update={"cleaned_sha256": compute_file_sha256(cleaned_path)}
    )
    replaced = contract.model_copy(
        update={"tables": (contract.tables[0], replaced_table)}
    )
    store = M15ArtifactStore(tmp_path)
    store.write_json(replaced)
    reloaded = store.read_json(replaced.artifact_path, DataContract)

    with pytest.raises(DataContractIntegrityError) as exc_info:
        validate_data_contract_integrity(tmp_path, reloaded)

    assert any(
        "verify-join-compatible" in issue.rule_id for issue in exc_info.value.issues
    )


def test_failed_table_is_not_published_in_partial_contract(tmp_path: Path):
    """任一表复验失败时整体阻断，不发布仅含成功表的契约。"""
    (tmp_path / "good.csv").write_text(
        "id,value\n1,10\n2,20\n",
        encoding="utf-8",
    )
    (tmp_path / "failed.csv").write_text(
        "id,value\n1,30\n2,40\n",
        encoding="utf-8",
    )
    facts, profiles, plans, cleaned = _prepare_cleaned(tmp_path)
    failed_path = tmp_path / cleaned[1].relative_path
    failed_path.write_text("id,value\n1,changed\n", encoding="utf-8")

    with pytest.raises(DataContractIntegrityError):
        freeze_data_contract(
            tmp_path,
            task_facts=facts,
            profiles=profiles,
            plans=plans,
            cleaned_tables=cleaned,
        )

    assert not (tmp_path / "m15" / "data_contract.json").exists()


def test_freeze_requires_complete_persisted_sources(tmp_path: Path):
    """缺少任一最终 CleaningPlan 时不能静默排除该表。"""
    (tmp_path / "first.csv").write_text("id\n1\n", encoding="utf-8")
    (tmp_path / "second.csv").write_text("id\n2\n", encoding="utf-8")
    facts, profiles, plans, cleaned = _prepare_cleaned(tmp_path)

    with pytest.raises(DataContractFreezeError, match="严格覆盖"):
        freeze_data_contract(
            tmp_path,
            task_facts=facts,
            profiles=profiles,
            plans=plans[:1],
            cleaned_tables=cleaned,
        )
