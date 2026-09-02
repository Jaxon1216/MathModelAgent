"""工作流模块，编排多 Agent 协作完成数学建模任务。"""

import asyncio
import json
import os
import time
from datetime import UTC, datetime

from app.core.agents import WriterAgent, CoderAgent, CoordinatorAgent, ModelerAgent
from app.schemas.request import Problem
from app.schemas.response import SystemMessage
from app.schemas.A2A import WriterResponse
from app.tools.openalex_scholar import OpenAlexScholar
from app.utils.log_util import logger
from app.utils.common_utils import create_work_dir, get_config_template
from app.utils.data_contract import (
    DataContractError,
    build_data_contract,
    cleaned_dir,
    format_data_prep_brief,
    format_no_data_brief,
    has_source_data,
    list_source_data_files,
    render_contract_for_llm,
    render_contract_summary_for_writer,
    save_data_contract,
    validate_data_contract,
)
from app.utils.problem_context import (
    find_constraint_echoes,
    redact_execution_constraints,
    redact_raw_problem_echoes,
)
from app.utils.source_inspection import (
    build_source_inspection,
    render_source_inspection,
    save_source_inspection,
)
from app.utils.phase_results import (
    WRITER_PROMPT_BUDGET,
    bound_writer_user_prompt,
    build_phase_result,
    save_phase_result,
    validate_phase_result,
)
from app.utils.task_manifest import update_task_manifest
from app.models.user_output import UserOutput
from app.config.setting import settings
from app.tools.interpreter_factory import create_interpreter
from app.services.redis_manager import redis_manager
from app.services.trace_recorder import set_trace_phase, trace_recorder
from app.tools.notebook_serializer import NotebookSerializer
from app.core.flows import Flows
from app.core.llm.llm_factory import LLMFactory


class WorkFlow:
    """工作流基类。"""

    def __init__(self):
        pass

    def execute(self) -> None:
        """执行工作流。"""
        # RichPrinter.workflow_start()
        # RichPrinter.workflow_end()
        pass


