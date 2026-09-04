"""阶段 trace 的运行时边界与旧 recorder 适配。"""

from __future__ import annotations

from typing import Any, Protocol

from app.services.trace_recorder import set_trace_phase, trace_recorder


class StageTracer(Protocol):
    """编排层所需的最小 trace 能力。"""

    async def start(self, task_id: str, phase: str) -> None:
        """记录阶段开始。"""
        ...

    async def event(
        self,
        task_id: str,
        event: str,
        *,
        phase: str,
        **payload: Any,
    ) -> None:
        """记录阶段内事件。"""
        ...

    async def end(
        self,
        task_id: str,
        phase: str,
        *,
        success: bool,
        duration_ms: int,
        failure_kind: str | None = None,
    ) -> None:
        """记录阶段结束并清理上下文。"""
        ...


class LegacyStageTracer:
    """把新编排事件写入现有 JSONL/Redis trace。"""

    async def start(self, task_id: str, phase: str) -> None:
        """设置 phase 上下文并记录开始。"""
        set_trace_phase(phase)
        await trace_recorder.emit(task_id, "phase.start", phase=phase)

    async def event(
        self,
        task_id: str,
        event: str,
        *,
        phase: str,
        **payload: Any,
    ) -> None:
        """记录阶段内结构化事件。"""
        await trace_recorder.emit(task_id, event, phase=phase, **payload)

    async def end(
        self,
        task_id: str,
        phase: str,
        *,
        success: bool,
        duration_ms: int,
        failure_kind: str | None = None,
    ) -> None:
        """记录终态；无论写入结果如何都清理 phase 上下文。"""
        try:
            await trace_recorder.emit(
                task_id,
                "phase.end",
                phase=phase,
                success=success,
                duration_ms=duration_ms,
                failure_kind=failure_kind,
            )
        finally:
            set_trace_phase(None)


class NullStageTracer:
    """测试或嵌入调用使用的无副作用 tracer。"""

    async def start(self, task_id: str, phase: str) -> None:
        """忽略阶段开始。"""

    async def event(
        self,
        task_id: str,
        event: str,
        *,
        phase: str,
        **payload: Any,
    ) -> None:
        """忽略阶段事件。"""

    async def end(
        self,
        task_id: str,
        phase: str,
        *,
        success: bool,
        duration_ms: int,
        failure_kind: str | None = None,
    ) -> None:
        """忽略阶段结束。"""
