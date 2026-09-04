"""Modeler 使用的 LLM 运行时边界与旧实现适配。"""

from __future__ import annotations

import asyncio
from typing import Any, Literal, Protocol

ChatMessage = dict[str, str]
_LEGACY_MODELER_AGENT_NAME = "ModelerAgent"
LLMFailureKind = Literal["config", "provider", "empty_response"]


class LLMClientError(RuntimeError):
    """LLM 运行时失败；区别于模型返回内容不合法。"""

    def __init__(self, kind: LLMFailureKind, detail: str) -> None:
        self.kind = kind
        self.detail = detail[:500]
        super().__init__(f"{kind}: {self.detail}")


class LLMClient(Protocol):
    """Agent 所需的最小 LLM 能力。"""

    async def complete(self, messages: list[ChatMessage]) -> str:
        """根据消息返回原始文本。"""
        ...


class LegacyLLMClient:
    """将旧 LLM 封装适配为新 Agent 的最小运行时接口。

    这是 M1 的唯一过渡依赖。领域和 Agent 不导入旧 `core` 模块。
    """

    def __init__(
        self,
        llm: Any,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        self._llm = llm
        self._cancel_event = cancel_event

    async def complete(self, messages: list[ChatMessage]) -> str:
        """执行一次旧 LLM 调用，不使用 Provider 内部重试。"""
        try:
            response = await self._complete_with_cancellation(messages)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            kind: LLMFailureKind = (
                "config" if type(exc).__name__ == "LLMConfigError" else "provider"
            )
            raise LLMClientError(
                kind,
                f"{type(exc).__name__}: {exc}",
            ) from exc

        if not isinstance(response.content, str) or not response.content.strip():
            raise LLMClientError("empty_response", "Modeler LLM returned empty content")
        return response.content

    async def _complete_with_cancellation(self, messages: list[ChatMessage]) -> Any:
        """等待旧 LLM 或取消信号，取消时终止正在进行的请求。"""
        if self._cancel_event is None:
            return await self._request(messages)
        if self._cancel_event.is_set():
            raise asyncio.CancelledError("任务被用户停止")

        request_task = asyncio.create_task(self._request(messages))
        cancel_task = asyncio.create_task(self._cancel_event.wait())
        done, pending = await asyncio.wait(
            {request_task, cancel_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_task in done and self._cancel_event.is_set():
            request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)
            raise asyncio.CancelledError("任务被用户停止")

        cancel_task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        return await request_task

    async def _request(self, messages: list[ChatMessage]) -> Any:
        """调用旧 LLM，并关闭前端消息发布以支持独立 smoke。"""
        return await self._llm.chat(
            history=messages,
            agent_name=_LEGACY_MODELER_AGENT_NAME,
            max_retries=0,
            publish_response=False,
        )
