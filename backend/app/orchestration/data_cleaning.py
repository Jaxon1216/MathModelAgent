"""M1.5 清洗计划、执行、验证和局部 Repair 编排。"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from app.agents.cleaning_planner import (
    CleaningPlannerAgent,
    validate_cleaning_plan_scope,
    validate_repair_scope,
)
from app.data.artifact_store import (
    ArtifactStorageError,
    M15ArtifactStore,
    resolve_work_dir_path,
)
from app.data.cleaning import (
    CleanedTable,
    TableCleaningError,
    VerificationFailure,
    cleaned_table_path,
    execute_cleaning_plan,
    verify_cleaned_table,
)
from app.domain.m15 import (
    CleaningPlan,
    DataIssue,
    DataProfile,
    TaskOutline,
)

MAX_DATA_REPAIR_ATTEMPTS = 2
DataCleaningStepKind = Literal["clean", "verify", "repair"]
DataCleaningStepStatus = Literal["success", "failure", "cancelled"]


class CleaningPlanner(Protocol):
    """数据清洗编排所需的最小 Modeler 规划能力。"""

    async def plan(
        self,
        outline: TaskOutline,
        profiles: tuple[DataProfile, ...],
    ) -> tuple[CleaningPlan, ...]:
        """生成完整初始计划。"""
        ...

    async def repair(
        self,
        profile: DataProfile,
        current_plan: CleaningPlan,
        issues: tuple[DataIssue, ...],
        *,
        repair_attempt: int,
    ) -> CleaningPlan:
        """仅修复指定表的失败规则。"""
        ...


class DataCleaningEventSink(Protocol):
    """DataCleaningWorkflow 所需的最小异步事件出口。"""

    async def emit(self, event: str, **payload: Any) -> None:
        """记录清洗子步骤事件。"""
        ...


class NullDataCleaningEventSink:
    """独立调用 DataCleaningWorkflow 时使用的无副作用事件出口。"""

    async def emit(self, event: str, **payload: Any) -> None:
        """忽略清洗子步骤事件。"""


@dataclass(frozen=True)
class DataCleaningResult:
    """Task 4 成功终态，不提前构造 Task 5 DataContract。"""

    plans: tuple[CleaningPlan, ...]
    cleaned_tables: tuple[CleanedTable, ...]
    issues: tuple[DataIssue, ...]

    @property
    def resolved_issue_ids(self) -> tuple[str, ...]:
        """返回最终已有 resolved 证据的 issue 标识。"""
        return tuple(
            issue.issue_id for issue in self.issues if issue.status == "resolved"
        )


class DataCleaningBlockedError(RuntimeError):
    """清洗阶段因不可修复失败或两次 Repair 耗尽而阻断。"""

    def __init__(
        self,
        issues: tuple[DataIssue, ...],
        *,
        failure_kind: str = "verify",
    ) -> None:
        self.issues = issues
        self.failure_kind = failure_kind
        terminal = issues[-1] if issues else None
        detail = (
            f"{terminal.table_id}/{terminal.rule_id}/{terminal.status}"
            if terminal is not None
            else "unknown"
        )
        super().__init__(f"M1.5 数据清洗被阻断: {detail}")


@dataclass(frozen=True)
class _CleanAttemptResult:
    """单次表清洗的成功产物或已持久化失败。"""

    cleaned: CleanedTable | None = None
    issue: DataIssue | None = None
    error: TableCleaningError | None = None


class DataCleaningWorkflow:
    """执行逐表清洗，并对每张失败表最多进行两次局部 Repair。"""

    def __init__(
        self,
        planner: CleaningPlannerAgent | CleaningPlanner,
        *,
        event_sink: DataCleaningEventSink | None = None,
    ) -> None:
        self._planner = planner
        self._event_sink = event_sink or NullDataCleaningEventSink()

    async def run(
        self,
        *,
        work_dir: str | Path,
        outline: TaskOutline,
        profiles: tuple[DataProfile, ...],
    ) -> DataCleaningResult:
        """完成 Task 4 清洗闭环，失败时保留全部 DataIssue 证据。"""
        store = M15ArtifactStore(work_dir)
        if outline.validation_status != "validated":
            raise ValueError("TaskOutline 未通过校验，不能进入清洗阶段")
        profile_by_table = {profile.table_id: profile for profile in profiles}
        if len(profile_by_table) != len(profiles):
            raise ValueError("DataProfile.table_id 不能重复")
        for profile in profiles:
            if profile.validation_status != "validated":
                raise ValueError("DataProfile 未通过校验，不能进入清洗阶段")
            if profile.task_outline_id != outline.artifact_id:
                raise ValueError("DataProfile 不属于当前 TaskOutline")

        initial_plans = await self._planner.plan(outline, profiles)
        plans_by_table = {plan.table_id: plan for plan in initial_plans}
        if len(plans_by_table) != len(initial_plans):
            raise ValueError("CleaningPlan.table_id 不能重复")
        if set(plans_by_table) != set(profile_by_table):
            raise ValueError("CleaningPlan 必须严格覆盖全部 DataProfile")
        for table_id, plan in plans_by_table.items():
            if plan.repair_attempt != 0:
                raise ValueError("初始 CleaningPlan 的 repair_attempt 必须为 0")
            validate_cleaning_plan_scope(plan, profile_by_table[table_id])
            store.write_json(plan)

        cleaned_by_table: dict[str, CleanedTable] = {}
        evidence: list[DataIssue] = []
        unresolved_history: dict[str, list[DataIssue]] = {
            table_id: [] for table_id in profile_by_table
        }
        verify_round_by_table = dict.fromkeys(profile_by_table, 0)

        for profile in profiles:
            plan, cleaned = await self._execute_with_repair(
                work_dir=work_dir,
                profiles=profiles,
                profile=profile,
                plan=plans_by_table[profile.table_id],
                store=store,
                evidence=evidence,
                unresolved_history=unresolved_history[profile.table_id],
            )
            plans_by_table[profile.table_id] = plan
            cleaned_by_table[profile.table_id] = cleaned

        while True:
            repaired_any = False
            for profile in profiles:
                table_id = profile.table_id
                plan = plans_by_table[table_id]
                cleaned = cleaned_by_table[table_id]
                verify_round_by_table[table_id] += 1
                failures, issues = await self._verify_once(
                    work_dir=work_dir,
                    profile=profile,
                    plan=plan,
                    cleaned=cleaned,
                    profiles_by_table=profile_by_table,
                    cleaned_by_table=cleaned_by_table,
                    store=store,
                    verify_round=verify_round_by_table[table_id],
                )
                if not failures:
                    continue

                has_nonrepairable = any(not failure.repairable for failure in failures)
                evidence.extend(issues)
                if has_nonrepairable:
                    raise DataCleaningBlockedError(
                        tuple(evidence),
                        failure_kind="verify",
                    )

                if plan.repair_attempt >= MAX_DATA_REPAIR_ATTEMPTS:
                    raise DataCleaningBlockedError(
                        tuple(evidence),
                        failure_kind="repair",
                    )

                unresolved_history[table_id].extend(issues)
                repaired = await self._repair_once(
                    profile=profile,
                    current_plan=plan,
                    issues=issues,
                    repair_attempt=plan.repair_attempt + 1,
                    store=store,
                )
                plans_by_table[table_id] = repaired

                repaired, cleaned = await self._execute_with_repair(
                    work_dir=work_dir,
                    profiles=profiles,
                    profile=profile,
                    plan=repaired,
                    store=store,
                    evidence=evidence,
                    unresolved_history=unresolved_history[table_id],
                )
                plans_by_table[table_id] = repaired
                cleaned_by_table[table_id] = cleaned
                repaired_any = True
                break

            if not repaired_any:
                break

        for profile in profiles:
            self._persist_resolutions(
                profile,
                plans_by_table[profile.table_id],
                unresolved_history[profile.table_id],
                store,
                evidence,
            )

        ordered_plans = tuple(plans_by_table[profile.table_id] for profile in profiles)
        ordered_cleaned = tuple(
            cleaned_by_table[profile.table_id] for profile in profiles
        )
        return DataCleaningResult(
            plans=ordered_plans,
            cleaned_tables=ordered_cleaned,
            issues=tuple(evidence),
        )

    async def _execute_with_repair(
        self,
        *,
        work_dir: str | Path,
        profiles: tuple[DataProfile, ...],
        profile: DataProfile,
        plan: CleaningPlan,
        store: M15ArtifactStore,
        evidence: list[DataIssue],
        unresolved_history: list[DataIssue],
    ) -> tuple[CleaningPlan, CleanedTable]:
        """将执行错误也纳入同一个两次表级 Repair 预算。"""
        current_plan = plan
        while True:
            attempt_result = await self._clean_once(
                work_dir=work_dir,
                profiles=profiles,
                profile=profile,
                plan=current_plan,
                store=store,
            )
            if attempt_result.cleaned is not None:
                return current_plan, attempt_result.cleaned
            if attempt_result.issue is None or attempt_result.error is None:
                raise RuntimeError("表清洗失败未返回结构化 DataIssue")

            issue = attempt_result.issue
            failure = attempt_result.error.failure
            evidence.append(issue)
            if not failure.repairable:
                raise DataCleaningBlockedError(
                    tuple(evidence),
                    failure_kind="clean",
                ) from attempt_result.error
            if current_plan.repair_attempt >= MAX_DATA_REPAIR_ATTEMPTS:
                raise DataCleaningBlockedError(
                    tuple(evidence),
                    failure_kind="repair",
                ) from attempt_result.error

            unresolved_history.append(issue)
            current_plan = await self._repair_once(
                profile=profile,
                current_plan=current_plan,
                issues=(issue,),
                repair_attempt=current_plan.repair_attempt + 1,
                store=store,
            )

    async def _clean_once(
        self,
        *,
        work_dir: str | Path,
        profiles: tuple[DataProfile, ...],
        profile: DataProfile,
        plan: CleaningPlan,
        store: M15ArtifactStore,
    ) -> _CleanAttemptResult:
        """执行一次表清洗，并在返回前写入唯一终态。"""
        step = "clean"
        attempt = plan.repair_attempt
        step_id = _step_id(step, profile.table_id, attempt)
        started_at = time.monotonic()
        status: DataCleaningStepStatus = "failure"
        failure_kind: str | None = "clean"
        await self._emit_step(
            step,
            "start",
            step_id=step_id,
            table_id=profile.table_id,
            attempt=attempt,
            cleaning_plan_id=plan.artifact_id,
        )
        try:
            try:
                cleaned = execute_cleaning_plan(
                    work_dir,
                    profile,
                    plan,
                    source_profiles=profiles,
                )
            except TableCleaningError as exc:
                _discard_cleaned_output(work_dir, profile.table_id)
                issue_status = (
                    "exhausted"
                    if (exc.failure.repairable and attempt >= MAX_DATA_REPAIR_ATTEMPTS)
                    else "unresolved"
                )
                issue = self._persist_failure(
                    profile,
                    plan,
                    exc.failure,
                    store,
                    status=issue_status,
                )
                await self._emit_issue(step, step_id, issue)
                return _CleanAttemptResult(issue=issue, error=exc)

            await self._emit_step(
                step,
                "artifact",
                step_id=step_id,
                table_id=profile.table_id,
                attempt=attempt,
                artifact_type="cleaned_table",
                cleaning_plan_id=plan.artifact_id,
                path=cleaned.relative_path,
                sha256=cleaned.sha256,
                row_count=cleaned.row_count,
            )
            status = "success"
            failure_kind = None
            return _CleanAttemptResult(cleaned=cleaned)
        except asyncio.CancelledError:
            status = "cancelled"
            failure_kind = "cancelled"
            raise
        finally:
            await self._emit_step_end(
                step,
                step_id=step_id,
                table_id=profile.table_id,
                attempt=attempt,
                started_at=started_at,
                status=status,
                failure_kind=failure_kind,
            )

    async def _verify_once(
        self,
        *,
        work_dir: str | Path,
        profile: DataProfile,
        plan: CleaningPlan,
        cleaned: CleanedTable,
        profiles_by_table: dict[str, DataProfile],
        cleaned_by_table: dict[str, CleanedTable],
        store: M15ArtifactStore,
        verify_round: int,
    ) -> tuple[tuple[VerificationFailure, ...], tuple[DataIssue, ...]]:
        """执行一轮确定性验证，并记录全部失败 issue。"""
        step = "verify"
        attempt = plan.repair_attempt
        step_id = _step_id(step, profile.table_id, attempt, verify_round)
        started_at = time.monotonic()
        status: DataCleaningStepStatus = "failure"
        failure_kind: str | None = "verify"
        await self._emit_step(
            step,
            "start",
            step_id=step_id,
            table_id=profile.table_id,
            attempt=attempt,
            verify_round=verify_round,
            cleaning_plan_id=plan.artifact_id,
        )
        try:
            failures = verify_cleaned_table(
                work_dir,
                profile,
                plan,
                cleaned,
                profiles_by_table=profiles_by_table,
                cleaned_by_table=cleaned_by_table,
            )
            if failures:
                _discard_cleaned_output(work_dir, profile.table_id)
                has_nonrepairable = any(not failure.repairable for failure in failures)
                issue_status = (
                    "exhausted"
                    if (not has_nonrepairable and attempt >= MAX_DATA_REPAIR_ATTEMPTS)
                    else "unresolved"
                )
                issues = tuple(
                    self._persist_failure(
                        profile,
                        plan,
                        failure,
                        store,
                        status=issue_status,
                    )
                    for failure in failures
                )
                for issue in issues:
                    await self._emit_issue(step, step_id, issue)
                return failures, issues

            await self._emit_step(
                step,
                "artifact",
                step_id=step_id,
                table_id=profile.table_id,
                attempt=attempt,
                verify_round=verify_round,
                artifact_type="verified_cleaned_table",
                cleaning_plan_id=plan.artifact_id,
                path=cleaned.relative_path,
                sha256=cleaned.sha256,
                row_count=cleaned.row_count,
            )
            status = "success"
            failure_kind = None
            return (), ()
        except asyncio.CancelledError:
            status = "cancelled"
            failure_kind = "cancelled"
            raise
        finally:
            await self._emit_step_end(
                step,
                step_id=step_id,
                table_id=profile.table_id,
                attempt=attempt,
                started_at=started_at,
                status=status,
                failure_kind=failure_kind,
                verify_round=verify_round,
            )

    async def _repair_once(
        self,
        *,
        profile: DataProfile,
        current_plan: CleaningPlan,
        issues: tuple[DataIssue, ...],
        repair_attempt: int,
        store: M15ArtifactStore,
    ) -> CleaningPlan:
        """执行一次局部 Repair，并记录输入 issue 与输出计划。"""
        step = "repair"
        step_id = _step_id(step, profile.table_id, repair_attempt)
        started_at = time.monotonic()
        status: DataCleaningStepStatus = "failure"
        failure_kind: str | None = "repair"
        await self._emit_step(
            step,
            "start",
            step_id=step_id,
            table_id=profile.table_id,
            attempt=repair_attempt,
            cleaning_plan_id=current_plan.artifact_id,
            issue_ids=tuple(issue.issue_id for issue in issues),
        )
        try:
            for issue in issues:
                await self._emit_issue(
                    step,
                    step_id,
                    issue,
                    attempt=repair_attempt,
                )
            repaired = await self._planner.repair(
                profile,
                current_plan,
                issues,
                repair_attempt=repair_attempt,
            )
            validate_cleaning_plan_scope(repaired, profile)
            validate_repair_scope(current_plan, repaired, issues)
            store.write_json(repaired)
            await self._emit_step(
                step,
                "artifact",
                step_id=step_id,
                table_id=profile.table_id,
                attempt=repair_attempt,
                artifact_type="cleaning_plan",
                artifact_id=repaired.artifact_id,
                artifact_path=repaired.artifact_path,
                validation_status=repaired.validation_status,
            )
            status = "success"
            failure_kind = None
            return repaired
        except asyncio.CancelledError:
            status = "cancelled"
            failure_kind = "cancelled"
            raise
        finally:
            await self._emit_step_end(
                step,
                step_id=step_id,
                table_id=profile.table_id,
                attempt=repair_attempt,
                started_at=started_at,
                status=status,
                failure_kind=failure_kind,
            )

    async def _emit_issue(
        self,
        step: DataCleaningStepKind,
        step_id: str,
        issue: DataIssue,
        *,
        attempt: int | None = None,
    ) -> None:
        """记录不含数据内容的结构化 issue 事件。"""
        await self._emit_step(
            step,
            "issue",
            step_id=step_id,
            table_id=issue.table_id,
            attempt=issue.repair_attempt if attempt is None else attempt,
            issue_attempt=issue.repair_attempt,
            artifact_type="data_issue",
            artifact_id=issue.artifact_id,
            artifact_path=issue.artifact_path,
            issue_id=issue.issue_id,
            rule_id=issue.rule_id,
            issue_status=issue.status,
        )

    async def _emit_step_end(
        self,
        step: DataCleaningStepKind,
        *,
        step_id: str,
        table_id: str,
        attempt: int,
        started_at: float,
        status: DataCleaningStepStatus,
        failure_kind: str | None,
        **payload: Any,
    ) -> None:
        """为一次清洗子步骤写入唯一终态。"""
        await self._emit_step(
            step,
            "end",
            step_id=step_id,
            table_id=table_id,
            attempt=attempt,
            status=status,
            success=status == "success",
            failure_kind=failure_kind,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            **payload,
        )

    async def _emit_step(
        self,
        step: DataCleaningStepKind,
        lifecycle: Literal["start", "artifact", "issue", "end"],
        **payload: Any,
    ) -> None:
        """通过最小 sink 发出子步骤事件，不管理外层 phase 上下文。"""
        await self._event_sink.emit(f"data_cleaning.{step}.{lifecycle}", **payload)

    @staticmethod
    def _persist_failure(
        profile: DataProfile,
        plan: CleaningPlan,
        failure: VerificationFailure,
        store: M15ArtifactStore,
        *,
        status: str,
    ) -> DataIssue:
        """将一次失败状态持久化为不可覆盖的 DataIssue。"""
        issue = _build_issue(
            profile=profile,
            plan=plan,
            failure=failure,
            status=status,
        )
        store.write_json(issue)
        return issue

    @staticmethod
    def _persist_resolutions(
        profile: DataProfile,
        plan: CleaningPlan,
        unresolved: list[DataIssue],
        store: M15ArtifactStore,
        evidence: list[DataIssue],
    ) -> None:
        """验证通过后为历史失败规则追加 resolved 终态证据。"""
        latest_by_rule: dict[str, DataIssue] = {}
        for issue in unresolved:
            latest_by_rule[issue.rule_id] = issue
        for issue in latest_by_rule.values():
            resolved = _build_resolved_issue(profile, plan, issue)
            store.write_json(resolved)
            evidence.append(resolved)


def _build_issue(
    *,
    profile: DataProfile,
    plan: CleaningPlan,
    failure: VerificationFailure,
    status: str,
) -> DataIssue:
    """从确定性失败构造稳定 DataIssue。"""
    attempt = plan.repair_attempt
    identity = (f"{profile.table_id}\0{failure.rule_id}\0{attempt}\0{status}").encode(
        "utf-8"
    )
    digest = hashlib.sha256(identity).hexdigest()[:16]
    issue_id = f"data-issue:{digest}"
    return DataIssue.model_validate(
        {
            "schema_version": "m1.5",
            "artifact_id": issue_id,
            "source_artifact_ids": (profile.artifact_id, plan.artifact_id),
            "validation_status": "validated",
            "artifact_path": f"m15/issues/{digest}.json",
            "issue_id": issue_id,
            "table_id": profile.table_id,
            "rule_id": failure.rule_id,
            "expected": failure.expected,
            "actual": failure.actual,
            "evidence_summary": failure.evidence_summary,
            "repair_attempt": attempt,
            "status": status,
        }
    )


def _build_resolved_issue(
    profile: DataProfile,
    plan: CleaningPlan,
    unresolved: DataIssue,
) -> DataIssue:
    """追加而非覆盖原失败证据，表明同一规则最终通过。"""
    identity = (
        f"{profile.table_id}\0{unresolved.rule_id}\0{plan.repair_attempt}\0resolved"
    ).encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:16]
    issue_id = f"data-issue:{digest}"
    return DataIssue(
        schema_version="m1.5",
        artifact_id=issue_id,
        source_artifact_ids=(
            profile.artifact_id,
            plan.artifact_id,
            unresolved.issue_id,
        ),
        validation_status="validated",
        artifact_path=f"m15/issues/{digest}.json",
        issue_id=issue_id,
        table_id=profile.table_id,
        rule_id=unresolved.rule_id,
        expected=unresolved.expected,
        actual="passed",
        evidence_summary=(
            f"规则在第 {plan.repair_attempt} 次 Repair 后通过全部表级验证。"
        ),
        repair_attempt=plan.repair_attempt,
        status="resolved",
    )


def _discard_cleaned_output(work_dir: str | Path, table_id: str) -> None:
    """移除未通过验证的 cleaned 文件，不触碰源文件或其他表产物。"""
    try:
        path = resolve_work_dir_path(work_dir, cleaned_table_path(table_id))
        path.unlink(missing_ok=True)
    except (ArtifactStorageError, OSError):
        # 清理失败不能覆盖原始、具有结构化证据的清洗异常。
        return


def _step_id(
    step: DataCleaningStepKind,
    table_id: str,
    attempt: int,
    sequence: int = 0,
) -> str:
    """按步骤、表、attempt 和轮次生成可复现且不含路径细节的标识。"""
    identity = f"{step}\0{table_id}\0{attempt}\0{sequence}".encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:16]
    return f"data-cleaning-{step}:{digest}"
