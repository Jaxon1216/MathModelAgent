"""工作流模块，编排多 Agent 协作完成数学建模任务。"""

import asyncio
import time

from app.agents.cleaning_planner import CleaningPlannerAgent
from app.agents.modeler import ModelerAgent
from app.core.agents.coder_agent import CoderAgent
from app.core.agents.coordinator_agent import CoordinatorAgent
from app.core.agents.writer_agent import WriterAgent
from app.data.contracts import DataContractIntegrityError
from app.domain.m15 import DataContract, TaskOutline
from app.orchestration.m15_workflow import M15Workflow, validate_contract_boundary
from app.orchestration.task_outline import TaskOutlineWorkflow
from app.runtime.llm.client import LegacyLLMClient
from app.runtime.tracing import LegacyStageTracer
from app.schemas.A2A import ModelerToCoder
from app.schemas.request import Problem as ApiProblem
from app.schemas.response import SystemMessage
from app.tools.openalex_scholar import OpenAlexScholar
from app.utils.log_util import logger
from app.utils.common_utils import create_work_dir, get_config_template
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
    task_outline: TaskOutline | None = None
    data_contract: DataContract | None = None
    cancel_event: asyncio.Event | None = None  # 取消信号

    async def _check_cancelled(self) -> None:
        """检查是否收到取消信号，若已取消则发布通知并抛出 CancelledError。"""
        if self.cancel_event and self.cancel_event.is_set():
            await redis_manager.publish_message(
                self.task_id,
                SystemMessage(content="任务已停止", type="warning"),
            )
            raise asyncio.CancelledError("任务被用户停止")

    async def execute(self, problem: ApiProblem):  # type: ignore[reportIncompatibleMethodOverride]
        """执行数学建模工作流。

        Args:
            problem: 包含题目信息、模板配置等的 Problem 对象。
        """
        self.task_id = problem.task_id
        self.work_dir = create_work_dir(self.task_id)

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
            raise ValueError(
                f"以下配置缺失，请先在设置中填写并保存：{', '.join(missing)}"
            )

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
            outline_result = await TaskOutlineWorkflow(
                coordinator_agent,
                tracer=LegacyStageTracer(),
            ).create_outline(
                task_id=problem.task_id,
                problem_text=problem.ques_all,
                work_dir=self.work_dir,
            )
            self.task_outline = outline_result.outline
            self.questions = outline_result.to_legacy_questions()
            self.ques_count = outline_result.outline.ques_count
        except Exception as e:
            #  非数学建模问题
            logger.error(f"CoordinatorAgent 执行失败: {e}")
            raise e

        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content="识别用户意图和拆解问题完成,任务转交给建模手"),
        )

        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content="建模手开始建模ing..."),
        )

        await self._check_cancelled()

        modeler_client = LegacyLLMClient(
            modeler_llm,
            cancel_event=self.cancel_event,
        )
        cleaning_planner = CleaningPlannerAgent(modeler_client)
        modeler_agent = ModelerAgent(
            modeler_client,
            max_repair_attempts=settings.MODELER_MAX_REPAIR_ATTEMPTS,
        )
        await trace_recorder.emit(
            self.task_id,
            "agent.init",
            agent="CleaningPlannerAgent",
            agent_class="CleaningPlannerAgent",
            model=modeler_llm.model,
            context_window=settings.MODELER_CONTEXT_WINDOW,
        )
        await trace_recorder.emit(
            self.task_id,
            "agent.init",
            agent="ModelerAgent",
            agent_class="ModelerAgent",
            model=modeler_llm.model,
            context_window=settings.MODELER_CONTEXT_WINDOW,
        )

        m15_result = await M15Workflow(
            cleaning_planner,
            modeler_agent,
            tracer=LegacyStageTracer(),
            cancel_check=self._check_cancelled,
        ).run(
            task_id=problem.task_id,
            work_dir=self.work_dir,
            task_facts=outline_result.task_facts,
            outline=outline_result.outline,
        )
        self.data_contract = m15_result.data_contract
        # 旧 Coder 尚未迁移；在唯一兼容边界将领域计划降级为原有文本交接。
        modeler_response = ModelerToCoder(questions_solution=m15_result.coder_handoff)

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
            timeout_seconds=settings.OPENALEX_TIMEOUT_SECONDS,
        )

        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content="创建完成"),
        )

        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content="初始化代码手"),
        )

        # modeler_agent
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
            max_tool_rounds=settings.WRITER_MAX_TOOL_ROUNDS,
            max_search_calls=settings.WRITER_MAX_SEARCH_CALLS,
        )
        await trace_recorder.emit(
            self.task_id,
            "agent.init",
            agent="WriterAgent",
            agent_class="WriterAgent",
            model=writer_llm.model,
            context_window=settings.WRITER_CONTEXT_WINDOW,
        )

        flows = Flows(self.questions, schedule=outline_result.schedule)

        ################################################ solution steps
        solution_flows = flows.get_solution_flows(
            self.questions,
            modeler_response,
            self.data_contract,
        )
        config_template = get_config_template(problem.comp_template)
        contract = self.data_contract
        assert contract is not None
        stage_tracer = LegacyStageTracer()

        for key, value in solution_flows.items():
            await self._check_cancelled()

            set_trace_phase(key)
            phase_started = time.monotonic()
            await trace_recorder.emit(
                self.task_id,
                "phase.start",
                phase=key,
            )

            await redis_manager.publish_message(
                self.task_id,
                SystemMessage(content=f"代码手开始求解{key}"),
            )

            phase_status = "success"
            failure_kind: str | None = None
            try:
                await validate_contract_boundary(
                    task_id=self.task_id,
                    work_dir=self.work_dir,
                    contract=contract,
                    phase=key,
                    boundary="before",
                    tracer=stage_tracer,
                )
                coder_response = await coder_agent.run(
                    prompt=value["coder_prompt"], subtask_title=key
                )
                await validate_contract_boundary(
                    task_id=self.task_id,
                    work_dir=self.work_dir,
                    contract=contract,
                    phase=key,
                    boundary="after_coder",
                    tracer=stage_tracer,
                )

                if coder_response.status == "partial":
                    phase_status = "degraded"
                    await redis_manager.publish_message(
                        self.task_id,
                        SystemMessage(
                            content=f"代码手部分完成{key}",
                            type="warning",
                        ),
                    )
                else:
                    await redis_manager.publish_message(
                        self.task_id,
                        SystemMessage(
                            content=f"代码手求解成功{key}",
                            type="success",
                        ),
                    )

                writer_prompt = flows.get_writer_prompt(
                    key,
                    coder_response.code_response or "",
                    code_interpreter,
                    config_template,
                )
                if coder_response.status == "partial":
                    writer_prompt += (
                        "\n本阶段状态为 partial。只能使用下列已验证材料撰写，"
                        "不得引用执行错误、未验证推断或把阶段描述为完全收敛。\n"
                        f"已验证指标：{coder_response.verified_metrics}\n"
                        f"已验证产物：{coder_response.created_artifacts}\n"
                        f"限制：{coder_response.limitations}\n"
                    )

                await redis_manager.publish_message(
                    self.task_id,
                    SystemMessage(content=f"论文手开始写{key}部分"),
                )

                ## TODO: 图片引用错误
                writer_response = await writer_agent.run(
                    writer_prompt,
                    available_images=coder_response.created_images,
                    sub_title=key,
                )
                if writer_response.status != "success":
                    phase_status = "degraded"

                await validate_contract_boundary(
                    task_id=self.task_id,
                    work_dir=self.work_dir,
                    contract=contract,
                    phase=key,
                    boundary="after",
                    tracer=stage_tracer,
                )
                await redis_manager.publish_message(
                    self.task_id,
                    SystemMessage(content=f"论文手完成{key}部分"),
                )

                user_output.set_res(key, writer_response)
            except asyncio.CancelledError:
                phase_status = "failed"
                failure_kind = "cancelled"
                raise
            except DataContractIntegrityError:
                phase_status = "failed"
                failure_kind = "integrity"
                raise
            except Exception:
                phase_status = "failed"
                failure_kind = "provider"
                raise
            finally:
                await trace_recorder.emit(
                    self.task_id,
                    "phase.end",
                    phase=key,
                    success=phase_status == "success",
                    status=phase_status,
                    duration_ms=int((time.monotonic() - phase_started) * 1000),
                    failure_kind=failure_kind,
                )
                set_trace_phase(None)

        # 关闭沙盒

        await code_interpreter.cleanup()
        logger.info(user_output.get_res())

        ################################################ write steps

        write_flows = flows.get_write_flows(
            user_output, config_template, problem.ques_all
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

            await redis_manager.publish_message(
                self.task_id,
                SystemMessage(content=f"论文手开始写{key}部分"),
            )

            phase_status = "success"
            failure_kind = None
            try:
                await validate_contract_boundary(
                    task_id=self.task_id,
                    work_dir=self.work_dir,
                    contract=contract,
                    phase=key,
                    boundary="before",
                    tracer=stage_tracer,
                )
                writer_response = await writer_agent.run(prompt=value, sub_title=key)
                user_output.set_res(key, writer_response)
                phase_status = writer_response.status
                await validate_contract_boundary(
                    task_id=self.task_id,
                    work_dir=self.work_dir,
                    contract=contract,
                    phase=key,
                    boundary="after",
                    tracer=stage_tracer,
                )
            except asyncio.CancelledError:
                phase_status = "failed"
                failure_kind = "cancelled"
                raise
            except DataContractIntegrityError:
                phase_status = "failed"
                failure_kind = "integrity"
                raise
            except Exception:
                phase_status = "failed"
                failure_kind = "provider"
                raise
            finally:
                await trace_recorder.emit(
                    self.task_id,
                    "phase.end",
                    phase=key,
                    stage="write",
                    success=phase_status == "success",
                    status=phase_status,
                    duration_ms=int((time.monotonic() - phase_started) * 1000),
                    failure_kind=failure_kind,
                )
                set_trace_phase(None)

        logger.info(user_output.get_res())

        await validate_contract_boundary(
            task_id=self.task_id,
            work_dir=self.work_dir,
            contract=contract,
            phase="publish",
            boundary="before",
            tracer=stage_tracer,
        )
        user_output.save_result()