class MathModelWorkFlow(WorkFlow):
    """数学建模工作流，协调协调者、建模手、代码手和写作手完成完整建模任务。"""

    task_id: str  #
    work_dir: str  # worklow work dir
    ques_count: int = 0  # 问题数量
    questions: dict[str, str | int] = {}  # 问题
    cancel_event: asyncio.Event | None = None  # 取消信号
    _manifest_completed_phases: list[str]
    _manifest_failures: list[dict[str, str]]
    _manifest_phase_artifacts: dict[str, set[str]]
    _raw_problem: str

    def _refresh_manifest(
        self,
        *,
        status: str = "running",
        current_phase: str | None = None,
        completed_phases: list[str] | None = None,
        failed_phase: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        """刷新 manifest；索引故障不能阻断主工作流。"""
        if completed_phases:
            for phase in completed_phases:
                if phase not in self._manifest_completed_phases:
                    self._manifest_completed_phases.append(phase)
        if self._manifest_failures and status == "running":
            status = "partial_failure"
            failed_phase = failed_phase or self._manifest_failures[0]["phase"]
            failure_reason = failure_reason or self._manifest_failures[0]["reason"]
        try:
            update_task_manifest(
                self.work_dir,
                task_id=self.task_id,
                status=status,
                current_phase=current_phase,
                completed_phases=self._manifest_completed_phases,
                failed_phase=failed_phase,
                failure_reason=failure_reason,
                phase_artifacts=self._manifest_phase_artifacts,
            )
        except Exception as exc:
            logger.warning(f"任务 manifest 更新失败，不影响主流程: {exc}")

    def _record_manifest_failure(self, phase: str, reason: str) -> None:
        """记录 phase 失败并保留此前已完成阶段。"""
        failure = {"phase": phase, "reason": reason or "unavailable"}
        if not any(item["phase"] == phase for item in self._manifest_failures):
            self._manifest_failures.append(failure)
        status = (
            "partial_failure"
            if self._manifest_completed_phases
            else "failed"
        )
        first_failure = self._manifest_failures[0]
        self._refresh_manifest(
            status=status,
            current_phase=phase,
            failed_phase=first_failure["phase"],
            failure_reason=first_failure["reason"],
        )

    async def _check_cancelled(self) -> None:
        """检查是否收到取消信号，若已取消则发布通知并抛出 CancelledError。"""
        if self.cancel_event and self.cancel_event.is_set():
            await redis_manager.publish_message(
                self.task_id,
                SystemMessage(content="任务已停止", type="warning"),
            )
            raise asyncio.CancelledError("任务被用户停止")

    async def _write_section(
        self,
        key: str,
        writer_agent: WriterAgent,
        user_output: UserOutput,
        writer_prompt: str,
        available_images: list[str] | None = None,
        constraints: list[str] | None = None,
    ) -> WriterResponse:
        """调用写作手并写入 UserOutput。"""
        final_prompt, handoff_evidence = self._prepare_writer_prompt(
            writer_prompt,
            available_images,
            constraints=constraints,
        )
        await self._emit_writer_handoff(
            key,
            final_prompt,
            original_prompt_chars=len(writer_prompt),
            constraints=constraints,
            evidence=handoff_evidence,
        )
        if not handoff_evidence["send_allowed"]:
            writer_response = self._failed_writer_response(
                key,
                "执行约束或过程回声在清洗后仍存在，已阻断原始 Writer 材料。",
            )
            user_output.set_res(key, writer_response)
            await redis_manager.publish_message(
                self.task_id,
                SystemMessage(content=f"论文手跳过{key}部分：交接材料不安全", type="error"),
            )
            return writer_response
        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content=f"论文手开始写{key}部分"),
        )
        writer_response = await writer_agent.run(
            final_prompt,
            sub_title=key,
        )
        user_output.set_res(key, writer_response)
        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content=f"论文手完成{key}部分"),
        )
        return writer_response

    def _prepare_writer_prompt(
        self,
        writer_prompt: str,
        available_images: list[str] | None = None,
        *,
        constraints: list[str] | None = None,
    ) -> tuple[str, dict[str, object]]:
        """清洗 Writer 材料并确定是否允许发送。

        原始题面和执行约束都只作为内存中的比较基准；trace 只接收清洗后的
        文本及计数证据。若确定性清洗后仍有过程回声，调用方必须 fail-closed。
        """
        bounded_prompt = bound_writer_user_prompt(
            writer_prompt,
            available_images,
            WRITER_PROMPT_BUDGET,
        )
        cleaned_prompt = redact_execution_constraints(bounded_prompt, constraints)
        cleaned_prompt = redact_raw_problem_echoes(
            cleaned_prompt,
            getattr(self, "_raw_problem", ""),
        )
        residual_echoes = find_constraint_echoes(cleaned_prompt, constraints)
        safe_placeholder_used = bool(residual_echoes)
        if safe_placeholder_used:
            cleaned_prompt = (
                "Writer material unavailable. "
                "Use only verified phase facts supplied by a later handoff."
            )
        return cleaned_prompt, {
            "redaction_applied": cleaned_prompt != bounded_prompt,
            "residual_echo_count": len(residual_echoes),
            "residual_echoes": residual_echoes[:5],
            "send_allowed": not residual_echoes,
            "safe_placeholder_used": safe_placeholder_used,
        }

    @staticmethod
    def _failed_writer_response(key: str, reason: str) -> WriterResponse:
        """生成明确的 Writer 交接失败响应，不暴露被阻断的原文。"""
        return WriterResponse(
            status="failed",
            response_content=(
                f"{key} 章节未生成。Writer 交接被安全检查阻断；"
                f"{reason}请依据后续可验证材料重试。"
            ),
        )

    async def _emit_writer_handoff(
        self,
        key: str,
        writer_prompt: str,
        *,
        original_prompt_chars: int | None = None,
        constraints: list[str] | None = None,
        evidence: dict[str, object] | None = None,
    ) -> None:
        """记录 Writer 实际交接文本、预算和结构化泄漏指标。"""
        writer_prompt = redact_execution_constraints(writer_prompt, constraints)
        writer_prompt = redact_raw_problem_echoes(
            writer_prompt,
            getattr(self, "_raw_problem", ""),
        )
        echoes = find_constraint_echoes(writer_prompt, constraints)
        prepared_send_allowed = (
            bool((evidence or {}).get("send_allowed", True)) and not echoes
        )
        prepared_residual_count = (evidence or {}).get(
            "residual_echo_count", len(echoes)
        )
        if not isinstance(prepared_residual_count, int):
            prepared_residual_count = len(echoes)
        prompt_truncated = (
            original_prompt_chars is not None
            and len(writer_prompt) < original_prompt_chars
        )
        handoff_payload = {
            "prompt": writer_prompt,
            "prompt_chars": len(writer_prompt),
            "material_budget": WRITER_PROMPT_BUDGET,
            "original_prompt_chars": original_prompt_chars,
            "prompt_truncated": prompt_truncated,
            "constraint_leakage": bool(echoes),
            "constraint_echo_count": len(echoes),
            "redaction_applied": bool((evidence or {}).get("redaction_applied")),
            "residual_echo_count": len(echoes),
            "send_allowed": prepared_send_allowed,
            "redaction_evidence": {
                "safe_placeholder_used": bool(
                    (evidence or {}).get("safe_placeholder_used")
                ),
                "prepared_residual_echo_count": prepared_residual_count,
                "blocked_after_redaction": not prepared_send_allowed,
            },
        }
        await trace_recorder.emit(
            self.task_id,
            "writer.handoff",
            phase=key,
            **handoff_payload,
        )
        await trace_recorder.emit(
            self.task_id,
            "handoff.budget",
            phase=key,
            kind="writer_material",
            chars=len(writer_prompt),
            budget=WRITER_PROMPT_BUDGET,
            truncated=prompt_truncated,
        )

    async def _save_phase_result(
        self,
        *,
        phase: str,
        status: str,
        started_at: str,
        ended_at: str,
        code_interpreter,
        coder_response=None,
        error: str = "",
        model_reference: str | None = None,
        constraints: list[str] | None = None,
        public_context: str | None = None,
        inspection_text: str | None = None,
    ) -> dict:
        """落盘并校验单个 Coder phase 的 bounded result packet。"""
        try:
            artifact_paths = code_interpreter.get_section_artifacts(phase)
        except AttributeError:
            artifact_paths = []
        self._manifest_phase_artifacts.setdefault(phase, set()).update(
            path.strip()
            for path in artifact_paths
            if isinstance(path, str) and path.strip()
        )
        packet = build_phase_result(
            self.work_dir,
            task_id=self.task_id,
            phase=phase,
            status=status,
            started_at=started_at,
            ended_at=ended_at,
            interpreter=code_interpreter,
            coder_summary=(
                coder_response.code_response
                if coder_response is not None
                else ""
            ),
            error=error,
            model_reference=model_reference,
            constraints=constraints,
            public_context=public_context,
            raw_problem=getattr(self, "_raw_problem", ""),
            inspection_text=inspection_text,
        )
        relpath = save_phase_result(self.work_dir, packet)
        errors = validate_phase_result(self.work_dir, packet)
        await trace_recorder.emit(
            self.task_id,
            "phase.result",
            phase=phase,
            path=relpath,
            status=packet.get("status"),
            evidence_status=packet.get("evidence_status"),
            artifact_count=len(packet.get("artifacts") or []),
            image_count=len(packet.get("images") or []),
            stdout_chars=len(packet.get("stdout") or ""),
            packet_chars=len(json.dumps(packet, ensure_ascii=False)),
            validation_errors=errors,
            packet_budget=6000,
        )
        if coder_response is not None:
            coder_response.phase_result = packet
        return packet

    def _set_failed_phase_output(
        self,
        user_output: UserOutput,
        phase: str,
        packet: dict,
    ) -> None:
        """为失败求解阶段写入确定性的限制说明，避免 Writer 编造结果。"""
        reason = str(packet.get("error_summary") or "").strip()
        if not reason or reason == "unavailable":
            reason = f"phase status: {packet.get('status', 'failed')}"
        user_output.set_res(
            phase,
            WriterResponse(
                response_content=(
                    f"{phase} 阶段未完成。由于执行失败，"
                    "无法提供经过验证的精确结果或图表结论。"
                    f"失败原因：{reason}"
                )
            ),
        )

    async def execute(self, problem: Problem):  # type: ignore[reportIncompatibleMethodOverride]
        """执行数学建模工作流。

        Args:
            problem: 包含题目信息、模板配置等的 Problem 对象。
        """
        self.task_id = problem.task_id
        self._raw_problem = problem.ques_all
        self.work_dir = create_work_dir(self.task_id)
        self._manifest_completed_phases = []
        self._manifest_failures = []
        self._manifest_phase_artifacts = {}
        self._refresh_manifest(status="running")

        await trace_recorder.emit(
            self.task_id,
            "task.start",
            work_dir=self.work_dir,
        )

        # 在创建 LLM 前预校验配置，避免进入 Agent 循环后才发现缺配置
        missing = []
        for name, model_val, key_val in [
            ("Coordinator", settings.COORDINATOR_MODEL, settings.COORDINATOR_API_KEY),
            ("Modeler", settings.MODELER_MODEL, settings.MODELER_API_KEY),
            ("Coder", settings.CODER_MODEL, settings.CODER_API_KEY),
            ("Writer", settings.WRITER_MODEL, settings.WRITER_API_KEY),
        ]:
            if not model_val or not str(model_val).strip():
                missing.append(f"{name} 模型 ID")
            if not key_val or not str(key_val).strip():
                missing.append(f"{name} API Key")
        if missing:
            error = f"以下配置缺失，请先在设置中填写并保存：{', '.join(missing)}"
            self._record_manifest_failure("initialization", error)
            raise ValueError(error)

        llm_factory = LLMFactory(self.task_id)
        coordinator_llm, modeler_llm, coder_llm, writer_llm = llm_factory.get_all_llms()

        coordinator_agent = CoordinatorAgent(
            self.task_id,
            coordinator_llm,
            context_window=settings.COORDINATOR_CONTEXT_WINDOW,
            cancel_event=self.cancel_event,
        )
        await trace_recorder.emit(
            self.task_id,
            "agent.init",
            agent="CoordinatorAgent",
            agent_class="CoordinatorAgent",
            model=coordinator_llm.model,
            context_window=settings.COORDINATOR_CONTEXT_WINDOW,
        )

        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content="识别用户意图和拆解问题ing..."),
        )

        await self._check_cancelled()

        try:
            coordinator_response = await coordinator_agent.run(problem.ques_all)
            self.questions = coordinator_response.questions
            self.ques_count = coordinator_response.ques_count
        except Exception as e:
            #  非数学建模问题
            logger.error(f"CoordinatorAgent 执行失败: {e}")
            self._record_manifest_failure("coordinator", str(e))
            raise e

        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content="识别用户意图和拆解问题完成"),
        )

        user_output = UserOutput(work_dir=self.work_dir, ques_count=self.ques_count)

        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content="正在创建代码沙盒环境"),
        )

        notebook_serializer = NotebookSerializer(work_dir=self.work_dir)
        code_interpreter = await create_interpreter(
            kind="local",
            task_id=self.task_id,
            work_dir=self.work_dir,
            notebook_serializer=notebook_serializer,
            timeout=3000,
        )
        await trace_recorder.emit(
            self.task_id,
            "interpreter.init",
            interpreter_type="local",
            work_dir=self.work_dir,
        )

        assert settings.OPENALEX_EMAIL is not None, "OPENALEX_EMAIL 未配置"
        scholar = OpenAlexScholar(
            task_id=self.task_id,
            email=settings.OPENALEX_EMAIL,
            api_key=settings.OPENALEX_API_KEY,
        )

        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content="创建完成"),
        )

        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content="初始化代码手"),
        )

        coder_agent = CoderAgent(
            task_id=problem.task_id,
            model=coder_llm,
            work_dir=self.work_dir,
            max_chat_turns=settings.MAX_CHAT_TURNS,
            max_retries=settings.MAX_RETRIES,
            code_interpreter=code_interpreter,
            context_window=settings.CODER_CONTEXT_WINDOW,
            cancel_event=self.cancel_event,
        )
        await trace_recorder.emit(
            self.task_id,
            "agent.init",
            agent="CoderAgent",
            agent_class="CoderAgent",
            model=coder_llm.model,
            context_window=settings.CODER_CONTEXT_WINDOW,
        )

        writer_agent = WriterAgent(
            task_id=problem.task_id,
            model=writer_llm,
            comp_template=problem.comp_template,
            format_output=problem.format_output,
            scholar=scholar,
            context_window=settings.WRITER_CONTEXT_WINDOW,
            cancel_event=self.cancel_event,
        )
        await trace_recorder.emit(
            self.task_id,
            "agent.init",
            agent="WriterAgent",
            agent_class="WriterAgent",
            model=writer_llm.model,
            context_window=settings.WRITER_CONTEXT_WINDOW,
        )

        flows = Flows(
            self.questions,
            coordinator_response.constraints,
            raw_problem=self._raw_problem,
        )
        config_template = get_config_template(problem.comp_template)

        ################################################ data prep then modeler then solve
        data_brief = format_no_data_brief()
        phase_results: dict[str, dict] = {}

        if has_source_data(self.work_dir):
            os.makedirs(cleaned_dir(self.work_dir), exist_ok=True)
            source_files = list_source_data_files(self.work_dir)
            set_trace_phase("eda")
            phase_started = time.monotonic()
            phase_started_at = datetime.now(UTC).isoformat()
            await trace_recorder.emit(self.task_id, "phase.start", phase="eda")
            phase_success = True
            inspection_path = "source_inspection.json"
            inspection_written = False
            inspection_text = ""
            coder_response = None
            self._refresh_manifest(status="running", current_phase="eda")
            try:
                await self._check_cancelled()
                inspection = build_source_inspection(
                    self.work_dir,
                    task_id=self.task_id,
                    generated_at=datetime.now(UTC).isoformat(),
                )
                inspection_path = os.path.relpath(
                    save_source_inspection(self.work_dir, inspection),
                    self.work_dir,
                ).replace("\\", "/")
                inspection_written = True
                inspection_text = render_source_inspection(inspection)
                set_inspection_text = getattr(coder_agent, "set_inspection_text", None)
                if callable(set_inspection_text):
                    set_inspection_text(inspection_text)
                self._refresh_manifest(status="running", current_phase="eda")
                readable_sheet_count = sum(
                    1
                    for entry in inspection.get("files") or []
                    for sheet in entry.get("sheets") or []
                    if sheet.get("readable") is True
                )
                warning_count = sum(
                    len(entry.get("warnings") or [])
                    for entry in inspection.get("files") or []
                )
                await trace_recorder.emit(
                    self.task_id,
                    "source.inspection",
                    phase="eda",
                    path=inspection_path,
                    status=inspection.get("status"),
                    data_status=inspection.get("data_status"),
                    source_files=inspection.get("source_files") or [],
                    skipped_files=inspection.get("skipped_files") or [],
                    file_count=len(inspection.get("files") or []),
                    readable_sheet_count=readable_sheet_count,
                    warning_count=warning_count,
                    rendered_chars=len(inspection_text),
                    render_budget=12000,
                    rendered_truncated=len(inspection_text) >= 12000,
                )
                eda_brief = format_data_prep_brief(source_files)
                coder_agent.set_data_brief(eda_brief)
                await trace_recorder.emit(
                    self.task_id,
                    "handoff.budget",
                    phase="eda",
                    kind="data_prep_brief",
                    chars=len(eda_brief),
                    budget=12000,
                    truncated=len(eda_brief) >= 12000,
                    inspection_chars=0,
                    inspection_in_prompt=True,
                )
                await redis_manager.publish_message(
                    self.task_id,
                    SystemMessage(content="代码手开始数据准备"),
                )
                coder_response = await coder_agent.run(
                    prompt=flows.get_data_prep_prompt(inspection_text),
                    subtask_title="eda",
                )
                coder_status = getattr(coder_response, "status", "success")
                if coder_status != "success":
                    failure_detail = (
                        coder_response.code_response
                        or f"coder status: {coder_status}"
                    )
                    raise DataContractError(
                        f"EDA Coder failed ({coder_status}): {failure_detail}"
                    )
                await redis_manager.publish_message(
                    self.task_id,
                    SystemMessage(content="代码手数据准备完成", type="success"),
                )
                contract = build_data_contract(
                    self.work_dir,
                    cleaning_notes=coder_response.code_response or "",
                    source_inspection=inspection,
                )
                save_data_contract(self.work_dir, contract)
                validate_data_contract(self.work_dir, contract)
                data_brief = render_contract_for_llm(contract)
                inventory = render_contract_summary_for_writer(contract)
                await trace_recorder.emit(
                    self.task_id,
                    "handoff.budget",
                    phase="eda",
                    kind="data_contract",
                    chars=len(data_brief),
                    budget=8000,
                    truncated=len(data_brief) >= 8000,
                )
                packet = await self._save_phase_result(
                    phase="eda",
                    status="success",
                    started_at=phase_started_at,
                    ended_at=datetime.now(UTC).isoformat(),
                    code_interpreter=code_interpreter,
                    coder_response=coder_response,
                    model_reference="data_preparation",
                    constraints=flows.constraints,
                    public_context=flows.get_public_problem_context(),
                    inspection_text=inspection_text,
                )
                phase_results["eda"] = packet
                await trace_recorder.emit(
                    self.task_id,
                    "data.contract",
                    table_count=len(contract.get("tables") or []),
                    paths=[t.get("path") for t in contract.get("tables") or []],
                    source_inspection_path=inspection_path,
                    provenance_count=sum(
                        1
                        for table in contract.get("tables") or []
                        if table.get("provenance")
                    ),
                )
                writer_prompt = flows.get_writer_prompt(
                    "eda",
                    coder_response.code_response or "",
                    code_interpreter,
                    config_template,
                    data_inventory=inventory,
                    phase_result=packet,
                )
                writer_response = await self._write_section(
                    "eda",
                    writer_agent,
                    user_output,
                    writer_prompt,
                    available_images=packet.get("images") or [],
                    constraints=flows.constraints,
                )
                if getattr(writer_response, "status", "success") != "success":
                    phase_success = False
                    self._record_manifest_failure(
                        "eda",
                        "Writer returned status=failed",
                    )
                coder_agent.reset_for_solve(data_brief)
                if phase_success:
                    self._refresh_manifest(
                        status="running",
                        current_phase=None,
                        completed_phases=["eda"],
                    )
            except DataContractError as exc:
                phase_success = False
                logger.error(f"数据准备产物不合格: {exc}")
                packet = await self._save_phase_result(
                    phase="eda",
                    status="failed",
                    started_at=phase_started_at,
                    ended_at=datetime.now(UTC).isoformat(),
                    code_interpreter=code_interpreter,
                    coder_response=coder_response,
                    error=str(exc),
                    model_reference="data_preparation",
                    constraints=flows.constraints,
                    public_context=flows.get_public_problem_context(),
                    inspection_text=inspection_text,
                )
                phase_results["eda"] = packet
                await trace_recorder.emit(
                    self.task_id,
                    "data.contract.error",
                    phase="eda",
                    error=str(exc),
                    source_inspection_path=inspection_path,
                )
                self._record_manifest_failure("eda", str(exc))
                raise
            except Exception as exc:
                phase_success = False
                packet = await self._save_phase_result(
                    phase="eda",
                    status="failed",
                    started_at=phase_started_at,
                    ended_at=datetime.now(UTC).isoformat(),
                    code_interpreter=code_interpreter,
                    coder_response=coder_response,
                    error=str(exc),
                    model_reference="data_preparation",
                    constraints=flows.constraints,
                    public_context=flows.get_public_problem_context(),
                    inspection_text=inspection_text,
                )
                phase_results["eda"] = packet
                if not inspection_written:
                    await trace_recorder.emit(
                        self.task_id,
                        "source.inspection.error",
                        phase="eda",
                        path=inspection_path,
                        error=str(exc),
                    )
                self._record_manifest_failure("eda", str(exc))
                raise
            finally:
                await trace_recorder.emit(
                    self.task_id,
                    "phase.end",
                    phase="eda",
                    success=phase_success,
                    duration_ms=int((time.monotonic() - phase_started) * 1000),
                )
                set_trace_phase(None)
        else:
            coder_agent.set_data_brief(data_brief)
            await self._check_cancelled()
            set_trace_phase("eda")
            phase_started = time.monotonic()
            phase_started_at = datetime.now(UTC).isoformat()
            await trace_recorder.emit(self.task_id, "phase.start", phase="eda")
            self._refresh_manifest(status="running", current_phase="eda")
            try:
                packet = await self._save_phase_result(
                    phase="eda",
                    status="not_applicable",
                    started_at=phase_started_at,
                    ended_at=datetime.now(UTC).isoformat(),
                    code_interpreter=code_interpreter,
                    coder_response=None,
                    model_reference="no_source_data",
                    public_context=flows.get_public_problem_context(),
                )
                phase_results["eda"] = packet
                writer_prompt = flows.get_writer_prompt(
                    "eda",
                    "未发现表格数据。",
                    code_interpreter,
                    config_template,
                    data_inventory=data_brief,
                    phase_result=packet,
                )
                writer_response = await self._write_section(
                    "eda",
                    writer_agent,
                    user_output,
                    writer_prompt,
                    constraints=flows.constraints,
                )
                if getattr(writer_response, "status", "success") == "success":
                    self._refresh_manifest(
                        status="running",
                        current_phase=None,
                        completed_phases=["eda"],
                    )
                else:
                    self._record_manifest_failure(
                        "eda",
                        "Writer returned status=failed",
                    )
            except Exception as exc:
                self._record_manifest_failure("eda", str(exc))
                raise
            finally:
                await trace_recorder.emit(
                    self.task_id,
                    "phase.end",
                    phase="eda",
                    success="eda" in self._manifest_completed_phases,
                    duration_ms=int((time.monotonic() - phase_started) * 1000),
                )
                set_trace_phase(None)

        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content="建模手开始建模ing..."),
        )

        await self._check_cancelled()

        set_trace_phase("modeler")
        modeler_started = time.monotonic()
        modeler_started_at = datetime.now(UTC).isoformat()
        modeler_success = False
        await trace_recorder.emit(self.task_id, "phase.start", phase="modeler")
        self._refresh_manifest(status="running", current_phase="modeler")
        try:
            modeler_agent = ModelerAgent(
                self.task_id,
                modeler_llm,
                context_window=settings.MODELER_CONTEXT_WINDOW,
                cancel_event=self.cancel_event,
            )
            await trace_recorder.emit(
                self.task_id,
                "agent.init",
                agent="ModelerAgent",
                agent_class="ModelerAgent",
                model=modeler_llm.model,
                context_window=settings.MODELER_CONTEXT_WINDOW,
            )

            modeler_response = await modeler_agent.run(
                coordinator_response, data_brief=data_brief
            )
            modeler_success = True
            self._refresh_manifest(
                status="running",
                current_phase=None,
                completed_phases=["modeler"],
            )
        except Exception as exc:
            packet = await self._save_phase_result(
                phase="modeler",
                status="failed",
                started_at=modeler_started_at,
                ended_at=datetime.now(UTC).isoformat(),
                code_interpreter=code_interpreter,
                error=str(exc),
                model_reference="modeler",
                public_context=flows.get_public_problem_context(),
            )
            phase_results["modeler"] = packet
            await trace_recorder.emit(
                self.task_id,
                "phase.failure",
                phase="modeler",
                status=packet.get("status"),
                evidence_status=packet.get("evidence_status"),
                error=packet.get("error_summary"),
            )
            self._record_manifest_failure("modeler", str(exc))
            raise
        finally:
            await trace_recorder.emit(
                self.task_id,
                "phase.end",
                phase="modeler",
                success=modeler_success,
                duration_ms=int((time.monotonic() - modeler_started) * 1000),
            )
            set_trace_phase(None)

        solution_flows = flows.get_solution_flows(self.questions, modeler_response)
        for phase, metrics in flows.handoff_metrics.items():
            await trace_recorder.emit(
                self.task_id,
                "handoff.budget",
                phase=phase,
                kind="model_solution",
                chars=metrics["chars"],
                budget=metrics["budget"],
                truncated=metrics["truncated"],
            )

        for key, value in solution_flows.items():
            await self._check_cancelled()

            set_trace_phase(key)
            phase_started = time.monotonic()
            phase_started_at = datetime.now(UTC).isoformat()
            await trace_recorder.emit(
                self.task_id,
                "phase.start",
                phase=key,
            )
            self._refresh_manifest(status="running", current_phase=key)

            await redis_manager.publish_message(
                self.task_id,
                SystemMessage(content=f"代码手开始求解{key}"),
            )

            phase_success = True
            coder_response = None
            try:
                coder_response = await coder_agent.run(
                    prompt=value["coder_prompt"], subtask_title=key
                )
                coder_status = getattr(coder_response, "status", "success")
                packet = await self._save_phase_result(
                    phase=key,
                    status=coder_status,
                    started_at=phase_started_at,
                    ended_at=datetime.now(UTC).isoformat(),
                    code_interpreter=code_interpreter,
                    coder_response=coder_response,
                    model_reference=f"modeler_solution:{key}",
                    constraints=flows.constraints,
                    public_context=flows.get_public_problem_context(),
                )
                phase_results[key] = packet
                if coder_status != "success":
                    phase_success = False
                    self._record_manifest_failure(
                        key,
                        f"coder status: {coder_status}",
                    )
                    await trace_recorder.emit(
                        self.task_id,
                        "phase.evidence.missing",
                        phase=key,
                        status=coder_status,
                        evidence_status=packet.get("evidence_status"),
                    )
                await redis_manager.publish_message(
                    self.task_id,
                    SystemMessage(
                        content=(
                            f"代码手求解完成{key}"
                            if coder_status == "success"
                            else f"代码手求解失败{key}，结果证据不完整"
                        ),
                        type="success" if coder_status == "success" else "error",
                    ),
                )
                if coder_status != "success":
                    self._set_failed_phase_output(user_output, key, packet)
                    await trace_recorder.emit(
                        self.task_id,
                        "writer.skipped",
                        phase=key,
                        reason="phase_failed",
                        packet_status=packet.get("status"),
                    )
                    continue
                writer_prompt = flows.get_writer_prompt(
                    key,
                    coder_response.code_response or "",
                    code_interpreter,
                    config_template,
                    phase_result=packet,
                )
                writer_response = await self._write_section(
                    key,
                    writer_agent,
                    user_output,
                    writer_prompt,
                    available_images=packet.get("images") or [],
                    constraints=flows.constraints,
                )
                if getattr(writer_response, "status", "success") == "success":
                    self._refresh_manifest(
                        status="running",
                        current_phase=None,
                        completed_phases=[key],
                    )
                else:
                    phase_success = False
                    self._record_manifest_failure(
                        key,
                        "Writer returned status=failed",
                    )
            except Exception as exc:
                phase_success = False
                if coder_response is None:
                    packet = await self._save_phase_result(
                        phase=key,
                        status="failed",
                        started_at=phase_started_at,
                        ended_at=datetime.now(UTC).isoformat(),
                        code_interpreter=code_interpreter,
                        coder_response=None,
                        error=str(exc),
                        model_reference=f"modeler_solution:{key}",
                        constraints=flows.constraints,
                        public_context=flows.get_public_problem_context(),
                    )
                    phase_results[key] = packet
                    self._set_failed_phase_output(user_output, key, packet)
                    await trace_recorder.emit(
                        self.task_id,
                        "writer.skipped",
                        phase=key,
                        reason="coder_exception",
                        packet_status=packet.get("status"),
                    )
                else:
                    await trace_recorder.emit(
                        self.task_id,
                        "writer.error",
                        phase=key,
                        error=str(exc),
                    )
                self._record_manifest_failure(key, str(exc))
                # 单个求解或对应章节失败不应丢弃此前已完成的阶段。
                continue
            finally:
                await trace_recorder.emit(
                    self.task_id,
                    "phase.end",
                    phase=key,
                    success=phase_success,
                    duration_ms=int((time.monotonic() - phase_started) * 1000),
                )
                set_trace_phase(None)

        # 关闭沙盒

        await code_interpreter.cleanup()
        logger.info(user_output.get_res())

        ################################################ write steps

        write_flows = flows.get_write_flows(
            user_output,
            config_template,
            phase_results=phase_results,
        )
        for key, value in write_flows.items():
            await self._check_cancelled()

            set_trace_phase(key)
            phase_started = time.monotonic()
            await trace_recorder.emit(
                self.task_id,
                "phase.start",
                phase=key,
                stage="write",
            )
            self._refresh_manifest(status="running", current_phase=key)

            await redis_manager.publish_message(
                self.task_id,
                SystemMessage(content=f"论文手开始写{key}部分"),
            )

            try:
                final_prompt, handoff_evidence = self._prepare_writer_prompt(
                    value,
                    constraints=flows.constraints,
                )
                await self._emit_writer_handoff(
                    key,
                    final_prompt,
                    original_prompt_chars=len(value),
                    constraints=flows.constraints,
                    evidence=handoff_evidence,
                )
                if handoff_evidence["send_allowed"]:
                    writer_response = await writer_agent.run(
                        prompt=final_prompt,
                        sub_title=key,
                    )
                else:
                    writer_response = self._failed_writer_response(
                        key,
                        "执行约束或过程回声在清洗后仍存在，已阻断原始 Writer 材料。",
                    )
                user_output.set_res(key, writer_response)
                if (
                    handoff_evidence["send_allowed"]
                    and getattr(writer_response, "status", "success") == "success"
                ):
                    self._refresh_manifest(
                        status="running",
                        current_phase=None,
                        completed_phases=[key],
                    )
                else:
                    self._record_manifest_failure(
                        key,
                        (
                            "Writer handoff rejected after deterministic redaction"
                            if not handoff_evidence["send_allowed"]
                            else "Writer returned status=failed"
                        ),
                    )
            except Exception as exc:
                self._record_manifest_failure(key, str(exc))
                raise
            finally:
                await trace_recorder.emit(
                    self.task_id,
                    "phase.end",
                    phase=key,
                    stage="write",
                    success=key in self._manifest_completed_phases,
                    duration_ms=int((time.monotonic() - phase_started) * 1000),
                )
                set_trace_phase(None)

        logger.info(user_output.get_res())

        try:
            user_output.save_result()
        except Exception as exc:
            self._record_manifest_failure("export", str(exc))
            raise
        self._refresh_manifest(
            status="partial_failure" if self._manifest_failures else "completed",
            current_phase=None,
            failed_phase=(
                self._manifest_failures[0]["phase"]
                if self._manifest_failures
                else None
            ),
            failure_reason=(
                self._manifest_failures[0]["reason"]
                if self._manifest_failures
                else None
            ),
        )
