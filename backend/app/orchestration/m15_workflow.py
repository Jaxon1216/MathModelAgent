"""M1.5 数据可靠性主链编排与稳定失败边界。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import ValidationError

from app.agents.cleaning_planner import CleaningPlanResponseError, CleaningPlannerAgent
from app.agents.modeler import ModelerAgent, ModelerResponseError
from app.data.artifact_store import ArtifactStorageError, M15ArtifactStore
from app.data.contracts import (
    DataContractFreezeError,
    DataContractIntegrityError,
    freeze_data_contract,
    validate_data_contract_integrity,
)
from app.data.profiling import DataProfileInspectionError, inspect_data_profiles
from app.domain.m15 import (
    DataContract,
    DataIssue,
    DataProfile,
    TaskFacts,
    TaskOutline,
    UpstreamResultReference,
    VersionedArtifact,
)
from app.domain.model_plan import QuestionPlanCollection
from app.orchestration.data_cleaning import (
    DataCleaningBlockedError,
    DataCleaningEventSink,
    DataCleaningResult,
    DataCleaningWorkflow,
)
from app.runtime.llm.client import LLMClientError
from app.runtime.tracing import NullStageTracer, StageTracer

M15FailureKind = Literal[
    "schema",
    "profile",
    "clean",
    "verify",
    "repair",
    "integrity",
    "provider",
    "cancelled",
]
CancelCheck = Callable[[], Awaitable[None]]


class M15StageError(RuntimeError):
    """M1.5 阶段失败，携带稳定分类和已持久化问题证据。"""

    def __init__(
        self,
        phase: str,
        failure_kind: M15FailureKind,
        detail: str,
        *,
        issue_ids: tuple[str, ...] = (),
        attempts: int = 0,
    ) -> None:
        self.phase = phase
        self.failure_kind = failure_kind
        self.detail = detail
        self.issue_ids = issue_ids
        self.attempts = attempts
        super().__init__(f"{phase}/{failure_kind}: {detail}")


@dataclass(frozen=True)
class M15WorkflowResult:
    """可交给旧 Coder 的完整 M1.5 成功终态。"""

    profiles: tuple[DataProfile, ...]
    cleaning: DataCleaningResult
    data_contract: DataContract
    question_plans: QuestionPlanCollection
    coder_handoff: dict[str, str]


class _StageTracerDataCleaningEventSink(DataCleaningEventSink):
    """将清洗子步骤事件绑定到现有 data_cleaning phase。"""

    def __init__(self, task_id: str, tracer: StageTracer) -> None:
        self._task_id = task_id
        self._tracer = tracer

    async def emit(self, event: str, **payload: Any) -> None:
        """只透传普通事件，不嵌套管理 LegacyStageTracer 上下文。"""
        await self._tracer.event(
            self._task_id,
            event,
            phase="data_cleaning",
            **payload,
        )


class M15Workflow:
    """按固定顺序生成数据契约、问题计划及唯一旧 Coder 交接。"""

    def __init__(
        self,
        cleaning_planner: CleaningPlannerAgent,
        modeler: ModelerAgent,
        *,
        tracer: StageTracer | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> None:
        self._cleaning_planner = cleaning_planner
        self._modeler = modeler
        self._tracer = tracer or NullStageTracer()
        self._cancel_check = cancel_check or _noop_cancel_check

    async def run(
        self,
        *,
        task_id: str,
        work_dir: str | Path,
        task_facts: TaskFacts,
        outline: TaskOutline,
    ) -> M15WorkflowResult:
        """执行 DataProfile 到 QuestionPlan 的全部阻断式阶段。"""
        profiles = await self._profile(
            task_id=task_id,
            work_dir=work_dir,
            outline=outline,
        )
        cleaning = await self._clean(
            task_id=task_id,
            work_dir=work_dir,
            outline=outline,
            profiles=profiles,
        )
        contract = await self._freeze_contract(
            task_id=task_id,
            work_dir=work_dir,
            task_facts=task_facts,
            profiles=profiles,
            cleaning=cleaning,
        )
        question_plans, coder_handoff = await self._plan_questions(
            task_id=task_id,
            work_dir=work_dir,
            outline=outline,
            contract=contract,
        )
        return M15WorkflowResult(
            profiles=profiles,
            cleaning=cleaning,
            data_contract=contract,
            question_plans=question_plans,
            coder_handoff=coder_handoff,
        )

    async def _profile(
        self,
        *,
        task_id: str,
        work_dir: str | Path,
        outline: TaskOutline,
    ) -> tuple[DataProfile, ...]:
        """生成全部只读画像，并拒绝部分成功。"""
        phase = "data_profile"
        started_at = time.monotonic()
        success = False
        failure_kind: M15FailureKind | None = None
        await self._tracer.start(task_id, phase)
        try:
            await self._cancel_check()
            profiles = inspect_data_profiles(work_dir, outline.artifact_id)
            for profile in profiles:
                await self._emit_artifact(task_id, phase, profile)
            if not profiles:
                await self._tracer.event(
                    task_id,
                    "data_profile.artifact",
                    phase=phase,
                    artifact_type="data_profile_set",
                    table_count=0,
                    validation_status="validated",
                )
            await self._cancel_check()
            success = True
            return profiles
        except asyncio.CancelledError:
            failure_kind = "cancelled"
            raise
        except DataProfileInspectionError as exc:
            failure_kind = "profile"
            await self._emit_artifact(task_id, phase, exc.issue)
            raise M15StageError(
                phase,
                failure_kind,
                str(exc),
                issue_ids=(exc.issue.issue_id,),
            ) from exc
        except (ArtifactStorageError, ValidationError, ValueError) as exc:
            failure_kind = "schema"
            raise M15StageError(phase, failure_kind, str(exc)) from exc
        except Exception as exc:
            failure_kind = "profile"
            raise M15StageError(
                phase,
                failure_kind,
                f"{type(exc).__name__}: {exc}",
            ) from exc
        finally:
            await self._end_stage(
                task_id,
                phase,
                started_at,
                success=success,
                failure_kind=failure_kind,
            )

    async def _clean(
        self,
        *,
        task_id: str,
        work_dir: str | Path,
        outline: TaskOutline,
        profiles: tuple[DataProfile, ...],
    ) -> DataCleaningResult:
        """生成计划、执行清洗、确定性验证并完成局部 Repair。"""
        phase = "data_cleaning"
        started_at = time.monotonic()
        success = False
        failure_kind: M15FailureKind | None = None
        await self._tracer.start(task_id, phase)
        try:
            await self._cancel_check()
            result = await DataCleaningWorkflow(
                self._cleaning_planner,
                event_sink=_StageTracerDataCleaningEventSink(
                    task_id,
                    self._tracer,
                ),
            ).run(
                work_dir=work_dir,
                outline=outline,
                profiles=profiles,
            )
            for plan in result.plans:
                await self._emit_artifact(task_id, phase, plan)
            for issue in result.issues:
                await self._emit_artifact(task_id, phase, issue)
            for cleaned in result.cleaned_tables:
                await self._tracer.event(
                    task_id,
                    "data_cleaning.artifact",
                    phase=phase,
                    artifact_type="cleaned_table",
                    table_id=cleaned.table_id,
                    path=cleaned.relative_path,
                    sha256=cleaned.sha256,
                    row_count=cleaned.row_count,
                )
            if not result.plans:
                await self._tracer.event(
                    task_id,
                    "data_cleaning.artifact",
                    phase=phase,
                    artifact_type="cleaning_result",
                    table_count=0,
                    issue_count=0,
                    validation_status="validated",
                )
            unresolved = tuple(
                issue
                for issue in result.issues
                if issue.status in {"unresolved", "exhausted"}
                and not _has_later_resolution(issue, result.issues)
            )
            if unresolved:
                raise DataCleaningBlockedError(
                    result.issues,
                    failure_kind="verify",
                )
            await self._cancel_check()
            success = True
            return result
        except asyncio.CancelledError:
            failure_kind = "cancelled"
            raise
        except DataCleaningBlockedError as exc:
            failure_kind = _cleaning_failure_kind(exc)
            for issue in _unique_issues(exc.issues):
                await self._emit_artifact(task_id, phase, issue)
            raise M15StageError(
                phase,
                failure_kind,
                str(exc),
                issue_ids=tuple(issue.issue_id for issue in exc.issues),
                attempts=max(
                    (issue.repair_attempt for issue in exc.issues),
                    default=0,
                ),
            ) from exc
        except LLMClientError as exc:
            failure_kind = "provider"
            raise M15StageError(phase, failure_kind, exc.detail) from exc
        except CleaningPlanResponseError as exc:
            failure_kind = "schema"
            raise M15StageError(
                phase,
                failure_kind,
                str(exc),
                attempts=exc.attempts,
            ) from exc
        except (ArtifactStorageError, ValidationError, ValueError) as exc:
            failure_kind = "schema"
            raise M15StageError(phase, failure_kind, str(exc)) from exc
        except Exception as exc:
            failure_kind = "clean"
            raise M15StageError(
                phase,
                failure_kind,
                f"{type(exc).__name__}: {exc}",
            ) from exc
        finally:
            await self._end_stage(
                task_id,
                phase,
                started_at,
                success=success,
                failure_kind=failure_kind,
            )

    async def _freeze_contract(
        self,
        *,
        task_id: str,
        work_dir: str | Path,
        task_facts: TaskFacts,
        profiles: tuple[DataProfile, ...],
        cleaning: DataCleaningResult,
    ) -> DataContract:
        """仅从完整成功的数据产物冻结契约。"""
        phase = "data_contract"
        started_at = time.monotonic()
        success = False
        failure_kind: M15FailureKind | None = None
        await self._tracer.start(task_id, phase)
        try:
            await self._cancel_check()
            contract = freeze_data_contract(
                work_dir,
                task_facts=task_facts,
                profiles=profiles,
                plans=cleaning.plans,
                cleaned_tables=cleaning.cleaned_tables,
                issues=cleaning.issues,
            )
            await self._emit_artifact(task_id, phase, contract)
            await self._cancel_check()
            success = True
            return contract
        except asyncio.CancelledError:
            failure_kind = "cancelled"
            raise
        except DataContractIntegrityError as exc:
            failure_kind = "integrity"
            for issue in exc.issues:
                await self._emit_artifact(task_id, phase, issue)
            raise M15StageError(
                phase,
                failure_kind,
                str(exc),
                issue_ids=tuple(issue.issue_id for issue in exc.issues),
            ) from exc
        except (DataContractFreezeError, ArtifactStorageError) as exc:
            failure_kind = "integrity"
            raise M15StageError(phase, failure_kind, str(exc)) from exc
        except (ValidationError, ValueError) as exc:
            failure_kind = "schema"
            raise M15StageError(phase, failure_kind, str(exc)) from exc
        except Exception as exc:
            failure_kind = "integrity"
            raise M15StageError(
                phase,
                failure_kind,
                f"{type(exc).__name__}: {exc}",
            ) from exc
        finally:
            await self._end_stage(
                task_id,
                phase,
                started_at,
                success=success,
                failure_kind=failure_kind,
            )

    async def _plan_questions(
        self,
        *,
        task_id: str,
        work_dir: str | Path,
        outline: TaskOutline,
        contract: DataContract,
    ) -> tuple[QuestionPlanCollection, dict[str, str]]:
        """在冻结契约完整性通过后生成完整按题计划。"""
        phase = "question_plan"
        started_at = time.monotonic()
        success = False
        failure_kind: M15FailureKind | None = None
        await self._tracer.start(task_id, phase)
        try:
            await self._cancel_check()
            validate_data_contract_integrity(work_dir, contract)
            plans = await self._modeler.run(
                outline,
                contract,
                work_dir=work_dir,
                upstream_result_declarations=_upstream_declarations(outline),
            )
            store = M15ArtifactStore(work_dir)
            for plan in plans.question_plans:
                persisted = store.read_json(plan.artifact_path, type(plan))
                if persisted != plan:
                    raise ArtifactStorageError(
                        f"QuestionPlan 与已落盘内容不一致: {plan.question_id}"
                    )
            validate_data_contract_integrity(work_dir, contract)
            coder_handoff = plans.to_coder_handoff(work_dir, contract)
            for plan in plans.question_plans:
                await self._emit_artifact(task_id, phase, plan)
            await self._cancel_check()
            success = True
            return plans, coder_handoff
        except asyncio.CancelledError:
            failure_kind = "cancelled"
            raise
        except DataContractIntegrityError as exc:
            failure_kind = "integrity"
            for issue in exc.issues:
                await self._emit_artifact(task_id, phase, issue)
            raise M15StageError(
                phase,
                failure_kind,
                str(exc),
                issue_ids=tuple(issue.issue_id for issue in exc.issues),
            ) from exc
        except LLMClientError as exc:
            failure_kind = "provider"
            raise M15StageError(phase, failure_kind, exc.detail) from exc
        except ModelerResponseError as exc:
            failure_kind = "schema"
            raise M15StageError(
                phase,
                failure_kind,
                str(exc),
                attempts=exc.attempts,
            ) from exc
        except ArtifactStorageError as exc:
            failure_kind = "integrity"
            raise M15StageError(phase, failure_kind, str(exc)) from exc
        except (ValidationError, ValueError) as exc:
            failure_kind = "schema"
            raise M15StageError(phase, failure_kind, str(exc)) from exc
        except Exception as exc:
            failure_kind = "schema"
            raise M15StageError(
                phase,
                failure_kind,
                f"{type(exc).__name__}: {exc}",
            ) from exc
        finally:
            await self._end_stage(
                task_id,
                phase,
                started_at,
                success=success,
                failure_kind=failure_kind,
            )

    async def _emit_artifact(
        self,
        task_id: str,
        phase: str,
        artifact: VersionedArtifact,
    ) -> None:
        """记录不含整表内容或模型长文本的统一产物事件。"""
        await self._tracer.event(
            task_id,
            f"{phase}.artifact",
            phase=phase,
            artifact_type=getattr(artifact, "artifact_type", type(artifact).__name__),
            schema_version=artifact.schema_version,
            artifact_id=artifact.artifact_id,
            validation_status=artifact.validation_status,
            artifact_path=artifact.artifact_path,
        )

    async def _end_stage(
        self,
        task_id: str,
        phase: str,
        started_at: float,
        *,
        success: bool,
        failure_kind: M15FailureKind | None,
    ) -> None:
        """为每次 start 写入唯一终态。"""
        await self._tracer.end(
            task_id,
            phase,
            success=success,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            failure_kind=failure_kind,
        )


async def validate_contract_boundary(
    *,
    task_id: str,
    work_dir: str | Path,
    contract: DataContract,
    phase: str,
    boundary: Literal["before", "after_coder", "after"],
    tracer: StageTracer | None = None,
) -> None:
    """在每个下游 phase 边界复核契约及全部来源/cleaned 指纹。"""
    validate_data_contract_integrity(work_dir, contract)
    active_tracer = tracer or NullStageTracer()
    await active_tracer.event(
        task_id,
        "data_contract.integrity",
        phase=phase,
        boundary=boundary,
        artifact_id=contract.artifact_id,
        status=contract.status,
        table_count=len(contract.tables),
    )


def _upstream_declarations(
    outline: TaskOutline,
) -> tuple[UpstreamResultReference, ...]:
    """从被依赖问题的显式交付物派生可供计划引用的结果声明。"""
    dependency_ids = {
        dependency_id
        for question in outline.questions
        for dependency_id in question.depends_on
    }
    return tuple(
        UpstreamResultReference(
            question_id=question.question_id,
            outputs=tuple(
                deliverable.deliverable_id for deliverable in question.deliverables
            ),
        )
        for question in outline.questions
        if question.question_id in dependency_ids
    )


def _cleaning_failure_kind(
    error: DataCleaningBlockedError,
) -> Literal["clean", "verify", "repair"]:
    """将 Task 4 阻断映射到稳定的公开失败分类。"""
    if error.failure_kind in {"clean", "verify", "repair"}:
        return cast(Literal["clean", "verify", "repair"], error.failure_kind)
    return "verify"


def _has_later_resolution(
    candidate: DataIssue,
    issues: tuple[DataIssue, ...],
) -> bool:
    """判断历史失败是否已有同表同规则且不早于它的 resolved 证据。"""
    return any(
        issue.table_id == candidate.table_id
        and issue.rule_id == candidate.rule_id
        and issue.status == "resolved"
        and issue.repair_attempt >= candidate.repair_attempt
        for issue in issues
    )


def _unique_issues(issues: tuple[DataIssue, ...]) -> tuple[DataIssue, ...]:
    """按 issue_id 稳定去重 trace 事件。"""
    seen: set[str] = set()
    result: list[DataIssue] = []
    for issue in issues:
        if issue.issue_id not in seen:
            seen.add(issue.issue_id)
            result.append(issue)
    return tuple(result)


async def _noop_cancel_check() -> None:
    """默认取消检查不做任何操作。"""
