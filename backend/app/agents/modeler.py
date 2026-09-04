"""基于领域计划契约的 Modeler Agent。"""

from __future__ import annotations

from app.domain.model_plan import (
    ModelPlan,
    ModelPlanValidationError,
    PlanValidation,
)
from app.domain.problem import Problem
from app.prompts.modeler import (
    get_modeler_repair_prompt,
    get_modeler_request,
    get_modeler_system_prompt,
)
from app.runtime.llm.client import ChatMessage, LLMClient


class ModelerResponseError(RuntimeError):
    """有限修复后仍无法获得合法 ModelPlan。"""

    def __init__(self, errors: tuple[str, ...], attempts: int) -> None:
        self.errors = errors
        self.attempts = attempts
        super().__init__(
            f"Modeler plan validation failed after {attempts} attempts: "
            f"{'; '.join(errors)}"
        )


class ModelerAgent:
    """仅接收领域问题并返回领域建模计划。"""

    def __init__(
        self,
        client: LLMClient,
        *,
        max_repair_attempts: int = 3,
    ) -> None:
        if max_repair_attempts < 0:
            raise ValueError("max_repair_attempts 不能小于 0")
        self._client = client
        self._max_repair_attempts = max_repair_attempts

    async def run(self, problem: Problem) -> ModelPlan:
        """生成计划，并按配置对契约错误进行有限修复。

        Args:
            problem: 题目、问题集合和已验证数据目录。

        Returns:
            已通过 Pydantic 和领域引用校验的 ModelPlan。

        Raises:
            ModelerResponseError: 初始响应和配置的修复尝试均不合法。
        """
        validator = PlanValidation.for_problem(problem)
        base_messages = self._base_messages(problem)
        errors: tuple[str, ...] = ()

        max_attempts = 1 + self._max_repair_attempts
        for attempt in range(1, max_attempts + 1):
            messages = list(base_messages)
            if errors:
                messages.append(
                    {
                        "role": "user",
                        "content": get_modeler_repair_prompt(errors),
                    }
                )

            response_text = await self._client.complete(messages)
            try:
                return validator.parse_json(response_text)
            except ModelPlanValidationError as exc:
                errors = exc.errors

        raise ModelerResponseError(errors, attempts=max_attempts)

    @staticmethod
    def _base_messages(problem: Problem) -> list[ChatMessage]:
        """构造每次请求共用的无状态上下文。"""
        return [
            {"role": "system", "content": get_modeler_system_prompt()},
            {"role": "user", "content": get_modeler_request(problem)},
        ]
