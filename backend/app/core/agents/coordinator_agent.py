"""协调者 Agent：从只读任务事实生成经校验的 TaskOutline。"""

from __future__ import annotations

import asyncio
from pydantic import ValidationError

from app.core.agents.agent import Agent
from app.core.llm.llm import LLM
from app.core.prompts import COORDINATOR_PROMPT
from app.domain.m15 import Deliverable, TaskFacts, TaskOutline
from app.utils.log_util import logger

MAX_OUTLINE_REPAIR_ATTEMPTS = 2
TASK_OUTLINE_ARTIFACT_ID = "task-outline:root"
TASK_OUTLINE_ARTIFACT_PATH = "m15/task_outline.json"


class CoordinatorResponseError(ValueError):
    """Coordinator 在有限修复后仍未给出合法 TaskOutline。"""

    def __init__(self, kind: str, detail: str, attempts: int) -> None:
        self.kind = kind
        self.detail = detail
        self.attempts = attempts
        super().__init__(
            f"Coordinator TaskOutline 校验失败: {kind}: {detail}; attempts={attempts}"
        )


class CoordinatorAgent(Agent):
    """只消费 TaskFacts，并生成一次完整、可调度的任务骨架。"""

    def __init__(
        self,
        task_id: str,
        model: LLM,
        context_window: int = 128000,
        cancel_event: asyncio.Event | None = None,
        max_repair_attempts: int = MAX_OUTLINE_REPAIR_ATTEMPTS,
    ) -> None:
        super().__init__(task_id, model, context_window, cancel_event=cancel_event)
        if max_repair_attempts < 0:
            raise ValueError("max_repair_attempts 不能为负数")
        self.system_prompt = COORDINATOR_PROMPT
        self.max_repair_attempts = max_repair_attempts

    async def run(self, task_facts: TaskFacts) -> TaskOutline:  # type: ignore[reportIncompatibleMethodOverride]
        """生成并校验 TaskOutline，失败时只提供有限的结构化修复反馈。

        Args:
            task_facts: API 题目和附件发现结果形成的只读事实。

        Returns:
            已通过 schema、来源和事实一致性校验的 TaskOutline。

        Raises:
            CoordinatorResponseError: 有限修复次数耗尽后输出仍无效。
            asyncio.CancelledError: 用户取消当前任务。
        """
        if task_facts.expected_question_ids is None:
            raise CoordinatorResponseError(
                "question_boundary_unavailable",
                "题面未包含可可靠提取且从 ques1 连续编号的明确问题题头",
                0,
            )

        self.reset_history("task-outline")
        await self.append_chat_history(
            {"role": "system", "content": self.system_prompt}
        )
        await self.append_chat_history(
            {
                "role": "user",
                "content": (
                    "只读 TaskFacts：\n"
                    + task_facts.model_dump_json(indent=2)
                    + "\n请生成唯一的 TaskOutline JSON。"
                ),
            }
        )

        last_error = CoordinatorResponseError("empty_response", "响应为空", 0)
        total_attempts = self.max_repair_attempts + 1
        for attempt in range(1, total_attempts + 1):
            response = await self._chat(
                history=self.chat_history,
                agent_name=self.__class__.__name__,
            )
            raw_content = response.content or ""
            try:
                outline = parse_task_outline(raw_content, task_facts)
                logger.info(
                    "Coordinator TaskOutline 已通过校验: "
                    f"questions={outline.ques_count}, attempts={attempt}"
                )
                return outline
            except CoordinatorResponseError as exc:
                last_error = CoordinatorResponseError(exc.kind, exc.detail, attempt)
                logger.warning(
                    "Coordinator TaskOutline 校验失败 "
                    f"(尝试 {attempt}/{total_attempts}): {exc.kind}: {exc.detail}"
                )
                if attempt < total_attempts:
                    await self.append_chat_history(
                        {
                            "role": "user",
                            "content": (
                                "上次输出未通过校验。不要复述无效输出，只重新输出"
                                "完整 JSON。错误类型："
                                f"{exc.kind}；错误详情：{exc.detail}"
                            ),
                        }
                    )
        raise last_error


