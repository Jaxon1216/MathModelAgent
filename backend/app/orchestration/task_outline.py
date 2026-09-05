"""TaskFacts、TaskOutline 与依赖调度的编排边界。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.agents.coordinator_agent import (
    CoordinatorAgent,
    CoordinatorResponseError,
)
from app.data.artifact_store import ArtifactStorageError, M15ArtifactStore
from app.data.preparation import (
    DataCatalogInspectionError,
    build_task_facts,
    inspect_data_catalog,
)
from app.domain.m15 import QuestionId, TaskFacts, TaskOutline
from app.domain.problem import DataCatalog, QuestionSet
from app.runtime.tracing import NullStageTracer, StageTracer
from pydantic import ValidationError


@dataclass(frozen=True)
class ScheduleLayer:
    """同一依赖深度的一组问题，仅表示并行候选关系。"""

    index: int
    question_ids: tuple[str, ...]
    parallel_candidate: bool


@dataclass(frozen=True)
class TaskSchedule:
    """由 TaskOutline 唯一派生的稳定拓扑调度计划。"""

    layers: tuple[ScheduleLayer, ...]
    execution_order: tuple[str, ...]

    @property
    def ques_count(self) -> int:
        """返回由执行计划覆盖的问题数量。"""
        return len(self.execution_order)


@dataclass(frozen=True)
class TaskOutlineStageResult:
    """Coordinator 阶段产物、附件发现结果和派生调度。"""

    task_facts: TaskFacts
    outline: TaskOutline
    schedule: TaskSchedule
    data_catalog: DataCatalog

    def to_question_set(self) -> QuestionSet:
        """生成旧 Modeler 仍可消费的问题集合。"""
        return QuestionSet(
            question_count=self.outline.ques_count,
            questions={
                question.question_id: question.text
                for question in self.outline.questions
            },
            background=self.task_facts.problem_text,
        )

    def to_legacy_questions(self) -> dict[str, str | int]:
        """生成旧 Flows 和 Writer 仍可消费的扁平字典。"""
        return {
            "background": self.task_facts.problem_text,
            "ques_count": self.outline.ques_count,
            **{
                question.question_id: question.text
                for question in self.outline.questions
            },
        }


class TaskOutlineStageError(RuntimeError):
    """TaskOutline 编排阶段失败，携带稳定失败分类。"""

    def __init__(
        self,
        failure_kind: str,
        detail: str,
        *,
        phase: str = "task_outline",
        attempts: int = 0,
    ) -> None:
        self.phase = phase
        self.failure_kind = failure_kind
        self.detail = detail
        self.attempts = attempts
        super().__init__(f"{failure_kind}: {detail}")


class TaskOutlineWorkflow:
    """从 API 输入和附件发现结果构造、持久化并调度 TaskOutline。"""

    _FACTS_PHASE = "task_facts"
    _OUTLINE_PHASE = "task_outline"

    def __init__(
        self,
        agent: CoordinatorAgent,
        *,
        tracer: StageTracer | None = None,
    ) -> None:
        self._agent = agent
        self._tracer = tracer or NullStageTracer()

    async def create_outline(
        self,
        *,
        task_id: str,
        problem_text: str,
        work_dir: str | Path,
    ) -> TaskOutlineStageResult:
        """运行附件发现、事实冻结、Coordinator 和依赖调度。"""
        data_catalog, task_facts = await self._create_task_facts(
            task_id=task_id,
            problem_text=problem_text,
            work_dir=work_dir,
        )
        outline, schedule = await self._create_task_outline(
            task_id=task_id,
            work_dir=work_dir,
            task_facts=task_facts,
        )
        return TaskOutlineStageResult(
            task_facts=task_facts,
            outline=outline,
            schedule=schedule,
            data_catalog=data_catalog,
        )

    async def _create_task_facts(
        self,
        *,
        task_id: str,
        problem_text: str,
        work_dir: str | Path,
    ) -> tuple[DataCatalog, TaskFacts]:
        """发现附件并以独立阶段冻结只读 TaskFacts。"""
        phase = self._FACTS_PHASE
        started_at = time.monotonic()
        success = False
        failure_kind: str | None = None
        await self._tracer.start(task_id, phase)
        try:
            data_catalog = inspect_data_catalog(work_dir)
            task_facts = build_task_facts(
                task_id=task_id,
                problem_text=problem_text,
                work_dir=work_dir,
                data_catalog=data_catalog,
            )
            store = M15ArtifactStore(work_dir)
            store.write_json(task_facts)
            await self._tracer.event(
                task_id,
                "task_facts.artifact",
                phase=phase,
                artifact_type=task_facts.artifact_type,
                schema_version=task_facts.schema_version,
                artifact_id=task_facts.artifact_id,
                validation_status=task_facts.validation_status,
                artifact_path=task_facts.artifact_path,
                attachment_count=len(task_facts.attachments),
                output_template_count=len(task_facts.output_templates),
            )
            success = True
            return data_catalog, task_facts
        except asyncio.CancelledError:
            failure_kind = "cancelled"
            raise
        except DataCatalogInspectionError as exc:
            failure_kind = "profile"
            raise TaskOutlineStageError(
                failure_kind,
                str(exc),
                phase=phase,
            ) from exc
        except ArtifactStorageError as exc:
            failure_kind = "integrity"
            raise TaskOutlineStageError(
                failure_kind,
                str(exc),
                phase=phase,
            ) from exc
        except (ValidationError, ValueError) as exc:
            failure_kind = "schema"
            raise TaskOutlineStageError(
                failure_kind,
                str(exc),
                phase=phase,
            ) from exc
        except Exception as exc:
            failure_kind = "profile"
            raise TaskOutlineStageError(
                failure_kind,
                f"{type(exc).__name__}: {exc}",
                phase=phase,
            ) from exc
        finally:
            await self._tracer.end(
                task_id,
                phase,
                success=success,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                failure_kind=failure_kind,
            )

    async def _create_task_outline(
        self,
        *,
        task_id: str,
        work_dir: str | Path,
        task_facts: TaskFacts,
    ) -> tuple[TaskOutline, TaskSchedule]:
        """调用 Coordinator 并以独立阶段发布稳定拓扑计划。"""
        phase = self._OUTLINE_PHASE
        started_at = time.monotonic()
        success = False
        failure_kind: str | None = None
        await self._tracer.start(task_id, phase)
        try:
            outline = await self._agent.run(task_facts)
            store = M15ArtifactStore(work_dir)
            store.write_json(outline)
            schedule = build_task_schedule(outline)
            await self._tracer.event(
                task_id,
                "task_outline.artifact",
                phase=phase,
                artifact_type=outline.artifact_type,
                schema_version=outline.schema_version,
                artifact_id=outline.artifact_id,
                validation_status=outline.validation_status,
                artifact_path=outline.artifact_path,
                question_count=outline.ques_count,
                layer_count=len(schedule.layers),
                execution_order=list(schedule.execution_order),
            )
            success = True
            return outline, schedule
        except asyncio.CancelledError:
            failure_kind = "cancelled"
            raise
        except ArtifactStorageError as exc:
            failure_kind = "integrity"
            raise TaskOutlineStageError(
                failure_kind,
                str(exc),
                phase=phase,
            ) from exc
        except CoordinatorResponseError as exc:
            failure_kind = "schema"
            raise TaskOutlineStageError(
                failure_kind,
                f"{exc.kind}: {exc.detail}",
                phase=phase,
                attempts=exc.attempts,
            ) from exc
        except (ValidationError, ValueError) as exc:
            failure_kind = "schema"
            raise TaskOutlineStageError(
                failure_kind,
                str(exc),
                phase=phase,
            ) from exc
        except Exception as exc:
            failure_kind = "provider"
            raise TaskOutlineStageError(
                failure_kind,
                f"{type(exc).__name__}: {exc}",
                phase=phase,
            ) from exc
        finally:
            await self._tracer.end(
                task_id,
                phase,
                success=success,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                failure_kind=failure_kind,
            )


def build_task_schedule(outline: TaskOutline) -> TaskSchedule:
    """按 order_key 稳定打破同层顺序，构造 Kahn 拓扑层。

    Args:
        outline: 已通过 DAG 校验的 TaskOutline。

    Returns:
        同层可并行、跨层有依赖的稳定调度计划。

    Raises:
        ValueError: 防御性检查发现依赖图不完整或含环。
    """
    question_by_id = {question.question_id: question for question in outline.questions}
    indegree = {
        question.question_id: len(question.depends_on) for question in outline.questions
    }
    dependants: dict[QuestionId, list[QuestionId]] = {
        question.question_id: [] for question in outline.questions
    }
    for question in outline.questions:
        for dependency_id in question.depends_on:
            if dependency_id not in dependants:
                raise ValueError(
                    f"{question.question_id} 包含未知依赖: {dependency_id}"
                )
            dependants[dependency_id].append(question.question_id)

    def stable_key(question_id: str) -> tuple[int, str]:
        question = question_by_id[question_id]
        return question.order_key, question.question_id

    ready = sorted(
        (question_id for question_id, degree in indegree.items() if degree == 0),
        key=stable_key,
    )
    layers: list[ScheduleLayer] = []
    execution_order: list[str] = []
    while ready:
        current_layer = tuple(ready)
        layers.append(
            ScheduleLayer(
                index=len(layers),
                question_ids=current_layer,
                parallel_candidate=len(current_layer) > 1,
            )
        )
        execution_order.extend(current_layer)

        next_ready: list[str] = []
        for question_id in current_layer:
            for dependant_id in dependants[question_id]:
                indegree[dependant_id] -= 1
                if indegree[dependant_id] == 0:
                    next_ready.append(dependant_id)
        ready = sorted(next_ready, key=stable_key)

    if len(execution_order) != outline.ques_count:
        raise ValueError("TaskOutline 依赖图包含环或未覆盖全部问题")
    return TaskSchedule(
        layers=tuple(layers),
        execution_order=tuple(execution_order),
    )
