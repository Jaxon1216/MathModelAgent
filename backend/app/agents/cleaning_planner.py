"""Modeler 的 M1.5 受约束清洗规划步骤。"""

from __future__ import annotations

import hashlib
import math

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from app.domain.m15 import (
    CleaningOperation,
    CleaningPlan,
    DataIssue,
    DataProfile,
    TaskOutline,
)
from app.prompts.cleaning import (
    get_cleaning_repair_prompt,
    get_cleaning_request,
    get_cleaning_system_prompt,
    get_table_repair_request,
    get_table_repair_system_prompt,
)
from app.runtime.llm.client import ChatMessage, LLMClient


class CleaningPlanResponseError(RuntimeError):
    """有限 JSON 修复后仍无法获得合法清洗计划。"""

    def __init__(self, errors: tuple[str, ...], attempts: int) -> None:
        self.errors = errors
        self.attempts = attempts
        super().__init__(
            f"CleaningPlan validation failed after {attempts} attempts: "
            f"{'; '.join(errors)}"
        )


class _PlanProposal(BaseModel):
    """模型可声明的最小表级计划，不允许提供路径或来源元数据。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    table_id: str
    operations: tuple[CleaningOperation, ...] = ()
    no_op_reason: str | None = None

    @model_validator(mode="after")
    def validate_no_op(self) -> _PlanProposal:
        """空操作和实际操作必须互斥。"""
        if self.operations and self.no_op_reason is not None:
            raise ValueError("有清洗操作时不能设置 no_op_reason")
        if not self.operations and (
            self.no_op_reason is None or not self.no_op_reason.strip()
        ):
            raise ValueError("空清洗计划必须说明 no_op_reason")
        return self


class _PlanProposalSet(BaseModel):
    """初始规划响应必须完整覆盖全部画像表。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    plans: tuple[_PlanProposal, ...]


