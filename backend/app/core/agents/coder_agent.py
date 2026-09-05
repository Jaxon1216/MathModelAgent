"""代码手 Agent 模块，负责生成和执行 Python 代码完成建模任务。"""

import asyncio
import json
import os
import re
import zipfile
from pathlib import Path

from app.core.agents.agent import Agent
from app.config.setting import settings, ApiType
from app.utils.log_util import logger
from app.services.redis_manager import redis_manager
from app.services.trace_recorder import trace_recorder
from app.schemas.response import SystemMessage, InterpreterMessage
from app.tools.base_interpreter import BaseCodeInterpreter
from app.core.llm.llm import LLM
from app.core.llm.types import ToolCall
from app.domain.result_package import FailureKind, ResultPackage
from app.results import persist_result_package
from app.schemas.A2A import CoderToWriter
from app.core.prompts import CODER_PROMPT
from app.core.prompts import (
    get_reflection_prompt,
    get_completion_check_prompt,
    get_figure_missing_prompt,
)
from app.core.skills.registry import SkillRegistry
from app.core.functions import get_coder_tools, get_coder_tools_anthropic
from app.utils.common_utils import get_current_files

# TODO: 时间等待过久，stop 进程
# TODO: 支持 cuda


class CoderAgent(Agent):
    """代码手 Agent，通过 LLM 生成代码并在解释器中执行，支持错误反思和重试。

    ReAct 循环：
    - execute_code  → 按响应顺序执行并逐一回填，失败后进入有界 Repair
    - load_skill    → 按需注入领域知识 body，不计入 retry_count
    - no tool call  → 触发 completion check；二次无工具调用才真正退出
    """

    def __init__(
        self,
        task_id: str,
        model: LLM,
        work_dir: str,
        max_chat_turns: int | None = settings.MAX_CHAT_TURNS,
        max_retries: int | None = settings.MAX_RETRIES,
        code_interpreter: BaseCodeInterpreter | None = None,
        context_window: int = 128000,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        super().__init__(task_id, model, context_window, cancel_event=cancel_event)
        self.work_dir = work_dir
        self.max_chat_turns = max_chat_turns
        self.current_chat_turns = 0
        self.max_retries = max_retries
        self.system_prompt = CODER_PROMPT
        self.code_interpreter = code_interpreter

        # SkillRegistry 只扫描 L1 描述符，正文必须经显式 load_skill 获取。
        self._skill_registry = SkillRegistry()
        api_type = self.model.api_type
        if api_type == ApiType.ANTHROPIC:
            self._tools = get_coder_tools_anthropic(self._skill_registry)
        else:
            self._tools = get_coder_tools(self._skill_registry)
        self._loaded_skill_versions: dict[str, str] = {}
        self._phase_prompt = ""
        self._phase_data_context = ""
        self._phase_executed_code: list[str] = []

    async def run(self, prompt: str, subtask_title: str) -> CoderToWriter:  # type: ignore[reportIncompatibleMethodOverride]
        """执行代码手子任务，生成并运行代码。

        Args:
            prompt: 子任务描述。
            subtask_title: 子任务标题，用于分段输出。

        Returns:
            CoderToWriter 对象，包含代码执行结果和生成的图片列表。
        """
        logger.info(f"{self.__class__.__name__}:开始:执行子任务: {subtask_title}")
        assert self.code_interpreter is not None, "code_interpreter 未初始化"
        self.code_interpreter.add_section(subtask_title)
        self.reset_history(f"phase:{subtask_title}")
        self.current_chat_turns = 0
        self._loaded_skill_versions = {}
        self._phase_executed_code = []
        self._phase_prompt = prompt
        self._phase_data_context = (
            f"当前文件夹下的数据集文件{get_current_files(self.work_dir, 'data')}"
        )
        await self.append_chat_history({"role": "system", "content": self.system_prompt})
        await self.append_chat_history(
            {
                "role": "user",
                "content": self._phase_data_context,
            }
        )
        logger.info(f"添加子任务提示: {prompt}")
        await self.append_chat_history({"role": "user", "content": prompt})

        retry_count = 0
        last_error_message = ""
        last_tool_output = ""  # 记录最近一次成功的 tool 输出，供 completion check 使用
        completion_checked = False  # 是否已经做过一次 completion check
        subtask_turns = 0

        while True:
            # ---- 退出条件检查 ----
            if self.max_retries is not None and retry_count >= self.max_retries:
                logger.error(f"超过最大尝试次数: {self.max_retries}")
                await redis_manager.publish_message(
                    self.task_id,
                    SystemMessage(content="超过最大尝试次数", type="error"),
                )
                logger.warning(
                    f"任务失败，超过最大尝试次数 {self.max_retries}，"
                    f"最后错误信息: {last_error_message}"
                )
                return await self._build_coder_response(
                    subtask_title=subtask_title,
                    status="partial",
                    turns=subtask_turns,
                    retries=retry_count,
                    code_response="本阶段未完全收敛，仅可使用已验证的执行输出和产物。",
                    limitations=[f"retry_exhausted: {self.max_retries}"],
                    last_error=last_error_message,
                    failure_kind="retry_exhausted",
                )

            if (
                self.max_chat_turns is not None
                and self.current_chat_turns >= self.max_chat_turns
            ):
                logger.error(f"超过最大聊天次数: {self.max_chat_turns}")
                await redis_manager.publish_message(
                    self.task_id,
                    SystemMessage(content="超过最大聊天次数", type="error"),
                )
                return await self._build_coder_response(
                    subtask_title=subtask_title,
                    status="partial",
                    turns=subtask_turns,
                    retries=retry_count,
                    code_response="本阶段达到对话轮数上限，仅可使用已验证的执行输出和产物。",
                    limitations=[f"turn_limit: {self.max_chat_turns}"],
                    last_error=last_error_message or None,
                    failure_kind="turn_limit",
                )

            self.current_chat_turns += 1
            subtask_turns += 1
            logger.info(f"当前对话轮次: {self.current_chat_turns}")

            await trace_recorder.emit(
                self.task_id,
                "react.turn",
                agent=self.__class__.__name__,
                phase=subtask_title,
                turn=subtask_turns,
                retry_count=retry_count,
                completion_checked=completion_checked,
            )

            try:
                response = await self._chat(
                    history=self.chat_history,
                    tools=self._tools,
                    tool_choice="auto",
                    agent_name=self.__class__.__name__,
                )

                if response.tool_calls:
                    completion_checked = False
                    # 所有 tool result 回填前禁止压缩或发起下一次模型请求。
                    await self.record_response(response, allow_compress=False)

                    for response_tool_call in response.tool_calls:
                        await trace_recorder.emit(
                            self.task_id,
                            "tool.call",
                            agent=self.__class__.__name__,
                            phase=subtask_title,
                            tool_name=response_tool_call.name,
                            tool_call_id=response_tool_call.id,
                            arg_summary=self._summarize_tool_args(
                                response_tool_call.name,
                                response_tool_call.arguments,
                            ),
                        )

                    execution_errors: list[tuple[str, str]] = []
                    successful_outputs: list[str] = []
                    for tool_call in response.tool_calls:
                        if tool_call.name == "load_skill":
                            skill_content = await self._load_skill_content(
                                tool_call.id,
                                tool_call.arguments,
                                subtask_title,
                            )
                            await self.append_chat_history(
                                {
                                    "role": "tool",
                                    "tool_call_id": tool_call.id,
                                    "name": "load_skill",
                                    "content": skill_content,
                                }
                            )
                            continue

                        if tool_call.name == "execute_code":
                            tool_content, code, error_message = (
                                await self._execute_code_call(tool_call)
                            )
                            await self.append_chat_history(
                                {
                                    "role": "tool",
                                    "tool_call_id": tool_call.id,
                                    "name": "execute_code",
                                    "content": tool_content,
                                }
                            )
                            if error_message is not None:
                                execution_errors.append((code, error_message))
                            else:
                                successful_outputs.append(tool_content)
                            continue

                        await self.append_chat_history(
                            {
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "name": tool_call.name,
                                "content": (
                                    f"未知工具 '{tool_call.name}'，未执行。"
                                ),
                            }
                        )

                    if successful_outputs:
                        last_tool_output = "\n".join(successful_outputs)
                    if execution_errors:
                        retry_count += len(execution_errors)
                        repair_code, last_error_message = execution_errors[-1]
                        logger.warning(f"代码执行错误: {last_error_message}")
                        logger.info(
                            f"当前尝试次: {retry_count} / {self.max_retries}"
                        )
                        await trace_recorder.emit(
                            self.task_id,
                            "react.reflect",
                            agent=self.__class__.__name__,
                            phase=subtask_title,
                            retry_count=retry_count,
                            error_preview=last_error_message[:200],
                            repair_context="short",
                            retained_skill_versions=dict(self._loaded_skill_versions),
                        )
                        await redis_manager.publish_message(
                            self.task_id,
                            SystemMessage(content="代码手反思纠正错误", type="error"),
                        )
                        await self._reset_for_repair(
                            subtask_title,
                            last_error_message,
                            repair_code,
                        )
                    else:
                        await self.compress_if_needed()
                    continue

                # ---- 无工具调用分支 ----
                if not completion_checked:
                    # 第一次无工具调用：注入 completion check，再给 LLM 一次机会
                    logger.info("无工具调用，注入 completion check")
                    completion_checked = True
                    section_images = await self.code_interpreter.get_created_images(
                        subtask_title
                    )
                    min_figs = self._min_figures_for_phase(subtask_title)
                    images_ok = len(section_images) >= min_figs
                    await trace_recorder.emit(
                        self.task_id,
                        "react.completion_check",
                        agent=self.__class__.__name__,
                        phase=subtask_title,
                        images_in_section=len(section_images),
                        images_required=min_figs,
                        images_ok=images_ok,
                        will_continue=True,
                    )
                    await self.record_response(response)
                    check_prompt = get_completion_check_prompt(
                        prompt, last_tool_output
                    )
                    if not images_ok and min_figs > 0:
                        check_prompt += get_figure_missing_prompt(
                            subtask_title, len(section_images), min_figs
                        )
                    await redis_manager.publish_message(
                        self.task_id,
                        SystemMessage(content="代码手执行完成验证"),
                    )
                    await self.append_chat_history(
                        {"role": "user", "content": check_prompt}
                    )
                    continue

                # 第二次无工具调用：检查图片是否达标后再退出
                created_images = await self.code_interpreter.get_created_images(
                    subtask_title
                )
                min_figs = self._min_figures_for_phase(subtask_title)
                if min_figs > 0 and len(created_images) < min_figs:
                    logger.warning(
                        f"子任务 {subtask_title} 图片不足 "
                        f"{len(created_images)}/{min_figs}，阻断退出并要求补图"
                    )
                    completion_checked = False
                    await trace_recorder.emit(
                        self.task_id,
                        "react.completion_check",
                        agent=self.__class__.__name__,
                        phase=subtask_title,
                        images_in_section=len(created_images),
                        images_required=min_figs,
                        images_ok=False,
                        will_continue=True,
                        blocked_exit=True,
                    )
                    await self.record_response(response)
                    await self.append_chat_history(
                        {
                            "role": "user",
                            "content": get_figure_missing_prompt(
                                subtask_title, len(created_images), min_figs
                            ),
                        }
                    )
                    continue

                logger.info("completion check 通过，子任务完成")
                return await self._build_coder_response(
                    subtask_title=subtask_title,
                    status="success",
                    turns=subtask_turns,
                    retries=retry_count,
                    code_response=response.content,
                    preferred_images=created_images,
                )

            except Exception as e:
                logger.error(f"执行过程中发生异常: {str(e)}")
                retry_count += 1
                last_error_message = str(e)
                continue

        logger.info(f"{self.__class__.__name__}:完成:执行子任务: {subtask_title}")

    @staticmethod
    def _min_figures_for_phase(phase: str) -> int:
        """返回子任务阶段要求的最低 png 数量。"""
        if phase.startswith("ques"):
            return 2
        if phase == "eda":
            return 2
        if phase == "sensitivity_analysis":
            return 1
        return 0

    async def _load_skill_content(
        self,
        tool_call_id: str,
        arguments: str,
        phase: str,
    ) -> str:
        """执行一次 L2 skill 加载并返回与调用一一对应的 tool result。"""
        skill_name = ""
        skill = None
        failure = ""
        try:
            payload = json.loads(arguments)
            if not isinstance(payload, dict):
                raise ValueError("arguments 必须是 JSON 对象。")
            requested_name = payload.get("skill_name")
            if not isinstance(requested_name, str) or not requested_name.strip():
                raise ValueError("skill_name 必须是非空字符串。")
            skill_name = requested_name.strip()
            skill = self._skill_registry.load_skill(skill_name)
            if skill is None:
                failure = (
                    f"技能 '{skill_name}' 不存在或无法加载。可用技能："
                    f"{', '.join(self._skill_registry.list_skills())}"
                )
        except (json.JSONDecodeError, ValueError) as exc:
            failure = f"工具调用参数无效：{exc}"

        if skill is not None:
            self._loaded_skill_versions[skill.name] = skill.version
            logger.info(f"加载技能: {skill.name}@{skill.version}")
            await redis_manager.publish_message(
                self.task_id,
                SystemMessage(content=f"代码手加载技能: {skill.name}"),
            )
            content = (
                f'<skill-loaded name="{skill.name}" version="{skill.version}">\n'
                f"{skill.body}\n"
                f"</skill-loaded>\n\n"
                f"技能已加载：{skill.name}。请严格遵循上述技能说明完成任务。"
            )
        else:
            content = failure or "技能加载失败。"

        await trace_recorder.emit(
            self.task_id,
            "skill.load",
            agent=self.__class__.__name__,
            phase=phase,
            tool_call_id=tool_call_id,
            skill_name=skill_name,
            found=skill is not None,
            version=skill.version if skill else None,
            body_chars=len(skill.body) if skill else 0,
            load_source="tool_call",
            loaded_skill_count=len(self._loaded_skill_versions),
        )
        return content

    async def _execute_code_call(
        self,
        tool_call: ToolCall,
    ) -> tuple[str, str, str | None]:
        """执行一个 ``execute_code`` 调用并返回 tool result、代码和失败信息。"""
        assert self.code_interpreter is not None
        try:
            payload = json.loads(tool_call.arguments)
            if not isinstance(payload, dict):
                raise ValueError("arguments 必须是 JSON 对象。")
            code = payload.get("code")
            if not isinstance(code, str):
                raise ValueError("code 必须是字符串。")
        except (json.JSONDecodeError, ValueError) as exc:
            return f"工具调用参数无效：{exc}", "", None

        self._phase_executed_code.append(code)
        logger.info("调用工具: execute_code")
        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content="代码手调用 execute_code 工具"),
        )
        await redis_manager.publish_message(
            self.task_id,
            InterpreterMessage(input={"code": code}),
        )
        try:
            output, error_occurred, error_message = (
                await self.code_interpreter.execute_code(code)
            )
        except Exception as exc:
            return f"代码执行异常：{exc}", code, str(exc)
        if error_occurred:
            return error_message, code, error_message
        return output, code, None

    async def _reset_for_repair(
        self,
        phase: str,
        error_message: str,
        code: str,
    ) -> None:
        """创建当前 phase 的短 Repair history，不重放任何 skill 正文。"""
        self.reset_history(f"repair:{phase}")
        loaded_skills = (
            "\n".join(
                f"- {name}@{version}"
                for name, version in self._loaded_skill_versions.items()
            )
            or "无"
        )
        for message in (
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": self._phase_data_context},
            {"role": "user", "content": self._phase_prompt},
            {
                "role": "user",
                "content": (
                    "已加载技能（仅名称与版本；正文未保留，如需指南请再次调用 "
                    f"load_skill）：\n{loaded_skills}"
                ),
            },
            {
                "role": "user",
                "content": get_reflection_prompt(error_message, code),
            },
        ):
            await self.append_chat_history(message)

    @staticmethod
    def _summarize_tool_args(tool_name: str, arguments: str) -> str:
        """生成工具参数摘要，避免 Trace 中写入完整代码。"""
        try:
            args = json.loads(arguments)
        except json.JSONDecodeError:
            return "invalid json"
        if not isinstance(args, dict):
            return "invalid json object"

        if tool_name == "execute_code":
            code = args.get("code", "")
            lines = code.count("\n") + 1 if code else 0
            return f"{lines} lines"
        if tool_name == "load_skill":
            return str(args.get("skill_name", ""))
        return ", ".join(f"{k}={v}" for k, v in list(args.items())[:3])

    async def _build_coder_response(
        self,
        *,
        subtask_title: str,
        status: str,
        turns: int,
        retries: int,
        code_response: str | None,
        preferred_images: list[str] | None = None,
        limitations: list[str] | None = None,
        last_error: str | None = None,
        failure_kind: FailureKind | None = None,
    ) -> CoderToWriter:
        """从成功执行输出和可读取文件构造受控交接与 ResultPackage。"""
        assert self.code_interpreter is not None
        code_output = self.code_interpreter.get_code_output(subtask_title)
        candidates = self.code_interpreter.get_section_artifacts(subtask_title)
        verified_artifacts = [
            path for path in candidates if self._artifact_is_readable(path)
        ]
        preferred = set(preferred_images or [])
        verified_images = [
            path
            for path in verified_artifacts
            if Path(path).suffix.lower() in {".png", ".jpg", ".jpeg"}
            and (not preferred or path in preferred or Path(path).name in preferred)
        ]
        if not verified_images:
            verified_images = [
                path
                for path in verified_artifacts
                if Path(path).suffix.lower() in {".png", ".jpg", ".jpeg"}
            ]

        result_limitations = list(limitations or [])
        metrics = self._extract_verified_metrics(code_output)
        if status == "partial" and not (verified_artifacts or metrics):
            result_limitations.append("verified_evidence_missing")
        result_package: ResultPackage | None = None
        try:
            result_package = persist_result_package(
                self.work_dir,
                task_id=self.task_id,
                phase=subtask_title,
                status="partial" if status == "partial" else "success",
                executed_code=self._phase_executed_code,
                stdout=code_output,
                artifact_paths=verified_artifacts,
                metric_statements=metrics,
                limitations=result_limitations,
                summary=code_response,
                failure_kind=failure_kind,
                failure_message=last_error,
            )
            await trace_recorder.emit(
                self.task_id,
                "result_package.persist",
                agent=self.__class__.__name__,
                phase=subtask_title,
                package_path=result_package.package_path,
                status=result_package.status,
                artifact_count=len(result_package.artifacts),
                figure_count=len(result_package.figures),
                success=True,
            )
        except Exception as exc:
            result_limitations.append("result_package_persist_failed")
            await trace_recorder.emit(
                self.task_id,
                "result_package.persist",
                agent=self.__class__.__name__,
                phase=subtask_title,
                success=False,
                error_preview=str(exc)[:500],
            )

        await trace_recorder.emit(
            self.task_id,
            "coder.result",
            agent=self.__class__.__name__,
            phase=subtask_title,
            status=status,
            artifact_count=len(verified_artifacts),
            image_count=len(verified_images),
            metric_count=len(metrics),
            limitations=result_limitations,
            last_error_preview=(last_error or "")[:500],
        )
        await self._emit_subtask_summary(
            subtask_title,
            turns,
            retries,
            verified_images,
            status=status,
            artifact_count=len(verified_artifacts),
        )
        return CoderToWriter(
            status="partial" if status == "partial" else "success",
            code_response=code_response,
            code_output=code_output,
            created_images=verified_images,
            created_artifacts=verified_artifacts,
            verified_metrics=metrics,
            limitations=result_limitations,
            last_error=last_error,
            result_package=result_package,
        )

    def _artifact_is_readable(self, relative_path: str) -> bool:
        """验证产物位于 work_dir 内、非空且格式可读取。"""
        root = Path(self.work_dir).resolve()
        path = (root / relative_path).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            return False
        if not path.is_file() or path.stat().st_size <= 0:
            return False
        suffix = path.suffix.lower()
        try:
            with path.open("rb") as stream:
                header = stream.read(8)
            if suffix == ".png":
                return header == b"\x89PNG\r\n\x1a\n"
            if suffix in {".jpg", ".jpeg"}:
                return header.startswith(b"\xff\xd8")
            if suffix == ".xlsx":
                return zipfile.is_zipfile(path)
            if suffix == ".xls":
                return header.startswith(b"\xd0\xcf\x11\xe0")
            if suffix == ".npy":
                return header.startswith(b"\x93NUMPY")
            if suffix == ".csv":
                with path.open(encoding="utf-8-sig") as stream:
                    return bool(stream.readline().strip())
        except (OSError, UnicodeError, zipfile.BadZipFile):
            return False
        return False

    @staticmethod
    def _extract_verified_metrics(code_output: str) -> list[str]:
        """只从成功执行 stdout 中提取有限数值事实。"""
        metrics: list[str] = []
        for line in code_output.splitlines():
            normalized = " ".join(line.split())
            if not normalized or len(normalized) > 300:
                continue
            if not re.search(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|%", normalized):
                continue
            if normalized not in metrics:
                metrics.append(normalized)
            if len(metrics) >= 20:
                break
        return metrics

    async def _emit_subtask_summary(
        self,
        subtask_title: str,
        turns: int,
        retries: int,
        created_images: list[str],
        *,
        status: str = "success",
        artifact_count: int = 0,
    ) -> None:
        """子任务结束时输出 Trace 汇总。"""
        png_count = len(created_images)
        csv_count = 0
        if os.path.isdir(self.work_dir):
            csv_count = sum(
                1 for name in os.listdir(self.work_dir) if name.lower().endswith(".csv")
            )
        await trace_recorder.emit(
            self.task_id,
            "subtask.summary",
            agent=self.__class__.__name__,
            phase=subtask_title,
            turns=turns,
            retries=retries,
            status=status,
            png_count=png_count,
            csv_count=csv_count,
            artifact_count=artifact_count,
        )