def parse_task_outline(raw_content: str, task_facts: TaskFacts) -> TaskOutline:
    """解析 Coordinator 文本，并对只读事实执行跨产物校验。

    Args:
        raw_content: Coordinator 返回的 JSON 文本。
        task_facts: 本轮唯一事实来源。

    Returns:
        经完整校验的 TaskOutline。

    Raises:
        CoordinatorResponseError: JSON、schema 或事实一致性无效。
    """
    json_text = _strip_json_fence(raw_content)
    if not json_text:
        raise CoordinatorResponseError("empty_response", "响应为空", 0)
    try:
        outline = TaskOutline.model_validate_json(json_text)
    except ValidationError as exc:
        error_kind = (
            "invalid_json"
            if any(error["type"] == "json_invalid" for error in exc.errors())
            else "invalid_schema"
        )
        raise CoordinatorResponseError(error_kind, str(exc), 0) from exc

    try:
        _validate_outline_against_facts(outline, task_facts)
    except ValueError as exc:
        raise CoordinatorResponseError("facts_mismatch", str(exc), 0) from exc
    return outline


def _strip_json_fence(raw_content: str) -> str:
    """仅移除包围整个响应的 Markdown JSON 代码围栏。"""
    text = raw_content.strip()
    if text.startswith("```json") and text.endswith("```"):
        return text[len("```json") : -len("```")].strip()
    if text.startswith("```") and text.endswith("```"):
        return text[len("```") : -len("```")].strip()
    return text


def _validate_outline_against_facts(
    outline: TaskOutline,
    task_facts: TaskFacts,
) -> None:
    """保证 Coordinator 不能改写事实元数据、问题原文或附件身份。"""
    expected_metadata = (
        outline.artifact_id == TASK_OUTLINE_ARTIFACT_ID
        and outline.task_facts_id == task_facts.artifact_id
        and outline.source_artifact_ids == (task_facts.artifact_id,)
        and outline.artifact_path == TASK_OUTLINE_ARTIFACT_PATH
        and outline.validation_status == "validated"
    )
    if not expected_metadata:
        raise ValueError("TaskOutline 元数据与当前 TaskFacts 不一致")

    expected_question_ids = task_facts.expected_question_ids
    if expected_question_ids is None:
        raise ValueError("TaskFacts 未提供可可靠验证的问题边界")
    actual_question_ids = {question.question_id for question in outline.questions}
    if actual_question_ids != set(expected_question_ids):
        raise ValueError(
            "TaskOutline 问题必须严格覆盖 TaskFacts 的预期问题集合: "
            f"expected={list(expected_question_ids)}, "
            f"actual={sorted(actual_question_ids)}"
        )

    for question in outline.questions:
        if question.text not in task_facts.problem_text:
            raise ValueError(f"{question.question_id} 的 text 不是题目原文片段")

    global_by_id = {
        deliverable.deliverable_id: deliverable for deliverable in outline.deliverables
    }
    for question in outline.questions:
        for deliverable in question.deliverables:
            if global_by_id.get(deliverable.deliverable_id) != deliverable:
                raise ValueError(
                    f"{question.question_id} 的交付物 "
                    f"{deliverable.deliverable_id} 未在总体交付物中原样声明"
                )

    template_paths = {item.path for item in task_facts.output_templates}
    deliverable_paths = _collect_file_deliverables(outline.deliverables)
    if len(deliverable_paths) != len(set(deliverable_paths)):
        raise ValueError("TaskOutline 文件交付物路径不能重复")
    if set(deliverable_paths) != template_paths:
        raise ValueError(
            "TaskOutline 文件交付物必须严格匹配 TaskFacts.output_templates: "
            f"expected={sorted(template_paths)}, actual={sorted(deliverable_paths)}"
        )


def _collect_file_deliverables(
    deliverables: tuple[Deliverable, ...],
) -> tuple[str, ...]:
    """收集总体交付物中的非空文件路径。"""
    return tuple(
        deliverable.path for deliverable in deliverables if deliverable.path is not None
    )
