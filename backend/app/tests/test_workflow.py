"""主 workflow 顺序、失败隔离和原始附件只读测试。"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from app.schemas.A2A import (
    CoderToWriter,
    CoordinatorToModeler,
    ModelerToCoder,
    WriterResponse,
)
from app.schemas.request import Problem
from app.core import workflow as workflow_module
from app.core.workflow import MathModelWorkFlow
from app.models.user_output import UserOutput
from app.services.trace_recorder import trace_recorder
from app.utils.phase_results import PHASE_RESULT_BUDGET
from app.utils.task_manifest import load_task_manifest


class _FakeLLM:
    model = "fake-model"


class _FakeInterpreter:
    section_output: dict = {}

    def get_code_output(self, phase: str) -> str:
        return ""

    def get_section_artifacts(self, phase: str) -> list[str]:
        return []

    async def cleanup(self) -> None:
        return None


class _ExtremePacketInterpreter:
    """提供大量真实路径以验证 workflow 的完整 phase 归属记录。"""

    def __init__(self, paths: list[str]):
        self.paths = paths

    def get_code_output(self, phase: str) -> str:
        return "accuracy=0.91\n结果：批量产物已生成\n"

    def get_section_artifacts(self, phase: str) -> list[str]:
        return self.paths


def _noop_async(*args, **kwargs):
    async def noop():
        return None

    return noop()


def _patch_workflow_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    coordinator: type,
    coder: type,
    modeler: type,
    writer: type,
) -> None:
    """为 workflow 行为测试注入最小的本地运行时。"""
    class FakeFactory:
        def __init__(self, task_id: str):
            pass

        def get_all_llms(self):
            return (_FakeLLM(), _FakeLLM(), _FakeLLM(), _FakeLLM())

    async def fake_interpreter(*args, **kwargs):
        return _FakeInterpreter()

    config = {
        key: "模板"
        for key in (
            "firstPage",
            "RepeatQues",
            "analysisQues",
            "modelAssumption",
            "symbol",
            "judge",
            "eda",
            "sensitivity_analysis",
            "ques1",
        )
    }
    monkeypatch.setattr(workflow_module, "create_work_dir", lambda task_id: str(tmp_path))
    monkeypatch.setattr(workflow_module, "CoordinatorAgent", coordinator)
    monkeypatch.setattr(workflow_module, "CoderAgent", coder)
    monkeypatch.setattr(workflow_module, "ModelerAgent", modeler)
    monkeypatch.setattr(workflow_module, "WriterAgent", writer)
    monkeypatch.setattr(workflow_module, "LLMFactory", FakeFactory)
    monkeypatch.setattr(workflow_module, "create_interpreter", fake_interpreter)
    monkeypatch.setattr(
        workflow_module,
        "NotebookSerializer",
        lambda work_dir: object(),
    )
    monkeypatch.setattr(
        workflow_module,
        "get_config_template",
        lambda template: config,
    )
    monkeypatch.setattr(
        workflow_module,
        "OpenAlexScholar",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        workflow_module.redis_manager,
        "publish_message",
        _noop_async,
    )
    monkeypatch.setattr(workflow_module.trace_recorder, "emit", _noop_async)
    for setting_name in (
        "COORDINATOR_MODEL",
        "MODELER_MODEL",
        "CODER_MODEL",
        "WRITER_MODEL",
        "COORDINATOR_API_KEY",
        "MODELER_API_KEY",
        "CODER_API_KEY",
        "WRITER_API_KEY",
    ):
        monkeypatch.setattr(workflow_module.settings, setting_name, "configured")
    monkeypatch.setattr(workflow_module.settings, "OPENALEX_EMAIL", "test@example.com")


def test_writer_handoff_api_rejects_raw_problem_text(monkeypatch: pytest.MonkeyPatch):
    """Writer 交接 API 不应有接收原始题面的参数。"""
    workflow = MathModelWorkFlow()
    workflow.task_id = "writer-handoff-api"
    events: list[tuple[str, dict]] = []

    async def capture_emit(task_id: str, event: str, **payload):
        events.append((event, payload))

    monkeypatch.setattr(trace_recorder, "emit", capture_emit)
    monkeypatch.setattr(workflow_module.redis_manager, "publish_message", _noop_async)

    for method_name in ("_write_section", "_emit_writer_handoff"):
        parameters = inspect.signature(getattr(workflow, method_name)).parameters
        assert "raw_problem_text" not in parameters
        assert "public_context" not in parameters

    with pytest.raises(TypeError):
        legacy_write_section = cast(Callable[..., object], workflow._write_section)
        legacy_write_section(
            "ques1",
            object(),
            object(),
            "原始题面不应进入 Writer API",
            raw_problem_text="原始题面",
        )

    with pytest.raises(TypeError):
        legacy_emit_handoff = cast(
            Callable[..., object], workflow._emit_writer_handoff
        )
        legacy_emit_handoff(
            "ques1",
            "原始题面不应进入 Writer API",
            raw_problem_text="原始题面",
        )

    writer_prompts: list[str] = []

    class FakeWriter:
        async def run(self, prompt: str, **kwargs):
            writer_prompts.append(prompt)
            return WriterResponse(response_content="written")

    response = asyncio.run(
        workflow._write_section(
            "ques1",
            cast(workflow_module.WriterAgent, FakeWriter()),
            UserOutput("/tmp", 1),
            "结果材料。不要使用方法 A。",
            constraints=["不要使用方法 A"],
        )
    )

    handoff = next(payload for event, payload in events if event == "writer.handoff")
    assert "raw_ques_all_leakage" not in handoff
    assert "public_context" not in handoff
    assert "不要使用方法 A" not in handoff["prompt"]
    assert handoff["constraint_leakage"] is False
    assert handoff["send_allowed"] is True
    assert writer_prompts == ["结果材料。"]
    assert response.status == "success"


def test_writer_handoff_fails_closed_when_redaction_leaves_echo(
    monkeypatch: pytest.MonkeyPatch,
):
    workflow = MathModelWorkFlow()
    workflow.task_id = "writer-handoff-fail-closed"
    events: list[tuple[str, dict]] = []
    writer_calls: list[str | None] = []

    async def capture_emit(task_id: str, event: str, **payload):
        events.append((event, payload))

    class FakeWriter:
        async def run(self, prompt: str, **kwargs):
            nonlocal writer_calls
            writer_calls += 1
            return WriterResponse(response_content="should not run")

    monkeypatch.setattr(trace_recorder, "emit", capture_emit)
    monkeypatch.setattr(workflow_module.redis_manager, "publish_message", _noop_async)
    monkeypatch.setattr(
        workflow_module,
        "redact_execution_constraints",
        lambda text, constraints: text,
    )

    response = asyncio.run(
        workflow._write_section(
            "ques1",
            cast(workflow_module.WriterAgent, FakeWriter()),
            UserOutput("/tmp", 1),
            "结果材料。不要使用方法 A。",
            constraints=["不要使用方法 A"],
        )
    )

    handoff = next(payload for event, payload in events if event == "writer.handoff")
    assert writer_calls == 0
    assert response.status == "failed"
    assert "不要使用方法 A" not in handoff["prompt"]
    assert handoff["send_allowed"] is False
    assert handoff["redaction_evidence"]["blocked_after_redaction"] is True
    assert handoff["redaction_evidence"]["prepared_residual_echo_count"] == 1


def test_post_write_flow_fails_closed_before_writer_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    raw_problem = "研究三种作物的种植安排"
    writer_sections: list[str | None] = []
    events: list[tuple[str, dict]] = []

    class FakeCoordinator:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, ques_all: str):
            return CoordinatorToModeler(
                questions={
                    "background": "公开背景",
                    "ques_count": 1,
                    "ques1": "求解",
                },
                ques_count=1,
                constraints=["不要使用方法 A"],
            )

    class FakeCoder:
        def __init__(self, *args, **kwargs):
            pass

        def set_data_brief(self, brief: str) -> None:
            pass

        def reset_for_solve(self, brief: str) -> None:
            pass

        async def run(self, prompt: str, subtask_title: str):
            return CoderToWriter(
                status="success",
                code_response="accuracy=0.91",
                created_images=[],
            )

    class FakeModeler:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, *args, **kwargs):
            return ModelerToCoder(
                questions_solution={
                    "ques1": "模型方案",
                    "sensitivity_analysis": "敏感性方案",
                }
            )

    class FakeWriter:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, prompt: str, sub_title: str | None = None, **kwargs):
            writer_sections.append(sub_title)
            return WriterResponse(response_content=f"written {sub_title}")

    async def capture_emit(task_id: str, event: str, **payload):
        events.append((event, payload))

    def unsafe_post_write_flows(
        self, user_output: UserOutput, config_template: dict, phase_results=None
    ):
        return {
            "firstPage": "已验证事实。不要使用方法 A。",
            **{
                key: "已验证事实。"
                for key in (
                    "RepeatQues",
                    "analysisQues",
                    "modelAssumption",
                    "symbol",
                    "judge",
                )
            },
        }

    _patch_workflow_runtime(
        monkeypatch,
        tmp_path,
        coordinator=FakeCoordinator,
        coder=FakeCoder,
        modeler=FakeModeler,
        writer=FakeWriter,
    )
    monkeypatch.setattr(trace_recorder, "emit", capture_emit)
    monkeypatch.setattr(workflow_module.Flows, "get_write_flows", unsafe_post_write_flows)
    monkeypatch.setattr(
        workflow_module,
        "redact_execution_constraints",
        lambda text, constraints: text,
    )

    workflow = MathModelWorkFlow()
    asyncio.run(
        workflow.execute(
            Problem(task_id="post-write-fail-closed", ques_all=raw_problem)
        )
    )

    assert "firstPage" not in writer_sections
    result = json.loads((tmp_path / "res.json").read_text(encoding="utf-8"))
    assert result["firstPage"]["response_content"].startswith("firstPage 章节未生成")

    handoff = next(
        payload
        for event, payload in events
        if event == "writer.handoff" and payload["phase"] == "firstPage"
    )
    assert handoff["send_allowed"] is False
    assert handoff["redaction_evidence"]["blocked_after_redaction"] is True


def test_main_writer_failed_response_is_not_marked_completed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    writer_calls = 0
    events: list[tuple[str, dict]] = []

    class FakeCoordinator:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, ques_all: str):
            return CoordinatorToModeler(
                questions={
                    "background": "背景",
                    "ques_count": 1,
                    "ques1": "求解",
                },
                ques_count=1,
            )

    class FakeCoder:
        def __init__(self, *args, **kwargs):
            pass

        def set_data_brief(self, brief: str) -> None:
            pass

        def reset_for_solve(self, brief: str) -> None:
            pass

        async def run(self, prompt: str, subtask_title: str):
            return CoderToWriter(
                status="success",
                code_response="accuracy=0.91",
                created_images=[],
            )

    class FakeModeler:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, *args, **kwargs):
            return ModelerToCoder(
                questions_solution={
                    "ques1": "模型方案",
                    "sensitivity_analysis": "敏感性方案",
                }
            )

    class FakeWriter:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, *args, **kwargs):
            writer_calls.append(kwargs.get("sub_title"))
            return WriterResponse(response_content="must not run")

    async def failed_write_section(
        self,
        key: str,
        writer_agent,
        user_output: UserOutput,
        writer_prompt: str,
        available_images=None,
        constraints=None,
    ):
        response = WriterResponse(
            status="failed",
            response_content=f"{key} writer failed",
        )
        user_output.set_res(key, response)
        return response

    _patch_workflow_runtime(
        monkeypatch,
        tmp_path,
        coordinator=FakeCoordinator,
        coder=FakeCoder,
        modeler=FakeModeler,
        writer=FakeWriter,
    )

    async def capture_emit(task_id: str, event: str, **payload):
        events.append((event, payload))

    monkeypatch.setattr(trace_recorder, "emit", capture_emit)
    monkeypatch.setattr(MathModelWorkFlow, "_write_section", failed_write_section)

    asyncio.run(
        MathModelWorkFlow().execute(
            Problem(task_id="workflow-writer-failed", ques_all="原始题面")
        )
    )

    manifest = load_task_manifest(tmp_path)
    assert manifest is not None
    assert manifest["status"] == "partial_failure"
    assert "eda" not in manifest["completed_phases"]
    assert "ques1" not in manifest["completed_phases"]
    assert "eda" not in writer_calls
    assert "ques1" not in writer_calls

    phase_end = {
        payload["phase"]: payload
        for event, payload in events
        if event == "phase.end" and payload.get("stage") is None
    }
    assert phase_end["eda"]["success"] is False
    assert phase_end["ques1"]["success"] is False


def test_workflow_manifest_keeps_full_phase_paths_outside_truncated_packet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    paths = [f"results/artifact_{index:04d}.csv" for index in range(240)]
    for relative in paths:
        artifact = tmp_path / relative
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"artifact")

    monkeypatch.setattr(workflow_module.trace_recorder, "emit", _noop_async)
    workflow = MathModelWorkFlow()
    workflow.task_id = "workflow-manifest-truncation"
    workflow.work_dir = str(tmp_path)
    workflow._manifest_completed_phases = []
    workflow._manifest_failures = []
    workflow._manifest_phase_artifacts = {}

    packet = asyncio.run(
        workflow._save_phase_result(
            phase="ques1",
            status="success",
            started_at="2026-08-30T00:00:00Z",
            ended_at="2026-08-30T00:01:00Z",
            code_interpreter=_ExtremePacketInterpreter(paths),
        )
    )
    packet_text = json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
    packet_paths = {item["path"] for item in packet["artifacts"]}
    omitted_paths = set(paths) - packet_paths

    assert len(packet_text) <= PHASE_RESULT_BUDGET
    assert packet["truncated"] is True
    assert omitted_paths
    assert workflow._manifest_phase_artifacts["ques1"] == set(paths)
    assert "phase_artifacts" not in packet
    assert all(path not in packet_text for path in omitted_paths)

    workflow._refresh_manifest(status="completed", completed_phases=["ques1"])
    manifest = load_task_manifest(tmp_path)

    assert manifest is not None
    by_path = {item["path"]: item for item in manifest["artifacts"]}
    assert all(by_path[path]["phase"] == "ques1" for path in paths)


def test_writer_handoff_trace_filters_raw_problem_and_keeps_metric(
    monkeypatch: pytest.MonkeyPatch,
):
    raw_problem = (
        "研究三种作物的种植安排并最小化总成本与资源浪费同时满足土地和水资源约束"
    )
    workflow = MathModelWorkFlow()
    workflow.task_id = "writer-handoff-raw-problem"
    workflow._raw_problem = raw_problem
    events: list[tuple[str, dict]] = []

    async def capture_emit(task_id: str, event: str, **payload):
        events.append((event, payload))

    monkeypatch.setattr(trace_recorder, "emit", capture_emit)

    asyncio.run(
        workflow._emit_writer_handoff(
            "ques1",
            f"{raw_problem}。accuracy=0.91。",
        )
    )

    handoff = next(payload for event, payload in events if event == "writer.handoff")
    serialized = json.dumps(handoff, ensure_ascii=False)
    assert raw_problem not in serialized
    assert "raw_problem" not in serialized
    assert "accuracy=0.91" in handoff["prompt"]


def test_main_workflow_keeps_inspection_contract_before_modeler():
    source = Path(workflow_module.__file__).read_text(encoding="utf-8")
    positions = [
        source.index("build_source_inspection("),
        source.index("coder_agent.run("),
        source.index("build_data_contract("),
        source.index("validate_data_contract("),
        source.index("modeler_agent = ModelerAgent("),
    ]

    assert positions == sorted(positions)


def test_contract_failure_isolated_before_modeler_and_original_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    source = tmp_path / "附件1.csv"
    source.write_text("x\n1\n2\n", encoding="utf-8")
    source_before = source.read_bytes()
    modeler_calls = 0

    class FakeCoordinator:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, ques_all: str):
            return CoordinatorToModeler(
                questions={
                    "background": "背景",
                    "ques_count": 1,
                    "ques1": "求解",
                },
                ques_count=1,
            )

    class FakeCoder:
        def __init__(self, *args, **kwargs):
            pass

        def set_data_brief(self, brief: str) -> None:
            return None

        async def run(self, prompt: str, subtask_title: str):
            return CoderToWriter(
                status="success",
                code_response="没有生成 cleaned csv",
                created_images=[],
            )

        def reset_for_solve(self, brief: str) -> None:
            return None

    class FakeModeler:
        def __init__(self, *args, **kwargs):
            nonlocal modeler_calls
            modeler_calls += 1

        async def run(self, *args, **kwargs):
            raise AssertionError("data contract 失败后不应进入 Modeler")

    class FakeWriter:
        def __init__(self, *args, **kwargs):
            pass

    class FakeFactory:
        def __init__(self, task_id: str):
            pass

        def get_all_llms(self):
            return (_FakeLLM(), _FakeLLM(), _FakeLLM(), _FakeLLM())

    async def fake_interpreter(*args, **kwargs):
        return _FakeInterpreter()

    monkeypatch.setattr(workflow_module, "create_work_dir", lambda task_id: str(tmp_path))
    monkeypatch.setattr(workflow_module, "CoordinatorAgent", FakeCoordinator)
    monkeypatch.setattr(workflow_module, "CoderAgent", FakeCoder)
    monkeypatch.setattr(workflow_module, "ModelerAgent", FakeModeler)
    monkeypatch.setattr(workflow_module, "WriterAgent", FakeWriter)
    monkeypatch.setattr(workflow_module, "LLMFactory", FakeFactory)
    monkeypatch.setattr(workflow_module, "create_interpreter", fake_interpreter)
    monkeypatch.setattr(
        workflow_module,
        "NotebookSerializer",
        lambda work_dir: object(),
    )
    monkeypatch.setattr(
        workflow_module,
        "get_config_template",
        lambda template: {},
    )
    monkeypatch.setattr(
        workflow_module,
        "OpenAlexScholar",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        workflow_module.redis_manager,
        "publish_message",
        _noop_async,
    )
    monkeypatch.setattr(workflow_module.trace_recorder, "emit", _noop_async)
    for setting_name in (
        "COORDINATOR_MODEL",
        "MODELER_MODEL",
        "CODER_MODEL",
        "WRITER_MODEL",
        "COORDINATOR_API_KEY",
        "MODELER_API_KEY",
        "CODER_API_KEY",
        "WRITER_API_KEY",
    ):
        monkeypatch.setattr(workflow_module.settings, setting_name, "configured")
    monkeypatch.setattr(workflow_module.settings, "OPENALEX_EMAIL", "test@example.com")

    with pytest.raises(Exception, match="readable sheet coverage 不完整"):
        asyncio.run(
            MathModelWorkFlow().execute(
                Problem(task_id="workflow-contract-failure", ques_all="原始题面")
            )
        )

    assert modeler_calls == 0
    assert source.read_bytes() == source_before
    assert (tmp_path / "source_inspection.json").is_file()
    packet = json.loads(
        (tmp_path / "phase_results" / "eda.json").read_text(encoding="utf-8")
    )
    assert packet["status"] == "failed"
    assert packet["evidence_status"] == "missing"
    assert "readable sheet coverage 不完整" in packet["error_summary"]
    manifest = load_task_manifest(tmp_path)
    assert manifest is not None
    assert manifest["status"] == "failed"
    assert manifest["failed_phase"] == "eda"


def test_eda_coder_failure_writes_failed_packet_before_modeler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    (tmp_path / "source.csv").write_text("x\n1\n", encoding="utf-8")
    modeler_calls = 0
    writer_calls = 0

    class FakeCoordinator:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, ques_all: str):
            return CoordinatorToModeler(
                questions={
                    "background": "背景",
                    "ques_count": 1,
                    "ques1": "求解",
                },
                ques_count=1,
            )

    class FakeCoder:
        def __init__(self, *args, **kwargs):
            pass

        def set_data_brief(self, brief: str) -> None:
            return None

        async def run(self, prompt: str, subtask_title: str):
            return CoderToWriter(
                status="failed",
                code_response="cleaning interrupted",
                created_images=[],
            )

    class FakeModeler:
        def __init__(self, *args, **kwargs):
            nonlocal modeler_calls
            modeler_calls += 1

    class FakeWriter:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, *args, **kwargs):
            nonlocal writer_calls
            writer_calls += 1
            return WriterResponse(response_content="unexpected writer output")

    _patch_workflow_runtime(
        monkeypatch,
        tmp_path,
        coordinator=FakeCoordinator,
        coder=FakeCoder,
        modeler=FakeModeler,
        writer=FakeWriter,
    )

    with pytest.raises(Exception, match="EDA Coder failed"):
        asyncio.run(
            MathModelWorkFlow().execute(
                Problem(task_id="workflow-eda-coder-failure", ques_all="原始题面")
            )
        )

    assert modeler_calls == 0
    assert writer_calls == 0
    packet = json.loads(
        (tmp_path / "phase_results" / "eda.json").read_text(encoding="utf-8")
    )
    assert packet["status"] == "failed"
    assert packet["evidence_status"] == "missing"
    assert "EDA Coder failed (failed)" in packet["error_summary"]
    manifest = load_task_manifest(tmp_path)
    assert manifest is not None
    assert manifest["status"] == "failed"
    assert manifest["failed_phase"] == "eda"


def test_modeler_failure_writes_packet_and_manifest_without_writer_solution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    (tmp_path / "preexisting.png").write_bytes(b"prior phase artifact")
    writer_sections: list[str | None] = []

    class FakeCoordinator:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, ques_all: str):
            return CoordinatorToModeler(
                questions={
                    "background": "背景",
                    "ques_count": 1,
                    "ques1": "求解",
                },
                ques_count=1,
            )

    class FakeCoder:
        def __init__(self, *args, **kwargs):
            pass

        def set_data_brief(self, brief: str) -> None:
            return None

        def reset_for_solve(self, brief: str) -> None:
            return None

    class FakeModeler:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, *args, **kwargs):
            raise RuntimeError("modeler service unavailable")

    class FakeWriter:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, prompt: str, sub_title: str | None = None, **kwargs):
            writer_sections.append(sub_title)
            return WriterResponse(response_content=f"written {sub_title}")

    _patch_workflow_runtime(
        monkeypatch,
        tmp_path,
        coordinator=FakeCoordinator,
        coder=FakeCoder,
        modeler=FakeModeler,
        writer=FakeWriter,
    )

    with pytest.raises(RuntimeError, match="modeler service unavailable"):
        asyncio.run(
            MathModelWorkFlow().execute(
                Problem(task_id="workflow-modeler-failure", ques_all="原始题面")
            )
        )

    packet = json.loads(
        (tmp_path / "phase_results" / "modeler.json").read_text(encoding="utf-8")
    )
    eda_packet = json.loads(
        (tmp_path / "phase_results" / "eda.json").read_text(encoding="utf-8")
    )
    assert eda_packet["artifacts"] == []
    assert packet["phase"] == "modeler"
    assert packet["status"] == "failed"
    assert packet["evidence_status"] == "missing"
    assert packet["error_summary"] == "modeler service unavailable"
    assert packet["artifacts"] == []
    assert writer_sections == ["eda"]

    manifest = load_task_manifest(tmp_path)
    assert manifest is not None
    assert manifest["status"] == "partial_failure"
    assert manifest["failed_phase"] == "modeler"
    assert "eda" in manifest["completed_phases"]
    assert "modeler" not in manifest["completed_phases"]
    modeler_entry = next(
        item
        for item in manifest["artifacts"]
        if item["path"] == "phase_results/modeler.json"
    )
    assert modeler_entry["status"] == "failed"


def test_coder_failure_skips_exact_writer_section_and_keeps_partial_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    raw_problem = (
        "研究三种作物的种植安排并最小化总成本与资源浪费同时满足土地和水资源约束"
    )
    coder_phases: list[str] = []
    writer_sections: list[str | None] = []

    class FakeCoordinator:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, ques_all: str):
            return CoordinatorToModeler(
                questions={
                    "background": "背景",
                    "ques_count": 1,
                    "ques1": "求解",
                },
                ques_count=1,
            )

    class FakeCoder:
        def __init__(self, *args, **kwargs):
            pass

        def set_data_brief(self, brief: str) -> None:
            return None

        def reset_for_solve(self, brief: str) -> None:
            return None

        async def run(self, prompt: str, subtask_title: str):
            coder_phases.append(subtask_title)
            return CoderToWriter(
                status="failed",
                code_response=f"{raw_problem}。accuracy=0.99，仅有部分输出",
                created_images=[],
            )

    class FakeModeler:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, *args, **kwargs):
            return ModelerToCoder(
                questions_solution={
                    "ques1": "模型方案",
                    "sensitivity_analysis": "敏感性方案",
                }
            )

    class FakeWriter:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, prompt: str, sub_title: str | None = None, **kwargs):
            writer_sections.append(sub_title)
            return WriterResponse(response_content=f"written {sub_title}")

    _patch_workflow_runtime(
        monkeypatch,
        tmp_path,
        coordinator=FakeCoordinator,
        coder=FakeCoder,
        modeler=FakeModeler,
        writer=FakeWriter,
    )

    asyncio.run(
        MathModelWorkFlow().execute(
            Problem(task_id="workflow-coder-failure", ques_all=raw_problem)
        )
    )

    assert coder_phases == ["ques1", "sensitivity_analysis"]
    assert "ques1" not in writer_sections
    assert "sensitivity_analysis" not in writer_sections

    for phase in ("ques1", "sensitivity_analysis"):
        packet = json.loads(
            (tmp_path / "phase_results" / f"{phase}.json").read_text(
                encoding="utf-8"
            )
        )
        assert packet["status"] == "failed"
        assert packet["evidence_status"] == "missing"
        assert raw_problem not in json.dumps(packet, ensure_ascii=False)

    result = json.loads((tmp_path / "res.json").read_text(encoding="utf-8"))
    assert raw_problem not in json.dumps(result, ensure_ascii=False)
    assert "未完成" in result["ques1"]["response_content"]
    assert "精确结果" in result["ques1"]["response_content"]
    assert "accuracy=0.99" not in result["ques1"]["response_content"]

    manifest = load_task_manifest(tmp_path)
    assert manifest is not None
    assert manifest["status"] == "partial_failure"
    assert manifest["failed_phase"] == "ques1"
    assert "ques1" not in manifest["completed_phases"]
    assert "sensitivity_analysis" not in manifest["completed_phases"]
    packet_entries = {
        item["path"]: item
        for item in manifest["artifacts"]
        if item["kind"] == "phase_result"
    }
    assert packet_entries["phase_results/ques1.json"]["status"] == "failed"
    assert (
        packet_entries["phase_results/sensitivity_analysis.json"]["status"] == "failed"
    )
    assert raw_problem not in json.dumps(manifest, ensure_ascii=False)
