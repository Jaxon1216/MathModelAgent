"""Modeler Agent 与领域契约的集成测试。"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from app.agents.modeler import ModelerAgent, ModelerResponseError
from app.domain.problem import (
    DataCatalog,
    DataColumn,
    DataTable,
    Problem,
    QuestionSet,
)
from app.orchestration.workflow import ModelerStageError, ModelerWorkflow
from app.runtime.llm.client import LLMClientError, LegacyLLMClient


class FakeLLMClient:
    """记录请求并按顺序返回预设文本的 LLM 边界替身。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = iter(responses)
        self.messages: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]]) -> str:
        """记录请求并返回下一条响应。"""
        self.messages.append(list(messages))
        return next(self._responses)


class LegacyLLMStub:
    """记录新适配器发出的旧 LLM 调用。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        """返回符合旧 LLM 响应形状的最小对象。"""
        self.calls.append(kwargs)
        return SimpleNamespace(content="{}")


class FakeTracer:
    """捕获阶段 trace，不访问 Redis 或文件。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def start(self, task_id: str, phase: str) -> None:
        """记录开始。"""
        self.events.append(("start", {"task_id": task_id, "phase": phase}))

    async def event(self, task_id: str, event: str, *, phase: str, **payload) -> None:
        """记录阶段事件。"""
        self.events.append((event, {"task_id": task_id, "phase": phase, **payload}))

    async def end(
        self,
        task_id: str,
        phase: str,
        *,
        success: bool,
        duration_ms: int,
        failure_kind: str | None = None,
    ) -> None:
        """记录终态。"""
        self.events.append(
            (
                "end",
                {
                    "task_id": task_id,
                    "phase": phase,
                    "success": success,
                    "duration_ms": duration_ms,
                    "failure_kind": failure_kind,
                },
            )
        )


def _problem() -> Problem:
    """构造单问题的 Modeler 输入。"""
    return Problem(
        task_id="modeler-integration",
        question_set=QuestionSet(
            question_count=1,
            questions={"ques1": "预测下一年度产量。"},
            background="农作物种植策略",
        ),
        data_catalog=DataCatalog(
            input_tables=(
                DataTable(
                    table_id="crop.csv",
                    file="crop.csv",
                    columns=(
                        DataColumn(name="year", source_name="year"),
                        DataColumn(name="yield", source_name="yield"),
                    ),
                ),
            ),
        ),
    )


def _payload() -> dict:
    """构造可通过领域校验的模型响应。"""
    section = {
        "data": [{"table_id": "crop.csv", "columns": ["year", "yield"]}],
        "objective": "预测下一年度产量",
        "model": "趋势回归模型",
        "method": "拟合趋势并外推",
        "constraints": ["产量非负"],
        "validation": ["留出年度误差"],
        "figures": ["预测趋势图"],
        "fallback": "使用最近年份均值",
    }
    return {
        "version": "m1",
        "question_plans": {"ques1": section},
        "sensitivity_analysis": {
            **section,
            "objective": "评估趋势参数扰动",
            "figures": ["参数敏感性曲线"],
        },
    }


def test_modeler_repairs_once_without_retaining_invalid_response():
    """Modeler 只给一次修复机会，修复请求不回灌无效模型正文。"""
    invalid_response = "这不是 JSON"
    client = FakeLLMClient(
        [invalid_response, json.dumps(_payload(), ensure_ascii=False)]
    )

    tracer = FakeTracer()
    plan = asyncio.run(
        ModelerWorkflow(ModelerAgent(client), tracer=tracer).create_plan(_problem())
    )

    assert set(plan.question_plans) == {"ques1"}
    assert len(client.messages) == 2
    assert len(client.messages[0]) == 2
    assert len(client.messages[1]) == 3
    assert "invalid_json" in client.messages[1][-1]["content"]
    assert invalid_response not in str(client.messages[1])
    assert tracer.events[0][0] == "start"
    assert tracer.events[-1][0] == "end"
    assert tracer.events[-1][1]["success"] is True
    assert tracer.events[-1][1]["failure_kind"] is None


def test_modeler_stage_stops_after_one_failed_repair():
    """两次非法输出后，编排层必须以明确阶段失败停止。"""
    client = FakeLLMClient(["{}", "{}"])
    tracer = FakeTracer()

    with pytest.raises(ModelerStageError, match="after 2 attempts") as exc_info:
        asyncio.run(
            ModelerWorkflow(ModelerAgent(client), tracer=tracer).create_plan(_problem())
        )

    assert isinstance(exc_info.value.__cause__, ModelerResponseError)
    assert exc_info.value.failure_kind == "invalid_response"
    assert exc_info.value.attempts == 2
    assert len(client.messages) == 2
    assert tracer.events[-1][1]["success"] is False
    assert tracer.events[-1][1]["failure_kind"] == "invalid_response"


