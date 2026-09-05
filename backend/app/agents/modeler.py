"""基于领域计划契约的 Modeler Agent。"""

from __future__ import annotations

from pathlib import Path

from app.data.artifact_store import M15ArtifactStore
from app.data.contracts import validate_data_contract_integrity
from app.domain.m15 import DataContract, TaskOutline, UpstreamResultReference
from app.domain.model_plan import (
    ModelPlan,
    ModelPlanValidationError,
    PlanValidation,
    QuestionPlanCollection,
    QuestionPlanValidation,
    QuestionPlanValidationError,
)
from app.domain.problem import Problem
from app.prompts.modeler import (
    get_modeler_repair_prompt,
    get_modeler_request,
    get_modeler_system_prompt,
    get_question_plan_repair_prompt,
    get_question_plan_request,
    get_question_plan_system_prompt,
)
from app.runtime.llm.client import ChatMessage, LLMClient


class ModelerResponseError(RuntimeError):
    """有限修复后仍无法获得合法 ModelPlan 或 QuestionPlan 集合。"""

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

    async def run(
        self,
        outline: TaskOutline,
        data_contract: DataContract,
        *,
        work_dir: str | Path,
        upstream_result_declarations: tuple[UpstreamResultReference, ...] = (),
    ) -> QuestionPlanCollection:
        """从 M1.5 允许输入生成、校验并持久化 QuestionPlan 集合。

        Args:
            outline: 已验证的全局任务骨架。
            data_contract: 已冻结或显式 no-data 的数据契约。
            work_dir: 契约及计划所在任务工作目录。
            upstream_result_declarations: outline 依赖可使用的结果声明。

        Returns:
            严格覆盖 outline 且已落盘的 QuestionPlan 集合。

        Raises:
            DataContractIntegrityError: 契约或 cleaned 文件发生漂移。
            ModelerResponseError: 初始响应和配置的修复尝试均不合法。
        """
        validate_data_contract_integrity(work_dir, data_contract)
        validator = QuestionPlanValidation.for_inputs(
            outline,
            data_contract,
            upstream_result_declarations,
        )
        base_messages = [
            {"role": "system", "content": get_question_plan_system_prompt()},
            {
                "role": "user",
                "content": get_question_plan_request(
                    outline,
                    data_contract,
                    upstream_result_declarations,
                ),
            },
        ]
        errors: tuple[str, ...] = ()

        max_attempts = 1 + self._max_repair_attempts
        for _attempt in range(1, max_attempts + 1):
            messages = list(base_messages)
            if errors:
                messages.append(
                    {
                        "role": "user",
                        "content": get_question_plan_repair_prompt(errors),
                    }
                )

            response_text = await self._client.complete(messages)
            try:
                collection = validator.parse_json(response_text)
            except QuestionPlanValidationError as exc:
                errors = exc.errors
                continue

            store = M15ArtifactStore(work_dir)
            for plan in collection.question_plans:
                store.write_json(plan)
            return collection

        raise ModelerResponseError(errors, attempts=max_attempts)

    async def run_legacy(self, problem: Problem) -> ModelPlan:
        """在 Task 7 接线前保留现有主工作流的 M1 兼容入口。

        Args:
            problem: 旧主工作流构造的题目与表头级数据目录。

        Returns:
            已通过原 M1 契约校验的 ModelPlan。

        Raises:
            ModelerResponseError: 初始响应和配置的修复尝试均不合法。
        """
        validator = PlanValidation.for_problem(problem)
        base_messages = self._legacy_base_messages(problem)
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
    def _legacy_base_messages(problem: Problem) -> list[ChatMessage]:
        """构造旧 M1 请求共用的无状态上下文。"""
        return [
            {"role": "system", "content": get_modeler_system_prompt()},
            {"role": "user", "content": get_modeler_request(problem)},
        ]