class CleaningPlannerAgent:
    """使用现有 LLMClient 生成严格、无代码的逐表 CleaningPlan。"""

    def __init__(
        self,
        client: LLMClient,
        *,
        max_json_repair_attempts: int = 2,
    ) -> None:
        if max_json_repair_attempts < 0:
            raise ValueError("max_json_repair_attempts 不能小于 0")
        self._client = client
        self._max_json_repair_attempts = max_json_repair_attempts

    async def plan(
        self,
        outline: TaskOutline,
        profiles: tuple[DataProfile, ...],
    ) -> tuple[CleaningPlan, ...]:
        """只依据 TaskOutline 和 DataProfile 生成完整清洗计划。"""
        if not profiles:
            return ()
        return await self._request_validated_plan_set(
            outline,
            profiles,
        )

    async def _request_validated_plan_set(
        self,
        outline: TaskOutline,
        profiles: tuple[DataProfile, ...],
    ) -> tuple[CleaningPlan, ...]:
        """在同一有限预算内修复 JSON 和计划语义错误。

        Args:
            outline: 当前经过校验的全局任务骨架。
            profiles: 只读数据画像。

        Returns:
            逐表完整、通过 scope 校验的清洗计划。

        Raises:
            CleaningPlanResponseError: 有限次数后仍没有合法计划。
        """
        base_messages: list[ChatMessage] = [
            {"role": "system", "content": get_cleaning_system_prompt()},
            {
                "role": "user",
                "content": get_cleaning_request(outline, profiles),
            },
        ]
        errors: tuple[str, ...] = ()
        total_attempts = self._max_json_repair_attempts + 1
        for _attempt in range(1, total_attempts + 1):
            messages = list(base_messages)
            if errors:
                messages.append(
                    {
                        "role": "user",
                        "content": get_cleaning_repair_prompt(errors),
                    }
                )
            raw_content = await self._client.complete(messages)
            try:
                proposal_set = _PlanProposalSet.model_validate_json(
                    _strip_json_fence(raw_content)
                )
                return _build_validated_plan_set(outline, profiles, proposal_set)
            except (ValidationError, ValueError) as exc:
                errors = _validation_error_messages(exc)

        raise CleaningPlanResponseError(errors, attempts=total_attempts)

    async def repair(
        self,
        profile: DataProfile,
        current_plan: CleaningPlan,
        issues: tuple[DataIssue, ...],
        *,
        repair_attempt: int,
    ) -> CleaningPlan:
        """只允许针对一个失败表和其失败规则重写计划。"""
        if repair_attempt not in {1, 2}:
            raise ValueError("repair_attempt 必须为 1 或 2")
        if not issues:
            raise ValueError("Repair 必须接收至少一个失败 DataIssue")
        if any(issue.table_id != profile.table_id for issue in issues):
            raise ValueError("Repair 不能接收其他表的 DataIssue")
        if current_plan.table_id != profile.table_id:
            raise ValueError("Repair 的 CleaningPlan 与 DataProfile 不属于同一表")
        _validate_repair_issues(current_plan, issues)
        if repair_attempt != current_plan.repair_attempt + 1:
            raise ValueError("repair_attempt 必须严格递增一次")

        return await self._request_validated_repair_plan(
            profile,
            current_plan,
            issues,
            repair_attempt=repair_attempt,
        )

    async def _request_validated_repair_plan(
        self,
        profile: DataProfile,
        current_plan: CleaningPlan,
        issues: tuple[DataIssue, ...],
        *,
        repair_attempt: int,
    ) -> CleaningPlan:
        """在同一有限预算内修复 Repair 的 JSON 和语义错误。

        Args:
            profile: 失败表的只读画像。
            current_plan: 产生失败证据的当前计划。
            issues: 与当前计划绑定的未解决失败证据。
            repair_attempt: 当前严格递增的一次 Repair 编号。

        Returns:
            不扩张授权范围且通过 scope 校验的 Repair 计划。

        Raises:
            CleaningPlanResponseError: 有限次数后仍没有合法 Repair 计划。
        """
        base_messages: list[ChatMessage] = [
            {"role": "system", "content": get_table_repair_system_prompt()},
            {
                "role": "user",
                "content": get_table_repair_request(
                    profile,
                    current_plan,
                    issues,
                ),
            },
        ]
        errors: tuple[str, ...] = ()
        total_attempts = self._max_json_repair_attempts + 1
        for _attempt in range(1, total_attempts + 1):
            messages = list(base_messages)
            if errors:
                messages.append(
                    {
                        "role": "user",
                        "content": get_cleaning_repair_prompt(errors),
                    }
                )
            raw_content = await self._client.complete(messages)
            try:
                proposal = _PlanProposal.model_validate_json(
                    _strip_json_fence(raw_content)
                )
                if proposal.table_id != profile.table_id:
                    raise ValueError("Repair 返回了未授权 table_id")
                repaired = _build_repaired_plan(
                    profile,
                    current_plan,
                    issues,
                    proposal,
                    repair_attempt=repair_attempt,
                )
                validate_cleaning_plan_scope(repaired, profile)
                validate_repair_scope(current_plan, repaired, issues)
                return repaired
            except (ValidationError, ValueError) as exc:
                errors = _validation_error_messages(exc)
        raise CleaningPlanResponseError(errors, attempts=total_attempts)


def _build_validated_plan_set(
    outline: TaskOutline,
    profiles: tuple[DataProfile, ...],
    proposal_set: _PlanProposalSet,
) -> tuple[CleaningPlan, ...]:
    """将模型提案转换为带可信元数据且通过 scope 校验的计划。"""
    profile_by_table = {profile.table_id: profile for profile in profiles}
    proposal_ids = [proposal.table_id for proposal in proposal_set.plans]
    if len(set(proposal_ids)) != len(proposal_ids):
        raise ValueError("plans 不能包含重复 table_id")
    if set(proposal_ids) != set(profile_by_table):
        raise ValueError(
            "plans 必须严格覆盖 DataProfile tables: "
            f"expected={sorted(profile_by_table)}, actual={sorted(proposal_ids)}"
        )

    plans: list[CleaningPlan] = []
    for profile in profiles:
        proposal = next(
            item
            for item in proposal_set.plans
            if item.table_id == profile.table_id
        )
        plan = _build_plan(outline, profile, proposal)
        validate_cleaning_plan_scope(plan, profile)
        plans.append(plan)
    return tuple(plans)


