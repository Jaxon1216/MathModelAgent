"""Modeler 使用的 LLM 运行时边界与旧实现适配。"""

from __future__ import annotations

from typing import Any, Protocol

ChatMessage = dict[str, str]
_LEGACY_MODELER_AGENT_NAME = "ModelerAgent"


class LLMClient(Protocol):
    """Agent 所需的最小 LLM 能力。"""

    async def complete(self, messages: list[ChatMessage]) -> str:
        """根据消息返回原始文本。"""
        ...


class LegacyLLMClient:
    """将旧 LLM 封装适配为新 Agent 的最小运行时接口。

    这是 M1 的唯一过渡依赖。领域和 Agent 不导入旧 `core` 模块。
    """

    def __init__(self, llm: Any) -> None:
        self._llm = llm

    async def complete(self, messages: list[ChatMessage]) -> str:
        """执行一次旧 LLM 调用，不使用 Provider 内部重试。"""
        response = await self._llm.chat(
            history=messages,
            agent_name=_LEGACY_MODELER_AGENT_NAME,
            max_retries=0,
        )
        if not isinstance(response.content, str) or not response.content.strip():
            raise RuntimeError("Modeler LLM returned empty content")
        return response.content
