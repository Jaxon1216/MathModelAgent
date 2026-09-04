"""Modeler 阶段的调度边界。"""

from __future__ import annotations

from app.agents.modeler import ModelerAgent, ModelerResponseError
from app.domain.model_plan import ModelPlan
from app.domain.problem import Problem


class ModelerStageError(RuntimeError):
    """编排层记录的 Modeler 阶段失败。"""


class ModelerWorkflow:
    """只决定 Modeler 阶段的成功或停止，不处理提示词或 JSON。"""

    def __init__(self, agent: ModelerAgent) -> None:
        self._agent = agent

    async def create_plan(self, problem: Problem) -> ModelPlan:
        """运行 Modeler，并将领域失败暴露为阶段失败。"""
        try:
            return await self._agent.run(problem)
        except ModelerResponseError as exc:
            raise ModelerStageError(
                f"Modeler stage failed after {exc.attempts} attempts: {exc}"
            ) from exc