def _validation_error_messages(
    exc: ValidationError | ValueError,
) -> tuple[str, ...]:
    """压缩结构化校验错误，不回灌无效模型原文。"""
    if isinstance(exc, ValidationError):
        return tuple(
            f"{error['type']} "
            f"{'.'.join(str(item) for item in error['loc'])}: "
            f"{error['msg']}"
            for error in exc.errors()
        )
    return (str(exc),)


def _build_plan(
    outline: TaskOutline,
    profile: DataProfile,
    proposal: _PlanProposal,
) -> CleaningPlan:
    """为模型提案补入唯一可信的来源、标识和产物路径。"""
    digest = _table_digest(profile.table_id)
    return CleaningPlan(
        schema_version="m1.5",
        artifact_id=f"cleaning-plan:{digest}",
        source_artifact_ids=(outline.artifact_id, profile.artifact_id),
        validation_status="validated",
        artifact_path=f"m15/cleaning_plans/{digest}.json",
        task_outline_id=outline.artifact_id,
        data_profile_id=profile.artifact_id,
        table_id=profile.table_id,
        operations=proposal.operations,
        no_op_reason=proposal.no_op_reason,
    )


def _build_repaired_plan(
    profile: DataProfile,
    current_plan: CleaningPlan,
    issues: tuple[DataIssue, ...],
    proposal: _PlanProposal,
    *,
    repair_attempt: int,
) -> CleaningPlan:
    """为 Repair 提案补入仅由本地事实决定的可信元数据。"""
    outline_id = current_plan.task_outline_id
    digest = _table_digest(profile.table_id)
    issue_ids = tuple(issue.issue_id for issue in issues)
    return CleaningPlan(
        schema_version="m1.5",
        artifact_id=f"cleaning-plan:{digest}-repair{repair_attempt}",
        source_artifact_ids=(
            outline_id,
            profile.artifact_id,
            current_plan.artifact_id,
            *issue_ids,
        ),
        validation_status="validated",
        artifact_path=f"m15/cleaning_plans/{digest}.repair{repair_attempt}.json",
        task_outline_id=outline_id,
        data_profile_id=profile.artifact_id,
        table_id=profile.table_id,
        operations=proposal.operations,
        no_op_reason=proposal.no_op_reason,
        repair_attempt=repair_attempt,
        repaired_issue_ids=issue_ids,
    )


def validate_cleaning_plan_scope(
    plan: CleaningPlan,
    profile: DataProfile,
) -> None:
    """拒绝契约外字段、关联和与画像不符的参数。"""
    if plan.validation_status != "validated":
        raise ValueError("CleaningPlan 未通过校验，不能执行")
    if plan.task_outline_id != profile.task_outline_id:
        raise ValueError("CleaningPlan 与 DataProfile 不属于同一 TaskOutline")
    if plan.table_id != profile.table_id:
        raise ValueError("CleaningPlan.table_id 与 DataProfile 不一致")
    if plan.data_profile_id != profile.artifact_id:
        raise ValueError("CleaningPlan.data_profile_id 与 DataProfile 不一致")
    if plan.repair_attempt == 0 and plan.source_artifact_ids != (
        plan.task_outline_id,
        plan.data_profile_id,
    ):
        raise ValueError("初始 CleaningPlan 只能来源于当前 outline 和 profile")

    known_columns = {column.name for column in profile.columns}
    current_types = {column.name: column.canonical_type for column in profile.columns}
    join_keys = {
        (
            evidence.target_table_id,
            evidence.source_columns,
            evidence.target_columns,
        )
        for evidence in profile.join_evidence
    }
    for operation in plan.operations:
        unknown = set(operation.target_columns) - known_columns
        if unknown:
            raise ValueError(
                f"{operation.operation_id} 引用契约外字段: {sorted(unknown)}"
            )
        if operation.operation_type == "fill_missing" and operation.strategy in {
            "mean",
            "median",
        }:
            non_numeric = [
                column
                for column in operation.target_columns
                if current_types[column] not in {"integer", "number"}
            ]
            if non_numeric:
                raise ValueError(f"数值填充只能用于数值列: {sorted(non_numeric)}")
        if (
            operation.operation_type == "fill_missing"
            and operation.strategy == "constant"
        ):
            incompatible = [
                column
                for column in operation.target_columns
                if not _constant_matches_type(
                    operation.fill_value,
                    current_types[column],
                )
            ]
            if incompatible:
                raise ValueError(
                    f"常量填充值与目标列类型不兼容: {sorted(incompatible)}"
                )
        if operation.operation_type == "normalize_join_key" and not any(
            evidence.source_columns == operation.target_columns
            for evidence in profile.join_evidence
        ):
            raise ValueError(f"{operation.operation_id} 只能规范化画像声明的关联键")
        for condition in operation.postconditions:
            unknown_condition_columns = set(condition.columns) - known_columns
            if unknown_condition_columns:
                raise ValueError(
                    f"{condition.rule_id} 引用契约外字段: "
                    f"{sorted(unknown_condition_columns)}"
                )
            if condition.rule_type == "source_sha256":
                if condition.expected != profile.source_sha256:
                    raise ValueError("source_sha256 后置条件必须等于画像指纹")
            if condition.rule_type == "join_compatible":
                join_key = (
                    condition.target_table_id,
                    condition.columns,
                    condition.target_columns,
                )
                if join_key not in join_keys:
                    raise ValueError(f"{condition.rule_id} 引用了画像外关联关系")
        if operation.operation_type == "cast_type":
            assert operation.target_type is not None
            for column in operation.target_columns:
                current_types[column] = operation.target_type
        elif operation.operation_type == "normalize_join_key":
            for column in operation.target_columns:
                current_types[column] = "string"
        elif operation.operation_type == "fill_missing" and operation.strategy in {
            "mean",
            "median",
        }:
            for column in operation.target_columns:
                current_types[column] = "number"


