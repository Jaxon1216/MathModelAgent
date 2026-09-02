"""Agent 基类模块，提供对话管理和记忆压缩功能。"""

import asyncio
from typing import Any

from app.core.llm.llm import LLM, simple_chat
from app.services.trace_recorder import trace_recorder
from app.utils.log_util import logger
from app.utils.problem_context import redact_execution_constraints
from app.utils.source_inspection import redact_source_inspection_echoes

# TODO: 评估任务完成情况，rethinking

# 每个字符估算的 token 数（中英混合文本的保守估计）
_CHARS_PER_TOKEN = 3
# 触发压缩的 token 占比阈值（相对 context_window）
_DEFAULT_TOKEN_THRESHOLD_RATIO = 0.75
_MAX_FACT_SUMMARY_CHARS = 6000
_FACT_MARKERS = (
    "path",
    "file",
    "csv",
    "xlsx",
    "png",
    "jpg",
    "npy",
    "metric",
    "accuracy",
    "precision",
    "recall",
    "f1",
    "rmse",
    "mae",
    "mse",
    "r2",
    "auc",
    "score",
    "loss",
    "rows",
    "columns",
    "指标",
    "结果",
    "结论",
    "警告",
    "限制",
    "失败",
    "未完成",
    "不可用",
    "unavailable",
)
_PROCESS_MARKERS = (
    "原始任务",
    "原始题面",
    "原始问题",
    "题面原文",
    "题目原文",
    "用户输入",
    "系统提示",
    "过程说明",
    "过程指令",
    "original task",
    "original prompt",
    "system prompt",
    "user prompt",
    "执行约束",
    "不要使用",
    "禁止使用",
    "必须使用",
    "不要调用",
    "请调用",
    "please call",
    "do not use",
    "must use",
)


