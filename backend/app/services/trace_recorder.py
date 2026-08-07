"""Trace 埋点记录器：JSONL 持久化 + Redis 实时推送。"""

from __future__ import annotations

import json
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.schemas.response import TraceMessage
from app.services.redis_manager import redis_manager
from app.utils.log_util import logger

current_trace_phase: ContextVar[str | None] = ContextVar("current_trace_phase", default=None)


def set_trace_phase(phase: str | None) -> None:
    """设置当前 workflow phase 上下文。

    Args:
        phase: 阶段名称，如 eda / ques1；传 None 表示清除。
    """
    current_trace_phase.set(phase)


def get_trace_phase() -> str | None:
    """获取当前 workflow phase 上下文。"""
    return current_trace_phase.get()


class TraceRecorder:
    """结构化 Trace 事件记录器，双写 JSONL 与 WebSocket。"""

    def __init__(self) -> None:
        self.traces_dir = Path("logs/traces")
        self.traces_dir.mkdir(parents=True, exist_ok=True)

    def _format_content(
        self, event: str, phase: str | None, payload: dict[str, Any]
    ) -> str:
        """将结构化 payload 格式化为前端可读摘要。"""
        phase_part = f" · {phase}" if phase else ""

        if event == "task.start":
            return f"task.start{phase_part} · work_dir={payload.get('work_dir', '')}"

        if event == "agent.init":
            return (
                f"agent.init{phase_part} · "
                f"{payload.get('agent_class', '')} model={payload.get('model', '')}"
            )

        if event == "interpreter.init":
            return (
                f"interpreter.init{phase_part} · "
                f"type={payload.get('interpreter_type', 'local')}"
            )

        if event == "phase.start":
            return f"phase.start · {payload.get('phase', phase or '')}"

        if event == "phase.end":
            status = "ok" if payload.get("success", True) else "fail"
            duration = payload.get("duration_ms")
            dur = f", {duration}ms" if duration is not None else ""
            return f"phase.end · {payload.get('phase', phase or '')} · {status}{dur}"

        if event == "skill.load":
            return (
                f"skill.load{phase_part} · {payload.get('skill_name', '')} · "
                f"found={payload.get('found', False)}, "
                f"{payload.get('body_chars', 0)} chars"
            )

        if event == "tool.call":
            return (
                f"tool.call{phase_part} · {payload.get('tool_name', '')} · "
                f"{payload.get('arg_summary', '')}"
            )

        if event == "execute.start":
            return (
                f"execute.start{phase_part} · "
                f"{payload.get('code_lines', 0)} lines"
            )

        if event == "execute.done":
            status = "error" if payload.get("error") else "ok"
            duration = payload.get("duration_ms")
            dur = f", {duration}ms" if duration is not None else ""
            extra = ""
            if payload.get("new_artifacts"):
                extra = f", +{payload['new_artifacts']} artifacts"
            return f"execute.done{phase_part} · {status}{dur}{extra}"

        if event == "react.turn":
            return (
                f"react.turn{phase_part} · turn={payload.get('turn', 0)}, "
                f"retry={payload.get('retry_count', 0)}"
            )

        if event == "react.reflect":
            preview = payload.get("error_preview", "")
            if len(preview) > 80:
                preview = preview[:80] + "..."
            return (
                f"react.reflect{phase_part} · retry {payload.get('retry_count', 0)} · "
                f"{preview}"
            )

        if event == "react.completion_check":
            req = payload.get("images_required")
            req_part = f", required={req}" if req is not None else ""
            blocked = " BLOCKED" if payload.get("blocked_exit") else ""
            return (
                f"react.completion_check{phase_part} · "
                f"images={payload.get('images_in_section', 0)}{req_part}, "
                f"ok={payload.get('images_ok', False)}, "
                f"continue={payload.get('will_continue', False)}{blocked}"
            )

        if event == "artifact.created":
            return (
                f"artifact.created{phase_part} · "
                f"{payload.get('path', '')} ({payload.get('kind', '')})"
            )

        if event == "subtask.summary":
            return (
                f"subtask.summary{phase_part} · "
                f"turns={payload.get('turns', 0)}, "
                f"retries={payload.get('retries', 0)}, "
                f"png={payload.get('png_count', 0)}, "
                f"csv={payload.get('csv_count', 0)}"
            )

        if event == "llm.response":
            return (
                f"llm.response{phase_part} · "
                f"{payload.get('agent', '')} model={payload.get('model', '')}, "
                f"{payload.get('latency_ms', 0)}ms, "
                f"prompt={payload.get('prompt_tokens', 0)}, "
                f"completion={payload.get('completion_tokens', 0)}, "
                f"total={payload.get('total_tokens', 0)}, "
                f"tools={payload.get('tool_calls_count', 0)}"
            )

        # 兜底：展示 event 与 payload 键
        keys = ", ".join(f"{k}={v}" for k, v in list(payload.items())[:4])
        return f"{event}{phase_part} · {keys}" if keys else event

    async def emit(
        self,
        task_id: str,
        event: str,
        *,
        agent: str | None = None,
        phase: str | None = None,
        **payload: Any,
    ) -> None:
        """写入 JSONL 并推送 TraceMessage 到前端。

        Args:
            task_id: 任务 ID。
            event: 事件名称。
            agent: Agent 类名。
            phase: 阶段名称；省略时使用 contextvar。
            **payload: 事件附加字段。
        """
        resolved_phase = phase if phase is not None else get_trace_phase()
        ts = datetime.now(UTC).isoformat()
        record = {
            "ts": ts,
            "task_id": task_id,
            "event": event,
            "agent": agent,
            "phase": resolved_phase,
            "payload": payload,
        }

        try:
            trace_path = self.traces_dir / f"{task_id}.jsonl"
            with trace_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            logger.error(f"Trace JSONL 写入失败: {exc}")

        content = self._format_content(event, resolved_phase, payload)
        try:
            await redis_manager.publish_message(
                task_id,
                TraceMessage(
                    event=event,
                    agent=agent,
                    phase=resolved_phase,
                    payload=payload,
                    content=content,
                ),
            )
        except Exception as exc:
            logger.error(f"Trace 消息发布失败: {exc}")


trace_recorder = TraceRecorder()