def validate_repair_scope(
    current_plan: CleaningPlan,
    repaired_plan: CleaningPlan,
    issues: tuple[DataIssue, ...],
) -> None:
    """保证 Repair 不改动已通过规则，也不扩张到新规则。"""
    _validate_repair_issues(current_plan, issues)
    issue_ids = tuple(issue.issue_id for issue in issues)
    if (
        repaired_plan.task_outline_id != current_plan.task_outline_id
        or repaired_plan.data_profile_id != current_plan.data_profile_id
        or repaired_plan.table_id != current_plan.table_id
    ):
        raise ValueError("Repair 不能切换 TaskOutline、DataProfile 或表")
    if repaired_plan.repair_attempt != current_plan.repair_attempt + 1:
        raise ValueError("Repair 必须严格递增一次 repair_attempt")
    if repaired_plan.repair_attempt not in {1, 2}:
        raise ValueError("Repair 不能超过两次")
    if repaired_plan.repaired_issue_ids != issue_ids:
        raise ValueError("Repair 必须且只能声明当前失败 issue")
    expected_sources = (
        current_plan.task_outline_id,
        current_plan.data_profile_id,
        current_plan.artifact_id,
        *issue_ids,
    )
    if repaired_plan.source_artifact_ids != expected_sources:
        raise ValueError("Repair 来源必须严格限定为当前计划和失败 issue")

    failed_rule_ids = {issue.rule_id for issue in issues}
    current_by_id = {
        operation.operation_id: operation for operation in current_plan.operations
    }
    repaired_by_id = {
        operation.operation_id: operation for operation in repaired_plan.operations
    }
    current_conditions = {
        condition.rule_id: condition
        for operation in current_plan.operations
        for condition in operation.postconditions
    }
    repaired_conditions = {
        condition.rule_id: condition
        for operation in repaired_plan.operations
        for condition in operation.postconditions
    }
    current_targets_by_rule = {
        condition.rule_id: operation.target_columns
        for operation in current_plan.operations
        for condition in operation.postconditions
    }

    for rule_id, current_condition in current_conditions.items():
        repaired_condition = repaired_conditions.get(rule_id)
        if rule_id in failed_rule_ids:
            if (
                repaired_condition is not None
                and repaired_condition != current_condition
            ):
                raise ValueError(f"Repair 不能重新定义失败规则定义: {rule_id}")
            continue
        if repaired_condition != current_condition:
            raise ValueError(f"Repair 不能改变或删除既有规则定义: {rule_id}")

    for operation_id, current_operation in current_by_id.items():
        operation_rules = {
            condition.rule_id for condition in current_operation.postconditions
        }
        if (
            not operation_rules.issubset(failed_rule_ids)
            and repaired_by_id.get(operation_id) != current_operation
        ):
            raise ValueError(f"Repair 修改了未失败规则对应操作: {operation_id}")

    for operation_id, repaired_operation in repaired_by_id.items():
        current_operation = current_by_id.get(operation_id)
        if current_operation == repaired_operation:
            continue
        repaired_rules = {
            condition.rule_id for condition in repaired_operation.postconditions
        }
        if not repaired_rules.issubset(failed_rule_ids):
            raise ValueError(f"Repair 操作超出失败规则授权: {operation_id}")
        if not repaired_rules.issubset(current_conditions):
            raise ValueError(f"Repair 引入了未授权规则: {operation_id}")
        expected_targets = {
            current_targets_by_rule[rule_id]
            for rule_id in repaired_rules
        }
        if expected_targets != {repaired_operation.target_columns}:
            raise ValueError(f"Repair 不能改变失败规则的目标列: {operation_id}")

    for operation_id, current_operation in current_by_id.items():
        if operation_id in repaired_by_id:
            continue
        removed_rules = {
            condition.rule_id for condition in current_operation.postconditions
        }
        if not removed_rules.issubset(failed_rule_ids):
            raise ValueError(f"Repair 删除了未失败规则对应操作: {operation_id}")

    protected_operation_ids = {
        operation.operation_id
        for operation in current_plan.operations
        if {condition.rule_id for condition in operation.postconditions}
        - failed_rule_ids
    }
    current_protected_order = [
        operation.operation_id
        for operation in current_plan.operations
        if operation.operation_id in protected_operation_ids
    ]
    repaired_protected_order = [
        operation.operation_id
        for operation in repaired_plan.operations
        if operation.operation_id in protected_operation_ids
    ]
    if repaired_protected_order != current_protected_order:
        raise ValueError("Repair 不能重排未失败规则对应操作")


