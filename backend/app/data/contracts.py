"""冻结 M1.5 DataContract 并持续复核数据完整性。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import ValidationError

from app.data.artifact_store import (
    ArtifactStorageError,
    M15ArtifactStore,
    resolve_work_dir_path,
)
from app.data.cleaning import (
    CleanedTable,
    VerificationFailure,
    cast_series_to_canonical,
    cleaned_table_path,
    expected_canonical_types,
    validation_rule_id,
    verify_cleaned_table,
)
from app.data.preparation import compute_file_sha256
from app.domain.m15 import (
    CleaningPlan,
    ContractColumn,
    ContractKey,
    ContractRelation,
    ContractTable,
    DataContract,
    DataIssue,
    DataProfile,
    TaskFacts,
    ValidationExpectation,
    VersionedArtifact,
)

DATA_CONTRACT_PATH = "m15/data_contract.json"
_STATISTIC_DEFINITION = (
    "row_count 按 cleaned CSV 数据行统计；nullable 按 CSV 缺失值重载结果统计。"
)


class DataContractFreezeError(RuntimeError):
    """输入集合不完整或不一致，不能发布部分 DataContract。"""


class DataContractIntegrityError(RuntimeError):
    """冻结契约、cleaned 文件或来源文件不再满足完整性要求。"""

    def __init__(self, issues: tuple[DataIssue, ...]) -> None:
        self.issues = issues
        detail = f"{issues[0].table_id}/{issues[0].rule_id}" if issues else "unknown"
        super().__init__(f"DataContract 完整性校验失败: {detail}")


def freeze_data_contract(
    work_dir: str | Path,
    *,
    task_facts: TaskFacts,
    profiles: tuple[DataProfile, ...],
    plans: tuple[CleaningPlan, ...],
    cleaned_tables: tuple[CleanedTable, ...],
    issues: tuple[DataIssue, ...] = (),
    artifact_store: M15ArtifactStore | None = None,
) -> DataContract:
    """从完整、已验证且已持久化的数据阶段产物冻结单一契约。

    Args:
        work_dir: 当前任务工作目录。
        task_facts: 已落盘的附件与输出模板事实。
        profiles: 全部输入表的已验证画像。
        plans: 每张画像表最终采用的已验证清洗计划。
        cleaned_tables: 每张表经过确定性验证的 cleaned 元数据。
        issues: 清洗闭环产生的全部 issue 历史。
        artifact_store: 可选的同工作目录产物存储。

    Returns:
        已原子持久化并再次通过完整性校验的冻结契约。

    Raises:
        DataContractFreezeError: 来源产物、覆盖关系或 issue 终态不完整。
        DataContractIntegrityError: 文件或确定性验证结果发生漂移。
    """
    store = artifact_store or M15ArtifactStore(work_dir)
    _require_persisted_artifact(store, task_facts, TaskFacts)

    persisted_issues = _collect_issues(work_dir, store, issues)
    blocking_issues = _blocking_issues(persisted_issues)
    if blocking_issues:
        raise DataContractIntegrityError(blocking_issues)

    attachment_by_path = {item.path: item for item in task_facts.attachments}
    template_paths = tuple(item.path for item in task_facts.output_templates)
    if set(attachment_by_path) & set(template_paths):
        raise DataContractFreezeError("输入附件与输出模板身份重叠")

    if not profiles:
        if plans or cleaned_tables:
            raise DataContractFreezeError(
                "no-data 契约不能包含 CleaningPlan 或 cleaned 表"
            )
        if attachment_by_path:
            raise DataContractFreezeError("存在输入附件时不能冻结 no-data 空契约")
        contract = _build_contract(
            task_facts=task_facts,
            tables=(),
            issues=persisted_issues,
            status="no_data",
        )
        store.write_json(contract)
        validate_data_contract_integrity(
            work_dir,
            contract,
            issues=persisted_issues,
            artifact_store=store,
        )
        return contract

    profile_by_table = _unique_by_table(profiles, "DataProfile")
    plan_by_table = _unique_by_table(plans, "CleaningPlan")
    cleaned_by_table = _unique_by_table(cleaned_tables, "cleaned")
    table_ids = set(profile_by_table)
    if set(plan_by_table) != table_ids or set(cleaned_by_table) != table_ids:
        raise DataContractFreezeError(
            "DataProfile、CleaningPlan 与 cleaned 表必须严格覆盖同一表集合"
        )

    profiled_paths = {profile.source_path for profile in profiles}
    if profiled_paths != set(attachment_by_path):
        raise DataContractFreezeError(
            "DataProfile 必须覆盖全部且仅覆盖 TaskFacts 输入附件"
        )
    if profiled_paths & set(template_paths):
        raise DataContractFreezeError("输出模板不能生成 DataProfile 或契约表")
    outline_ids = {profile.task_outline_id for profile in profiles}
    if len(outline_ids) != 1:
        raise DataContractFreezeError("全部 DataProfile 必须属于同一 TaskOutline")

    for profile in profiles:
        _require_persisted_artifact(store, profile, DataProfile)
        plan = plan_by_table[profile.table_id]
        _require_persisted_artifact(store, plan, CleaningPlan)
        if profile.source_sha256 != attachment_by_path[profile.source_path].sha256:
            raise DataContractFreezeError(
                f"DataProfile 来源指纹与 TaskFacts 不一致: {profile.table_id}"
            )
        if (
            plan.validation_status != "validated"
            or plan.data_profile_id != profile.artifact_id
            or plan.task_outline_id != profile.task_outline_id
        ):
            raise DataContractFreezeError(
                f"CleaningPlan 来源或状态无效: {profile.table_id}"
            )

    failures: list[VerificationFailure] = []
    for profile in profiles:
        failures.extend(
            verify_cleaned_table(
                work_dir,
                profile,
                plan_by_table[profile.table_id],
                cleaned_by_table[profile.table_id],
                profiles_by_table=profile_by_table,
                cleaned_by_table=cleaned_by_table,
            )
        )
    if failures:
        generated = _persist_verification_failures(
            store,
            failures,
            profile_by_table,
            plan_by_table,
            phase="freeze",
        )
        raise DataContractIntegrityError(generated)

    tables = tuple(
        _build_contract_table(
            work_dir,
            profile,
            plan_by_table[profile.table_id],
            cleaned_by_table[profile.table_id],
        )
        for profile in profiles
    )
    contract = _build_contract(
        task_facts=task_facts,
        tables=tables,
        issues=persisted_issues,
        status="frozen",
        profiles=profiles,
        plans=tuple(plan_by_table[profile.table_id] for profile in profiles),
    )
    store.write_json(contract)
    validate_data_contract_integrity(
        work_dir,
        contract,
        issues=persisted_issues,
        artifact_store=store,
    )
    return contract


def validate_data_contract_integrity(
    work_dir: str | Path,
    contract: DataContract,
    *,
    issues: tuple[DataIssue, ...] = (),
    artifact_store: M15ArtifactStore | None = None,
) -> None:
    """在 Modeler 或旧 Coder 交接前统一复核冻结契约及文件。

    Args:
        work_dir: 当前任务工作目录。
        contract: 待交接的冻结或 no-data 契约。
        issues: 调用方已持有的 issue 历史；同时会扫描已落盘 issue。
        artifact_store: 可选的同工作目录产物存储。

    Raises:
        DataContractIntegrityError: 状态、路径、指纹或行列 schema 不一致。
    """
    store = artifact_store or M15ArtifactStore(work_dir)
    failures: list[VerificationFailure] = []

    try:
        validated = DataContract.model_validate_json(contract.model_dump_json())
        persisted = store.read_json(contract.artifact_path, DataContract)
        if persisted != validated:
            failures.append(
                _integrity_failure(
                    "data-contract",
                    "integrity:contract-replaced",
                    validated.model_dump(mode="json"),
                    persisted.model_dump(mode="json"),
                    "DataContract 文件与交接对象不一致。",
                )
            )
    except (ArtifactStorageError, ValidationError, ValueError) as exc:
        failures.append(
            _integrity_failure(
                "data-contract",
                "integrity:contract-status-path",
                {
                    "status": "frozen or no_data",
                    "validation_status": "validated",
                    "path": DATA_CONTRACT_PATH,
                },
                f"{type(exc).__name__}: {exc}",
                "DataContract 状态、schema 或持久化路径无效。",
            )
        )

    if (
        contract.validation_status != "validated"
        or contract.status not in {"frozen", "no_data"}
        or contract.artifact_path != DATA_CONTRACT_PATH
    ):
        failures.append(
            _integrity_failure(
                "data-contract",
                "integrity:contract-status-path",
                {
                    "status": "frozen or no_data",
                    "validation_status": "validated",
                    "path": DATA_CONTRACT_PATH,
                },
                {
                    "status": contract.status,
                    "validation_status": contract.validation_status,
                    "path": contract.artifact_path,
                },
                "DataContract 不是可供下游消费的冻结状态。",
            )
        )

    try:
        persisted_issues = _collect_issues(work_dir, store, issues)
    except DataContractFreezeError as exc:
        failures.append(
            _integrity_failure(
                "data-contract",
                "integrity:issue-artifact",
                "all DataIssue artifacts readable and validated",
                str(exc),
                "DataIssue 历史无法完整复验。",
            )
        )
        persisted_issues = ()
    blocking_issues = _blocking_issues(persisted_issues)
    if blocking_issues:
        raise DataContractIntegrityError(blocking_issues)

    if contract.status == "no_data":
        if failures:
            raise DataContractIntegrityError(
                _persist_integrity_failures(store, contract, failures)
            )
        return

    for table in contract.tables:
        failures.extend(_validate_contract_table(work_dir, store, table))
    failures.extend(
        _validate_rebuilt_contract_semantics(
            work_dir,
            store,
            contract,
        )
    )

    if failures:
        raise DataContractIntegrityError(
            _persist_integrity_failures(store, contract, failures)
        )


def _require_persisted_artifact(
    store: M15ArtifactStore,
    artifact: VersionedArtifact,
    artifact_type: type[VersionedArtifact],
) -> None:
    """要求冻结来源已经以同一严格 schema 原子落盘。"""
    if artifact.validation_status != "validated":
        raise DataContractFreezeError(f"来源产物未通过校验: {artifact.artifact_id}")
    try:
        persisted = store.read_json(artifact.artifact_path, artifact_type)
    except ArtifactStorageError as exc:
        raise DataContractFreezeError(str(exc)) from exc
    if persisted != artifact:
        raise DataContractFreezeError(
            f"来源产物与已落盘内容不一致: {artifact.artifact_id}"
        )


def _unique_by_table(
    artifacts: Iterable[DataProfile | CleaningPlan | CleanedTable],
    label: str,
) -> dict[str, Any]:
    """按 table_id 建索引并拒绝静默覆盖。"""
    result: dict[str, Any] = {}
    for artifact in artifacts:
        if artifact.table_id in result:
            raise DataContractFreezeError(f"{label}.table_id 不能重复")
        result[artifact.table_id] = artifact
    return result


def _collect_issues(
    work_dir: str | Path,
    store: M15ArtifactStore,
    supplied: tuple[DataIssue, ...],
) -> tuple[DataIssue, ...]:
    """合并调用方与磁盘 issue，防止遗漏未解决证据。"""
    by_id: dict[str, DataIssue] = {}
    for issue in supplied:
        _require_persisted_artifact(store, issue, DataIssue)
        by_id[issue.issue_id] = issue

    issue_dir = Path(work_dir) / "m15" / "issues"
    if not issue_dir.exists():
        return tuple(by_id.values())
    if issue_dir.is_symlink() or not issue_dir.is_dir():
        raise DataContractFreezeError("DataIssue 目录不是安全的普通目录")
    for path in sorted(issue_dir.glob("*.json")):
        relative_path = path.relative_to(Path(work_dir)).as_posix()
        try:
            issue = store.read_json(relative_path, DataIssue)
        except ArtifactStorageError as exc:
            raise DataContractFreezeError(str(exc)) from exc
        existing = by_id.get(issue.issue_id)
        if existing is not None and existing != issue:
            raise DataContractFreezeError(f"DataIssue 内容冲突: {issue.issue_id}")
        by_id[issue.issue_id] = issue
    return tuple(sorted(by_id.values(), key=lambda item: item.issue_id))


def _blocking_issues(issues: tuple[DataIssue, ...]) -> tuple[DataIssue, ...]:
    """仅允许有明确失败 lineage 的 resolved 关闭对应规则。"""
    latest = _latest_failed_issues(issues)
    valid_resolutions = _valid_issue_resolutions(issues, latest)
    return tuple(
        issue
        for key, issue in sorted(latest.items())
        if issue.status == "exhausted" or key not in valid_resolutions
    )


def _latest_failed_issues(
    issues: tuple[DataIssue, ...],
) -> dict[tuple[str, str], DataIssue]:
    """按表和规则选取最新的真实失败，不让 resolved 参与排序覆盖。"""
    latest: dict[tuple[str, str], DataIssue] = {}
    status_rank = {"unresolved": 0, "exhausted": 1}
    for issue in issues:
        if issue.status == "resolved":
            continue
        key = (issue.table_id, issue.rule_id)
        current = latest.get(key)
        if current is None or (
            issue.repair_attempt,
            status_rank[issue.status],
            issue.issue_id,
        ) > (
            current.repair_attempt,
            status_rank[current.status],
            current.issue_id,
        ):
            latest[key] = issue
    return latest


def _valid_issue_resolutions(
    issues: tuple[DataIssue, ...],
    latest_failures: dict[tuple[str, str], DataIssue],
) -> dict[tuple[str, str], DataIssue]:
    """找出直接引用最新 unresolved 且发生在后续 Repair 的 resolved 证据。"""
    resolutions: dict[tuple[str, str], DataIssue] = {}
    for issue in issues:
        if issue.status != "resolved":
            continue
        key = (issue.table_id, issue.rule_id)
        failed = latest_failures.get(key)
        if (
            failed is None
            or failed.status != "unresolved"
            or failed.issue_id not in issue.source_artifact_ids
            or issue.repair_attempt <= failed.repair_attempt
        ):
            continue
        current = resolutions.get(key)
        if current is None or (
            issue.repair_attempt,
            issue.issue_id,
        ) > (
            current.repair_attempt,
            current.issue_id,
        ):
            resolutions[key] = issue
    return resolutions


def _build_contract(
    *,
    task_facts: TaskFacts,
    tables: tuple[ContractTable, ...],
    issues: tuple[DataIssue, ...],
    status: str,
    profiles: tuple[DataProfile, ...] = (),
    plans: tuple[CleaningPlan, ...] = (),
) -> DataContract:
    """从已验证事实构造稳定标识的 DataContract。"""
    latest_failures = _latest_failed_issues(issues)
    valid_resolutions = _valid_issue_resolutions(issues, latest_failures)
    resolved_issue_ids = tuple(
        issue.issue_id for _, issue in sorted(valid_resolutions.items())
    )
    source_ids = _ordered_unique(
        (
            task_facts.artifact_id,
            *(profile.task_outline_id for profile in profiles),
            *(profile.artifact_id for profile in profiles),
            *(plan.artifact_id for plan in plans),
            *(issue.issue_id for issue in issues),
        )
    )
    identity = json.dumps(
        {
            "status": status,
            "sources": source_ids,
            "tables": [
                {
                    "table_id": table.table_id,
                    "source_sha256": table.source_sha256,
                    "cleaned_sha256": table.cleaned_sha256,
                }
                for table in tables
            ],
            "attachments": [item.path for item in task_facts.attachments],
            "output_templates": [item.path for item in task_facts.output_templates],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:16]
    return DataContract.model_validate(
        {
            "schema_version": "m1.5",
            "artifact_id": f"data-contract:{digest}",
            "source_artifact_ids": source_ids,
            "validation_status": "validated",
            "artifact_path": DATA_CONTRACT_PATH,
            "status": status,
            "tables": tables,
            "scanned_attachments": tuple(item.path for item in task_facts.attachments),
            "output_templates": tuple(
                item.path for item in task_facts.output_templates
            ),
            "resolved_issue_ids": resolved_issue_ids,
        }
    )


def _build_contract_table(
    work_dir: str | Path,
    profile: DataProfile,
    plan: CleaningPlan,
    cleaned: CleanedTable,
) -> ContractTable:
    """只从已复验的 profile、plan 与 cleaned 文件提取契约事实。"""
    path = resolve_work_dir_path(work_dir, cleaned.relative_path, must_exist=True)
    frame = pd.read_csv(
        path,
        encoding="utf-8",
        dtype="string",
        keep_default_na=True,
    )
    types_by_column = dict(cleaned.canonical_types)
    columns = tuple(
        ContractColumn(
            name=column.name,
            source_name=column.source_name,
            canonical_type=types_by_column[column.name],
            nullable=bool(_series(frame, column.name).isna().any()),
            statistic_definition=_STATISTIC_DEFINITION,
        )
        for column in profile.columns
    )
    return ContractTable(
        table_id=profile.table_id,
        data_profile_id=profile.artifact_id,
        data_profile_path=profile.artifact_path,
        cleaning_plan_id=plan.artifact_id,
        cleaning_plan_path=plan.artifact_path,
        source_path=profile.source_path,
        source_sheet=profile.source_sheet,
        source_sha256=profile.source_sha256,
        cleaned_path=cleaned.relative_path,
        cleaned_sha256=cleaned.sha256,
        row_count=cleaned.row_count,
        columns=columns,
        keys=_build_contract_keys(profile, plan),
        relations=_build_contract_relations(profile, plan),
    )


def _build_contract_keys(
    profile: DataProfile,
    plan: CleaningPlan,
) -> tuple[ContractKey, ...]:
    """冻结画像候选键及 CleaningPlan 显式验证的键约束。"""
    keys: list[ContractKey] = []
    for candidate in profile.candidate_keys:
        keys.append(
            ContractKey(
                key_id=_stable_id(
                    "contract-key",
                    profile.table_id,
                    "candidate",
                    *candidate.columns,
                ),
                columns=candidate.columns,
                kind="candidate",
                uniqueness_verified=True,
                non_null_verified=True,
                validation_rule_ids=(
                    validation_rule_id(
                        "verify-key-unique",
                        *candidate.columns,
                    ),
                    validation_rule_id(
                        "verify-key-non-null",
                        *candidate.columns,
                    ),
                ),
            )
        )

    declared: dict[tuple[str, ...], list[ValidationExpectation]] = {}
    for operation in plan.operations:
        for condition in operation.postconditions:
            if condition.rule_type in {"key_unique", "key_non_null"}:
                declared.setdefault(condition.columns, []).append(condition)
    for columns, conditions in sorted(declared.items()):
        rule_types = {condition.rule_type for condition in conditions}
        keys.append(
            ContractKey(
                key_id=_stable_id(
                    "contract-key",
                    profile.table_id,
                    "declared",
                    *columns,
                ),
                columns=columns,
                kind="declared",
                uniqueness_verified="key_unique" in rule_types,
                non_null_verified="key_non_null" in rule_types,
                validation_rule_ids=tuple(
                    sorted(condition.rule_id for condition in conditions)
                ),
            )
        )
    return tuple(keys)


def _build_contract_relations(
    profile: DataProfile,
    plan: CleaningPlan,
) -> tuple[ContractRelation, ...]:
    """冻结画像关联和计划显式 join 后置条件。"""
    relations: list[ContractRelation] = []
    for evidence in profile.join_evidence:
        rule_id = validation_rule_id(
            "verify-join-compatible",
            evidence.target_table_id,
            *evidence.source_columns,
        )
        relations.append(
            _contract_relation(
                profile.table_id,
                evidence.source_columns,
                evidence.target_table_id,
                evidence.target_columns,
                rule_id,
            )
        )
    for operation in plan.operations:
        for condition in operation.postconditions:
            if condition.rule_type != "join_compatible":
                continue
            assert condition.target_table_id is not None
            relations.append(
                _contract_relation(
                    profile.table_id,
                    condition.columns,
                    condition.target_table_id,
                    condition.target_columns,
                    condition.rule_id,
                )
            )
    return tuple(relations)


def _contract_relation(
    source_table_id: str,
    source_columns: tuple[str, ...],
    target_table_id: str,
    target_columns: tuple[str, ...],
    rule_id: str,
) -> ContractRelation:
    """构造可追溯到实际 verifier rule 的稳定关联。"""
    return ContractRelation(
        relation_id=_stable_id(
            "contract-relation",
            source_table_id,
            *source_columns,
            target_table_id,
            *target_columns,
            rule_id,
        ),
        source_columns=source_columns,
        target_table_id=target_table_id,
        target_columns=target_columns,
        validation_rule_id=rule_id,
    )


def _validate_rebuilt_contract_semantics(
    work_dir: str | Path,
    store: M15ArtifactStore,
    contract: DataContract,
) -> list[VerificationFailure]:
    """从可信来源和实际 cleaned 文件重建并比较完整表契约。"""
    failures: list[VerificationFailure] = []
    table_sources: dict[str, tuple[DataProfile, CleaningPlan]] = {}
    cleaned_by_table: dict[str, CleanedTable] = {}

    for table in contract.tables:
        try:
            profile = store.read_json(table.data_profile_path, DataProfile)
            plan = store.read_json(table.cleaning_plan_path, CleaningPlan)
        except ArtifactStorageError:
            continue
        if (
            profile.artifact_id != table.data_profile_id
            or profile.table_id != table.table_id
            or plan.artifact_id != table.cleaning_plan_id
            or plan.table_id != table.table_id
            or plan.data_profile_id != profile.artifact_id
            or plan.task_outline_id != profile.task_outline_id
        ):
            continue
        table_sources[table.table_id] = (profile, plan)

        try:
            cleaned_path = resolve_work_dir_path(
                work_dir,
                cleaned_table_path(table.table_id),
                must_exist=True,
            )
            frame = pd.read_csv(
                cleaned_path,
                encoding="utf-8",
                dtype="string",
                keep_default_na=True,
            )
            cleaned_by_table[table.table_id] = CleanedTable(
                table_id=table.table_id,
                data_profile_id=profile.artifact_id,
                cleaning_plan_id=plan.artifact_id,
                relative_path=cleaned_table_path(table.table_id),
                sha256=compute_file_sha256(cleaned_path),
                row_count=len(frame),
                canonical_types=tuple(expected_canonical_types(profile, plan).items()),
            )
        except (ArtifactStorageError, OSError, UnicodeError, ValueError):
            continue

    profiles_by_table = {
        table_id: sources[0] for table_id, sources in table_sources.items()
    }
    for table in contract.tables:
        sources = table_sources.get(table.table_id)
        cleaned = cleaned_by_table.get(table.table_id)
        if sources is None or cleaned is None:
            continue
        profile, plan = sources
        failures.extend(
            verify_cleaned_table(
                work_dir,
                profile,
                plan,
                cleaned,
                profiles_by_table=profiles_by_table,
                cleaned_by_table=cleaned_by_table,
            )
        )
        try:
            expected = _build_contract_table(
                work_dir,
                profile,
                plan,
                cleaned,
            )
        except (ArtifactStorageError, KeyError, OSError, ValueError) as exc:
            failures.append(
                _integrity_failure(
                    table.table_id,
                    "integrity:contract-table-semantics",
                    "rebuildable ContractTable",
                    f"{type(exc).__name__}: {exc}",
                    "无法从可信来源重新构建 DataContract 表。",
                )
            )
            continue
        if expected != table:
            failures.append(
                _integrity_failure(
                    table.table_id,
                    "integrity:contract-table-semantics",
                    expected.model_dump(mode="json"),
                    table.model_dump(mode="json"),
                    (
                        "DataContract 表未与可信 DataProfile、CleaningPlan "
                        "和 cleaned 文件重建结果一致。"
                    ),
                )
            )
    return failures


def _validate_contract_table(
    work_dir: str | Path,
    store: M15ArtifactStore,
    table: ContractTable,
) -> list[VerificationFailure]:
    """复核一张契约表的来源、cleaned 指纹和行列 schema。"""
    failures: list[VerificationFailure] = []
    try:
        profile = store.read_json(table.data_profile_path, DataProfile)
        plan = store.read_json(table.cleaning_plan_path, CleaningPlan)
        expected_lineage = {
            "table_id": table.table_id,
            "data_profile_id": table.data_profile_id,
            "cleaning_plan_id": table.cleaning_plan_id,
            "source_path": table.source_path,
            "source_sheet": table.source_sheet,
            "source_sha256": table.source_sha256,
            "columns": [(column.name, column.source_name) for column in table.columns],
        }
        actual_lineage = {
            "table_id": profile.table_id,
            "data_profile_id": profile.artifact_id,
            "cleaning_plan_id": plan.artifact_id,
            "source_path": profile.source_path,
            "source_sheet": profile.source_sheet,
            "source_sha256": profile.source_sha256,
            "columns": [
                (column.name, column.source_name) for column in profile.columns
            ],
        }
        if (
            actual_lineage != expected_lineage
            or plan.table_id != table.table_id
            or plan.data_profile_id != profile.artifact_id
        ):
            failures.append(
                _integrity_failure(
                    table.table_id,
                    "integrity:lineage-artifacts",
                    expected_lineage,
                    actual_lineage,
                    "DataProfile/CleaningPlan 与冻结表来源不一致。",
                )
            )
    except ArtifactStorageError as exc:
        failures.append(
            _integrity_failure(
                table.table_id,
                "integrity:lineage-artifacts",
                {
                    "data_profile_path": table.data_profile_path,
                    "cleaning_plan_path": table.cleaning_plan_path,
                },
                f"{type(exc).__name__}: {exc}",
                "DataProfile 或 CleaningPlan 无法按冻结路径重载。",
            )
        )

    expected_path = cleaned_table_path(table.table_id)
    if table.cleaned_path != expected_path:
        failures.append(
            _integrity_failure(
                table.table_id,
                "integrity:cleaned-path",
                expected_path,
                table.cleaned_path,
                "cleaned 路径不再匹配稳定 table_id。",
            )
        )
        return failures

    failures.extend(
        _file_hash_failures(
            work_dir,
            table.table_id,
            "source",
            table.source_path,
            table.source_sha256,
        )
    )
    failures.extend(
        _file_hash_failures(
            work_dir,
            table.table_id,
            "cleaned",
            table.cleaned_path,
            table.cleaned_sha256,
        )
    )
    if any(failure.rule_id == "integrity:cleaned-file" for failure in failures):
        return failures

    try:
        path = resolve_work_dir_path(
            work_dir,
            table.cleaned_path,
            must_exist=True,
        )
        frame = pd.read_csv(
            path,
            encoding="utf-8",
            dtype="string",
            keep_default_na=True,
        )
    except Exception as exc:
        failures.append(
            _integrity_failure(
                table.table_id,
                "integrity:cleaned-readable",
                "reloadable UTF-8 CSV",
                f"{type(exc).__name__}: {exc}",
                "cleaned 文件无法按冻结格式重载。",
            )
        )
        return failures

    expected_columns = [column.name for column in table.columns]
    actual_columns = [str(column) for column in frame.columns]
    if actual_columns != expected_columns:
        failures.append(
            _integrity_failure(
                table.table_id,
                "integrity:columns",
                expected_columns,
                actual_columns,
                "cleaned 字段或字段顺序与冻结契约不一致。",
            )
        )
        return failures
    if len(frame) != table.row_count:
        failures.append(
            _integrity_failure(
                table.table_id,
                "integrity:row-count",
                table.row_count,
                len(frame),
                "cleaned 行数与冻结契约不一致。",
            )
        )

    for column in table.columns:
        try:
            cast_series_to_canonical(
                _series(frame, column.name),
                column.canonical_type,
            )
        except Exception as exc:
            failures.append(
                _integrity_failure(
                    table.table_id,
                    validation_rule_id("integrity-type", column.name),
                    column.canonical_type,
                    f"{type(exc).__name__}: {exc}",
                    f"字段 {column.name} 无法按冻结规范类型重载。",
                )
            )
        actual_nullable = bool(_series(frame, column.name).isna().any())
        if actual_nullable != column.nullable:
            failures.append(
                _integrity_failure(
                    table.table_id,
                    validation_rule_id("integrity-nullable", column.name),
                    column.nullable,
                    actual_nullable,
                    f"字段 {column.name} 的 nullable schema 发生变化。",
                )
            )
    return failures


def _file_hash_failures(
    work_dir: str | Path,
    table_id: str,
    role: str,
    relative_path: str,
    expected_sha256: str,
) -> list[VerificationFailure]:
    """校验普通文件仍位于声明路径且指纹未变。"""
    try:
        path = resolve_work_dir_path(
            work_dir,
            relative_path,
            must_exist=True,
        )
        actual: Any = compute_file_sha256(path)
    except (ArtifactStorageError, OSError) as exc:
        actual = f"{type(exc).__name__}: {exc}"
    if actual == expected_sha256:
        return []
    return [
        _integrity_failure(
            table_id,
            f"integrity:{role}-file",
            {"path": relative_path, "sha256": expected_sha256},
            {"path": relative_path, "sha256": actual},
            f"{role} 文件被修改、删除或替换。",
        )
    ]


def _persist_verification_failures(
    store: M15ArtifactStore,
    failures: list[VerificationFailure],
    profiles: dict[str, DataProfile],
    plans: dict[str, CleaningPlan],
    *,
    phase: str,
) -> tuple[DataIssue, ...]:
    """把冻结前复验失败转换为结构化 issue。"""
    result: list[DataIssue] = []
    for failure in failures:
        profile = profiles[failure.table_id]
        plan = plans[failure.table_id]
        issue = _build_integrity_issue(
            failure,
            source_artifact_ids=(profile.artifact_id, plan.artifact_id),
            repair_attempt=plan.repair_attempt,
            phase=phase,
        )
        store.write_json(issue)
        result.append(issue)
    return tuple(result)


def _persist_integrity_failures(
    store: M15ArtifactStore,
    contract: DataContract,
    failures: list[VerificationFailure],
) -> tuple[DataIssue, ...]:
    """把冻结后的完整性漂移转换为结构化 issue。"""
    result: list[DataIssue] = []
    table_by_id = {table.table_id: table for table in contract.tables}
    for failure in failures:
        table = table_by_id.get(failure.table_id)
        source_ids = [contract.artifact_id]
        if table is not None:
            source_ids.extend((table.data_profile_id, table.cleaning_plan_id))
        issue = _build_integrity_issue(
            failure,
            source_artifact_ids=tuple(source_ids),
            repair_attempt=0,
            phase="integrity",
        )
        store.write_json(issue)
        result.append(issue)
    return tuple(result)


def _build_integrity_issue(
    failure: VerificationFailure,
    *,
    source_artifact_ids: tuple[str, ...],
    repair_attempt: int,
    phase: str,
) -> DataIssue:
    """构造稳定且不包含整表数据的完整性失败证据。"""
    actual = _json_value(failure.actual)
    expected = _json_value(failure.expected)
    identity = json.dumps(
        {
            "phase": phase,
            "table_id": failure.table_id,
            "rule_id": failure.rule_id,
            "expected": expected,
            "actual": actual,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:16]
    issue_id = f"data-issue:{digest}"
    return DataIssue(
        schema_version="m1.5",
        artifact_id=issue_id,
        source_artifact_ids=_ordered_unique(source_artifact_ids),
        validation_status="validated",
        artifact_path=f"m15/issues/{digest}.json",
        issue_id=issue_id,
        table_id=failure.table_id,
        rule_id=failure.rule_id,
        expected=expected,
        actual=actual,
        evidence_summary=failure.evidence_summary,
        repair_attempt=repair_attempt,
        status="unresolved",
    )


def _integrity_failure(
    table_id: str,
    rule_id: str,
    expected: Any,
    actual: Any,
    summary: str,
) -> VerificationFailure:
    """构造不可由下游忽略的完整性失败。"""
    return VerificationFailure(
        table_id=table_id,
        rule_id=rule_id,
        expected=expected,
        actual=actual,
        evidence_summary=summary,
        repairable=False,
    )


def _stable_id(prefix: str, *parts: str) -> str:
    """把任意 Unicode 来源字段折叠为合法稳定标识。"""
    identity = "\0".join(parts).encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:16]
    return f"{prefix}:{digest}"


def _series(frame: pd.DataFrame, column_name: str) -> pd.Series:
    """读取唯一字段并为 pandas 返回值收窄类型。"""
    result = frame[column_name]
    if isinstance(result, pd.DataFrame):
        raise ValueError(f"字段不唯一: {column_name}")
    return result


def _ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    """按首次出现顺序去重来源标识。"""
    return tuple(dict.fromkeys(values))


def _json_value(value: Any) -> Any:
    """将 verifier 的 tuple 等内部值转换为严格 JSON 值。"""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))