class Agent:
    """Agent 基类，管理对话历史、轮次控制和记忆压缩。"""

    def __init__(
        self,
        task_id: str,
        model: LLM,
        context_window: int = 128000,  # 模型上下文窗口大小（token）
        token_threshold_ratio: float = _DEFAULT_TOKEN_THRESHOLD_RATIO,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        self.task_id = task_id
        self.model = model
        self.chat_history: list[dict] = []  # 存储对话历史
        self.context_window = context_window
        self.token_threshold_ratio = token_threshold_ratio
        self.current_token_count = 0  # 当前历史的估算 token 数
        self.cancel_event = cancel_event  # 取消信号
        self.history_scope = "default"
        self.compression_count = 0
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0

    def _estimate_tokens(self, text: str) -> int:
        """估算文本的 token 数量。"""
        return max(1, len(text) // _CHARS_PER_TOKEN)

    def _estimate_message_tokens(self, msg: dict) -> int:
        """估算单条消息的 token 数（含结构开销）。"""
        content = self._message_text(msg)
        # 4 token 额外开销（role、分隔符等）
        return self._estimate_tokens(content) + 4

    @staticmethod
    def _message_text(msg: dict) -> str:
        """把消息正文和工具调用参数纳入预算估算。"""
        parts = [
            str(msg.get("content") or ""),
            str(msg.get("reasoning_content") or ""),
            str(msg.get("tool_call_id") or ""),
            str(msg.get("name") or ""),
        ]
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            parts.append(str(tool_calls))
        return "\n".join(part for part in parts if part)

    def _history_chars(self) -> int:
        """统计当前历史中可见文本字符数，供上下文预算 trace 使用。"""
        total = 0
        for message in self.chat_history:
            total += len(self._message_text(message))
        return total

    def _history_char_budget(self) -> int:
        """把上下文窗口换算成与 trace 一致的字符预算。"""
        return int(self.context_window * self.token_threshold_ratio * _CHARS_PER_TOKEN)

    async def _chat(self, **kwargs) -> Any:
        """调用 LLM 模型，支持取消中断。

        将所有关键字参数透传给 self.model.chat()。
        若设置了 cancel_event，则通过 asyncio.wait 实现可中断等待。

        Returns:
            模型响应对象。
        """
        if not self.cancel_event:
            return await self.model.chat(**kwargs)

        chat_task = asyncio.create_task(self.model.chat(**kwargs))
        cancel_wait_task = asyncio.create_task(self.cancel_event.wait())
        done, pending = await asyncio.wait(
            {chat_task, cancel_wait_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_wait_task in done:
            chat_task.cancel()
            for p in pending:
                p.cancel()
            raise asyncio.CancelledError("任务被用户停止")
        return await chat_task

    async def append_chat_history(self, msg: dict) -> None:
        """向对话历史追加消息，并在必要时触发记忆压缩。

        Args:
            msg: 消息字典，需包含 role 和 content 字段。
        """
        self.chat_history.append(msg)
        self.current_token_count += self._estimate_message_tokens(msg)

        # 只有在添加非tool消息时才进行内存清理，避免在工具调用期间破坏消息结构
        if msg.get("role") != "tool":
            await self.compress_if_needed()

    def reset_history(self, scope: str) -> None:
        """重置当前上下文边界，但不触碰 Agent 依赖的外部资源。

        Args:
            scope: 新历史的逻辑范围，例如 ``ques1`` 或 ``section:abstract``。
        """
        self.chat_history = []
        self.current_token_count = 0
        self.history_scope = scope
        self.compression_count = 0
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0

    async def record_response(
        self,
        response: Any,
        *,
        allow_compress: bool = True,
    ) -> None:
        """追加 LLM 响应并统一更新 history/token 事实。

        自定义 ReAct 循环不能复用 ``run``，因此必须通过此方法记录响应。
        工具调用阶段可关闭压缩，避免在 assistant/tool 成对消息之间切断历史。

        Args:
            response: 标准 LLM 响应对象。
            allow_compress: 是否在追加响应后检查历史压缩。
        """
        content = response.content or ""
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": content}
        if response.reasoning_content:
            assistant_msg["reasoning_content"] = response.reasoning_content
        if response.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.name,
                        "arguments": tool_call.arguments,
                    },
                }
                for tool_call in response.tool_calls
            ]

        self.chat_history.append(assistant_msg)
        self.current_token_count += self._estimate_message_tokens(assistant_msg)

        usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        self.last_prompt_tokens = prompt_tokens
        self.last_completion_tokens = completion_tokens
        if prompt_tokens > 0:
            # prompt_tokens 是本次请求发出前的真实 history 成本；响应消息
            # 尚未包含在下一次请求中，因此在此基础上补一条响应估算。
            self.current_token_count = prompt_tokens + self._estimate_message_tokens(
                assistant_msg
            )

        await trace_recorder.emit(
            self.task_id,
            "history.update",
            agent=self.__class__.__name__,
            phase=self.history_scope,
            history_messages=len(self.chat_history),
            history_chars=self._history_chars(),
            history_budget_chars=self._history_char_budget(),
            history_token_count=self.current_token_count,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            compression_count=self.compression_count,
        )
        if allow_compress:
            await self.compress_if_needed()

    async def compress_if_needed(self) -> None:
        """当 token 数超过上下文窗口阈值时，使用 LLM 总结压缩历史。"""
        threshold = int(self.context_window * self.token_threshold_ratio)
        if self.current_token_count <= threshold:
            return

        logger.info(
            f"{self.__class__.__name__}:触发记忆压缩，"
            f"当前 token ~{self.current_token_count}，阈值 {threshold}"
        )

        try:
            # 保留第一条系统消息
            system_msg = (
                self.chat_history[0]
                if self.chat_history and self.chat_history[0]["role"] == "system"
                else None
            )

            # 查找需要保留的消息范围 - 保留最后几条完整的对话和工具调用
            preserve_start_idx = self._find_safe_preserve_point()

            # 确定需要总结的消息范围
            start_idx = 1 if system_msg else 0
            end_idx = preserve_start_idx

            if end_idx > start_idx:
                # 只从 assistant/tool 结果中提取事实，避免把原始题面、
                # 重试闲聊和执行禁令再次带入下一个上下文边界。
                summarize_history = []
                if system_msg:
                    summarize_history.append(system_msg)

                summarize_history.append(
                    {
                        "role": "user",
                        "content": (
                            "请仅整理以下已经产生的可引用事实：路径、列名、"
                            "指标、结论、警告、限制和未完成项。不要复述任务、"
                            "执行约束、代码或对话过程；没有事实时写 unavailable。\n\n"
                            f"{self._format_history_for_summary(self.chat_history[start_idx:end_idx])}"
                        ),
                    }
                )

                # 调用 simple_chat 进行总结
                summary = self._sanitize_compression_summary(
                    await simple_chat(self.model, summarize_history)
                )

                # 重构聊天历史：系统消息 + 总结 + 保留的消息
                new_history = []
                if system_msg:
                    new_history.append(system_msg)

                new_history.append(
                    {"role": "assistant", "content": f"[历史对话总结] {summary}"}
                )

                # 添加需要保留的消息（最后几条完整对话）
                new_history.extend(self.chat_history[preserve_start_idx:])

                self.chat_history = new_history

                # 重新估算 token 数
                self.current_token_count = sum(
                    self._estimate_message_tokens(m) for m in self.chat_history
                )
                self.compression_count += 1
                await trace_recorder.emit(
                    self.task_id,
                    "context.compress",
                    agent=self.__class__.__name__,
                    phase=self.history_scope,
                    before_messages=end_idx,
                    after_messages=len(self.chat_history),
                    history_chars=self._history_chars(),
                    history_budget_chars=self._history_char_budget(),
                    history_token_count=self.current_token_count,
                    compression_count=self.compression_count,
                    summary_chars=len(summary),
                )
                logger.info(
                    f"{self.__class__.__name__}:记忆压缩完成，"
                    f"压缩至 {len(self.chat_history)} 条记录，"
                    f"约 {self.current_token_count} tokens"
                )
            else:
                logger.info(f"{self.__class__.__name__}:无需压缩，记录数量合理")

        except Exception as e:
            logger.error(f"记忆压缩失败，使用简单切片策略: {str(e)}")
            # 如果总结失败，回退到安全的策略：保留系统消息和最后几条消息，确保工具调用完整性
            safe_history = self._get_safe_fallback_history()
            self.chat_history = safe_history
            self.current_token_count = sum(
                self._estimate_message_tokens(m) for m in self.chat_history
            )
            self.compression_count += 1
            await trace_recorder.emit(
                self.task_id,
                "context.compress",
                agent=self.__class__.__name__,
                phase=self.history_scope,
                before_messages=len(self.chat_history),
                after_messages=len(safe_history),
                history_chars=self._history_chars(),
                history_budget_chars=self._history_char_budget(),
                history_token_count=self.current_token_count,
                compression_count=self.compression_count,
                summary_chars=0,
                fallback=True,
            )

    def _sanitize_compression_summary(self, summary: Any) -> str:
        """只保留模型摘要中的确定性事实，阻断过程指令回写 history。"""
        facts: list[str] = []
        for raw_line in str(summary or "").splitlines():
            safe_line = redact_source_inspection_echoes(raw_line)
            safe_line = redact_execution_constraints(safe_line)
            for line in safe_line.splitlines():
                candidate = line.strip()
                if not candidate:
                    continue
                candidate = redact_source_inspection_echoes(candidate).strip()
                if not candidate:
                    continue
                lowered = candidate.casefold()
                if any(
                    marker.casefold() in lowered for marker in _PROCESS_MARKERS
                ):
                    continue
                if any(marker.casefold() in lowered for marker in _FACT_MARKERS):
                    if candidate not in facts:
                        facts.append(candidate)
        if not facts:
            return "unavailable"
        return self._truncate_summary("\n".join(facts))

    def _find_safe_preserve_point(self) -> int:
        """找到安全的保留起始点，确保不会破坏工具调用序列。"""
        # 最少保留最后3条消息，确保基本对话完整性
        min_preserve = min(3, len(self.chat_history))
        preserve_start = len(self.chat_history) - min_preserve

        # 从后往前查找，确保不会在工具调用序列中间切断
        for i in range(preserve_start, -1, -1):
            if i >= len(self.chat_history):
                continue

            if self._is_safe_cut_point(i):
                return i

        # 如果找不到安全点，至少保留最后1条消息
        return len(self.chat_history) - 1

    def _is_safe_cut_point(self, start_idx: int) -> bool:
        """检查从指定位置开始切割是否安全（不会产生孤立的tool消息）。"""
        if start_idx >= len(self.chat_history):
            return True

        for i in range(start_idx, len(self.chat_history)):
            msg = self.chat_history[i]
            if isinstance(msg, dict) and msg.get("role") == "tool":
                tool_call_id = msg.get("tool_call_id")

                # 向前查找对应的tool_calls消息
                if tool_call_id:
                    found_tool_call = False
                    for j in range(start_idx, i):
                        prev_msg = self.chat_history[j]
                        if (
                            isinstance(prev_msg, dict)
                            and "tool_calls" in prev_msg
                            and prev_msg["tool_calls"]
                        ):
                            for tool_call in prev_msg["tool_calls"]:
                                if tool_call.get("id") == tool_call_id:
                                    found_tool_call = True
                                    break
                            if found_tool_call:
                                break

                    if not found_tool_call:
                        return False

        return True

    def _get_safe_fallback_history(self) -> list:
        """获取安全的后备历史记录，确保不会有孤立的tool消息。"""
        if not self.chat_history:
            return []

        # 保留系统消息
        safe_history = []
        if self.chat_history and self.chat_history[0]["role"] == "system":
            safe_history.append(self.chat_history[0])

        # 从后往前查找安全的消息序列
        for preserve_count in range(1, min(4, len(self.chat_history)) + 1):
            start_idx = len(self.chat_history) - preserve_count
            if self._is_safe_cut_point(start_idx):
                safe_history.extend(self.chat_history[start_idx:])
                return safe_history

        # 如果都不安全，只保留最后一条非tool消息
        for i in range(len(self.chat_history) - 1, -1, -1):
            msg = self.chat_history[i]
            if isinstance(msg, dict) and msg.get("role") != "tool":
                safe_history.append(msg)
                break

        return safe_history

    def _format_history_for_summary(self, history: list[dict]) -> str:
        """提取可交接事实，过滤原始任务和过程指令。"""
        formatted: list[str] = []
        for msg in history:
            role = msg.get("role")
            if role not in {"assistant", "tool"}:
                continue
            content = msg.get("content") or ""
            for line in str(content).splitlines():
                candidate = line.strip()
                if not candidate:
                    continue
                candidate = redact_source_inspection_echoes(candidate).strip()
                if not candidate:
                    continue
                lowered = candidate.lower()
                if any(marker in lowered for marker in _PROCESS_MARKERS):
                    continue
                if any(marker in lowered for marker in _FACT_MARKERS):
                    formatted.append(f"{role}: {candidate}")

        if not formatted:
            return "unavailable: no bounded facts were found"
        return self._truncate_summary("\n".join(formatted))

    @staticmethod
    def _truncate_summary(text: str) -> str:
        """限制压缩摘要长度，避免摘要本身成为新的长历史。"""
        if len(text) <= _MAX_FACT_SUMMARY_CHARS:
            return text
        return text[: _MAX_FACT_SUMMARY_CHARS - 20].rstrip() + "\n...[truncated]"