def _validate_repair_issues(
    current_plan: CleaningPlan,
    issues: tuple[DataIssue, ...],
) -> None:
    """在调用 Repair 模型前确认 issue 确实来自当前表和当前计划。"""
    if not issues:
        raise ValueError("Repair 必须接收至少一个失败 DataIssue")
    issue_ids = tuple(issue.issue_id for issue in issues)
    if len(set(issue_ids)) != len(issue_ids):
        raise ValueError("Repair 的 DataIssue 不能重复")
    for issue in issues:
        if issue.table_id != current_plan.table_id:
            raise ValueError("Repair 不能接收其他表的 DataIssue")
        if issue.status != "unresolved":
            raise ValueError("Repair 只能处理 unresolved DataIssue")
        if issue.repair_attempt != current_plan.repair_attempt:
            raise ValueError("DataIssue 与当前 CleaningPlan 的 Repair 次数不一致")
        if current_plan.artifact_id not in issue.source_artifact_ids:
            raise ValueError("DataIssue 不是由当前 CleaningPlan 产生")


def _constant_matches_type(value: object, canonical_type: str) -> bool:
    """限制常量填充值与画像规范类型一致，不执行隐式表达式转换。"""
    if canonical_type in {"string", "category", "date", "datetime"}:
        return isinstance(value, str)
    if canonical_type == "boolean":
        return isinstance(value, bool)
    if canonical_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if canonical_type == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and (not isinstance(value, float) or math.isfinite(value))
        )
    return False


def _strip_json_fence(raw_content: str) -> str:
    """仅移除包围完整响应的 Markdown 围栏。"""
    text = raw_content.strip()
    if text.startswith("```json") and text.endswith("```"):
        return text[len("```json") : -len("```")].strip()
    if text.startswith("```") and text.endswith("```"):
        return text[len("```") : -len("```")].strip()
    return text


def _table_digest(table_id: str) -> str:
    """返回跨运行稳定的短表标识。"""
    return hashlib.sha256(table_id.encode("utf-8")).hexdigest()[:16]