def test_legacy_llm_adapter_disables_provider_retries():
    """唯一旧 LLM 适配器必须保留 Modeler 身份并禁用内部重试。"""
    legacy_llm = LegacyLLMStub()

    response = asyncio.run(
        LegacyLLMClient(legacy_llm).complete([{"role": "user", "content": "返回 JSON"}])
    )

    assert response == "{}"
    assert legacy_llm.calls == [
        {
            "history": [{"role": "user", "content": "返回 JSON"}],
            "agent_name": "ModelerAgent",
            "max_retries": 0,
            "publish_response": False,
        }
    ]


def test_legacy_llm_adapter_wraps_provider_errors():
    """Provider 异常应有稳定类型，不与响应校验失败混淆。"""

    class FailingLegacyLLM:
        async def chat(self, **kwargs):
            del kwargs
            raise ConnectionError("provider unavailable")

    with pytest.raises(LLMClientError) as exc_info:
        asyncio.run(
            LegacyLLMClient(FailingLegacyLLM()).complete(
                [{"role": "user", "content": "返回 JSON"}]
            )
        )

    assert exc_info.value.kind == "provider"
    assert "ConnectionError" in exc_info.value.detail


def test_legacy_llm_adapter_classifies_empty_response():
    """空响应应与 Provider 异常、计划校验失败区分。"""

    class EmptyLegacyLLM:
        async def chat(self, **kwargs):
            del kwargs
            return SimpleNamespace(content="  ")

    with pytest.raises(LLMClientError) as exc_info:
        asyncio.run(
            LegacyLLMClient(EmptyLegacyLLM()).complete(
                [{"role": "user", "content": "返回 JSON"}]
            )
        )

    assert exc_info.value.kind == "empty_response"


def test_legacy_llm_adapter_classifies_configuration_error():
    """旧 LLM 的配置异常应与 Provider 故障区分。"""

    class LLMConfigError(RuntimeError):
        pass

    class MisconfiguredLegacyLLM:
        async def chat(self, **kwargs):
            del kwargs
            raise LLMConfigError("missing model")

    with pytest.raises(LLMClientError) as exc_info:
        asyncio.run(
            LegacyLLMClient(MisconfiguredLegacyLLM()).complete(
                [{"role": "user", "content": "返回 JSON"}]
            )
        )

    assert exc_info.value.kind == "config"


def test_modeler_stage_classifies_provider_failure():
    """编排层应将运行时失败转成统一阶段错误并落失败终态。"""

    class FailingClient:
        async def complete(self, messages):
            del messages
            raise LLMClientError("provider", "upstream timeout")

    tracer = FakeTracer()
    with pytest.raises(ModelerStageError) as exc_info:
        asyncio.run(
            ModelerWorkflow(ModelerAgent(FailingClient()), tracer=tracer).create_plan(
                _problem()
            )
        )

    assert exc_info.value.failure_kind == "llm_provider"
    assert tracer.events[-1][1]["failure_kind"] == "llm_provider"


def test_modeler_stage_scans_catalog_inside_trace(tmp_path: Path):
    """真实附件扫描必须发生在 Modeler phase.start 与 phase.end 之间。"""
    (tmp_path / "crop.csv").write_text("year,yield\n", encoding="utf-8")
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.append(["year"])
    workbook.save(tmp_path / "result.xlsx")
    client = FakeLLMClient([json.dumps(_payload(), ensure_ascii=False)])
    tracer = FakeTracer()

    result = asyncio.run(
        ModelerWorkflow(ModelerAgent(client), tracer=tracer).create_plan_from_work_dir(
            task_id="catalog-stage",
            question_set=_problem().question_set,
            work_dir=tmp_path,
        )
    )

    assert result.problem.data_catalog.output_templates == ("result.xlsx",)
    assert [event for event, _ in tracer.events] == [
        "start",
        "modeler.input_catalog",
        "end",
    ]
    assert tracer.events[1][1]["input_table_count"] == 1
    assert tracer.events[-1][1]["success"] is True


def test_modeler_stage_preserves_cancellation_and_records_terminal_trace():
    """取消不能包装成普通 Modeler 失败，但必须落 phase.end。"""

    class CancelledClient:
        async def complete(self, messages):
            del messages
            raise asyncio.CancelledError("stop")

    tracer = FakeTracer()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            ModelerWorkflow(ModelerAgent(CancelledClient()), tracer=tracer).create_plan(
                _problem()
            )
        )

    assert tracer.events[-1][1]["success"] is False
    assert tracer.events[-1][1]["failure_kind"] == "cancelled"


def test_legacy_llm_adapter_cancels_inflight_request():
    """取消信号必须终止 Provider 请求并保持 CancelledError 语义。"""

    class SlowLegacyLLM:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = False

        async def chat(self, **kwargs):
            del kwargs
            self.started.set()
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    async def scenario() -> tuple[bool, bool]:
        cancel_event = asyncio.Event()
        legacy_llm = SlowLegacyLLM()
        task = asyncio.create_task(
            LegacyLLMClient(legacy_llm, cancel_event=cancel_event).complete(
                [{"role": "user", "content": "返回 JSON"}]
            )
        )
        await legacy_llm.started.wait()
        cancel_event.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        return legacy_llm.cancelled, task.cancelled()

    provider_cancelled, task_cancelled = asyncio.run(scenario())
    assert provider_cancelled is True
    assert task_cancelled is True
