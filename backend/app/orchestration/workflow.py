"""Modeler 阶段的调度边界。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.agents.modeler import ModelerAgent, ModelerResponseError
from app.data.preparation import DataCatalogInspectionError, inspect_data_catalog
from app.domain.model_plan import ModelPlan
from app.domain.problem import Problem, QuestionSet
from app.runtime.llm.client import LLMClientError
from app.runtime.tracing import NullStageTracer, StageTracer


class ModelerStageError(RuntimeError):
    """编排层记录的 Modeler 阶段失败。"""

    def __init__(
        self,
        failure_kind: str,
        detail: str,
        *,
        attempts: int = 0,
    ) -> None:
        self.failure_kind = failure_kind
        self.detail = detail
        self.attempts = attempts
        super().__init__(f"{failure_kind}: {detail}")


@dataclass(frozen=True)
class ModelerStageResult:
    """Modeler 阶段产物及其真实输入。"""

    problem: Problem
    plan: ModelPlan


class ModelerWorkflow:
    """只决定 Modeler 阶段的成功或停止，不处理提示词或 JSON。"""

    _PHASE = "modeler"

    def __init__(
        self,
        agent: ModelerAgent,
        *,
        tracer: StageTracer | None = None,
    ) -> None:
        self._agent = agent
        self._tracer = tracer or NullStageTracer()

    async def create_plan(self, problem: Problem) -> ModelPlan:
        """运行 Modeler，并将领域失败暴露为阶段失败。"""
        result = await self._run(problem.task_id, lambda: problem)
        return result.plan

    async def create_plan_from_work_dir(
        self,
        *,
        task_id: str,
        question_set: QuestionSet,
        work_dir: str | Path,
    ) -> ModelerStageResult:
        """扫描真实附件并在同一 phase 内生成计划。"""
        return await self._run(
            task_id,
            lambda: Problem(
                task_id=task_id,
                question_set=question_set,
                data_catalog=inspect_data_catalog(work_dir),
            ),
        )

    async def _run(
        self,
        task_id: str,
        problem_factory: Callable[[], Problem],
    ) -> ModelerStageResult:
        """在统一 trace 和异常边界内构造输入并调用 Agent。"""
        started_at = time.monotonic()
        success = False
        failure_kind: str | None = None
        await self._tracer.start(task_id, self._PHASE)
        try:
            problem = problem_factory()
            await self._tracer.event(
                task_id,
                "modeler.input_catalog",
                phase=self._PHASE,
                input_table_count=len(problem.data_catalog.input_tables),
                output_template_count=len(problem.data_catalog.output_templates),
                table_ids=[
                    table.table_id for table in problem.data_catalog.input_tables
                ],
            )
            plan = await self._agent.run(problem)
            success = True
            return ModelerStageResult(problem=problem, plan=plan)
        except asyncio.CancelledError:
            failure_kind = "cancelled"
            raise
        except DataCatalogInspectionError as exc:
            failure_kind = "input_catalog"
            raise ModelerStageError(failure_kind, str(exc)) from exc
        except LLMClientError as exc:
            failure_kind = f"llm_{exc.kind}"
            raise ModelerStageError(failure_kind, exc.detail) from exc
        except ModelerResponseError as exc:
            failure_kind = "invalid_response"
            raise ModelerStageError(
                failure_kind,
                str(exc),
                attempts=exc.attempts,
            ) from exc
        finally:
            await self._tracer.end(
                task_id,
                self._PHASE,
                success=success,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                failure_kind=failure_kind,
            )
