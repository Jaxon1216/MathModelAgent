"""代码手 Agent 模块，负责生成和执行 Python 代码完成建模任务。"""

import asyncio
import json
import os

from app.core.agents.agent import Agent
from app.config.setting import settings, ApiType
from app.utils.log_util import logger
from app.services.redis_manager import redis_manager
from app.services.trace_recorder import trace_recorder
from app.schemas.response import SystemMessage, InterpreterMessage
from app.tools.base_interpreter import BaseCodeInterpreter
from app.core.llm.llm import LLM
from app.schemas.A2A import CoderToWriter
from app.core.prompts import CODER_PROMPT
from app.core.prompts import (
    get_reflection_prompt,
    get_completion_check_prompt,
    get_figure_missing_prompt,
)
from app.core.skills.loader import SkillLoader
from app.core.functions import get_coder_tools, get_coder_tools_anthropic
from app.utils.common_utils import get_current_files

# TODO: 时间等待过久，stop 进程
# TODO: 支持 cuda


class CoderAgent(Agent):
    """代码手 Agent，通过 LLM 生成代码并在解释器中执行，支持错误反思和重试。

    ReAct 循环：
    - execute_code  → 执行代码，成功继续，失败 reflection+retry
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
        self.is_first_run = True
        self.system_prompt = CODER_PROMPT
        self.code_interpreter = code_interpreter

        # Skills Loader：启动时扫描 catalog/ 元数据
        self._skill_loader = SkillLoader()
        api_type = self.model.api_type
        if api_type == ApiType.ANTHROPIC:
            self._tools = get_coder_tools_anthropic(self._skill_loader)
        else:
            self._tools = get_coder_tools(self._skill_loader)

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

        # 如果是第一次运行，添加系统提示和数据集文件信息
        if self.is_first_run:
            logger.info("首次运行，添加系统提示和数据集文件信息")
            self.is_first_run = False
            await self.append_chat_history(
                {"role": "system", "content": self.system_prompt}
            )
            await self.append_chat_history(
                {
                    "role": "user",
                    "content": f"当前文件夹下的数据集文件{get_current_files(self.work_dir, 'data')}",
                }
            )

        logger.info(f"添加子任务提示: {prompt}")
        await self.append_chat_history({"role": "user", "content": prompt})

        await self._ensure_phase_skills(subtask_title)

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
                return CoderToWriter(
                    code_response=(
                        f"任务失败，超过最大尝试次数 {self.max_retries}，"
                        f"最后错误信息: {last_error_message}"
                    ),
                    created_images=[],
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
                raise Exception(
                    f"Reached maximum number of chat turns ({self.max_chat_turns}). Task incomplete."
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
                    # 重置 completion_checked：有新的工具调用，说明还在工作
                    completion_checked = False
                    tool_call = response.tool_calls[0]
                    tool_id = tool_call.id

                    # ---- 构建 assistant 消息（含 tool_calls） ----
                    assistant_msg: dict = {
                        "role": "assistant",
                        "content": response.content,
                    }
                    if response.reasoning_content:
                        assistant_msg["reasoning_content"] = response.reasoning_content
                    assistant_msg["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": tc.arguments},
                        }
                        for tc in response.tool_calls
                    ]
                    await self.append_chat_history(assistant_msg)

                    await trace_recorder.emit(
                        self.task_id,
                        "tool.call",
                        agent=self.__class__.__name__,
                        phase=subtask_title,
                        tool_name=tool_call.name,
                        arg_summary=self._summarize_tool_args(
                            tool_call.name, tool_call.arguments
                        ),
                    )

                    # ---- 分支：load_skill ----
                    if tool_call.name == "load_skill":
                        skill_name = json.loads(tool_call.arguments).get(
                            "skill_name", ""
                        )
                        logger.info(f"加载技能: {skill_name}")
                        await redis_manager.publish_message(
                            self.task_id,
                            SystemMessage(content=f"代码手加载技能: {skill_name}"),
                        )
                        skill = self._skill_loader.get_skill(skill_name)
                        if skill:
                            skill_content = (
                                f'<skill-loaded name="{skill_name}">\n'
                                f"{skill.body}\n"
                                f"</skill-loaded>\n\n"
                                f"技能已加载：{skill.name}。请严格遵循上述技能说明完成任务。"
                            )
                        else:
                            available = ", ".join(self._skill_loader.list_skills())
                            skill_content = (
                                f"技能 '{skill_name}' 不存在。可用技能：{available}"
                            )
                        await trace_recorder.emit(
                            self.task_id,
                            "skill.load",
                            agent=self.__class__.__name__,
                            phase=subtask_title,
                            skill_name=skill_name,
                            found=skill is not None,
                            body_chars=len(skill.body) if skill else 0,
                        )
                        await self.append_chat_history(
                            {
                                "role": "tool",
                                "tool_call_id": tool_id,
                                "name": "load_skill",
                                "content": skill_content,
                            }
                        )
                        # load_skill 不计入 retry_count，直接继续循环
                        continue

                    # ---- 分支：execute_code ----
                    if tool_call.name == "execute_code":
                        logger.info("调用工具: execute_code")
                        await redis_manager.publish_message(
                            self.task_id,
                            SystemMessage(content="代码手调用 execute_code 工具"),
                        )

                        code = json.loads(tool_call.arguments)["code"]
                        await redis_manager.publish_message(
                            self.task_id,
                            InterpreterMessage(input={"code": code}),
                        )

                        (
                            text_to_gpt,
                            error_occurred,
                            error_message,
                        ) = await self.code_interpreter.execute_code(code)

                        if error_occurred:
                            await self.append_chat_history(
                                {
                                    "role": "tool",
                                    "tool_call_id": tool_id,
                                    "name": "execute_code",
                                    "content": error_message,
                                }
                            )
                            logger.warning(f"代码执行错误: {error_message}")
                            retry_count += 1
                            last_error_message = error_message
                            logger.info(
                                f"当前尝试次: {retry_count} / {self.max_retries}"
                            )
                            await trace_recorder.emit(
                                self.task_id,
                                "react.reflect",
                                agent=self.__class__.__name__,
                                phase=subtask_title,
                                retry_count=retry_count,
                                error_preview=error_message[:200],
                            )
                            await redis_manager.publish_message(
                                self.task_id,
                                SystemMessage(
                                    content="代码手反思纠正错误", type="error"
                                ),
                            )
                            await self.append_chat_history(
                                {
                                    "role": "user",
                                    "content": get_reflection_prompt(
                                        error_message, code
                                    ),
                                }
                            )
                        else:
                            last_tool_output = text_to_gpt
                            await self.append_chat_history(
                                {
                                    "role": "tool",
                                    "tool_call_id": tool_id,
                                    "name": "execute_code",
                                    "content": text_to_gpt,
                                }
                            )
                        continue

                else:
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
                        await self.append_chat_history(
                            {"role": "assistant", "content": response.content or ""}
                        )
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
                        await self.append_chat_history(
                            {"role": "assistant", "content": response.content or ""}
                        )
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
                    await self._emit_subtask_summary(
                        subtask_title, subtask_turns, retry_count, created_images
                    )
                    return CoderToWriter(
                        code_response=response.content,
                        created_images=created_images,
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

    async def _ensure_phase_skills(self, subtask_title: str) -> None:
        """子任务开始时预注入必备技能，避免 ques 阶段跳过 visualization 导致零产出。"""
        if subtask_title.startswith("ques"):
            skill_names = ["mathematical-modeling", "visualization", "figure-reporting"]
        elif subtask_title == "sensitivity_analysis":
            skill_names = ["sensitivity-analysis", "visualization", "figure-reporting"]
        elif subtask_title == "eda":
            skill_names = ["eda", "visualization", "figure-reporting"]
        else:
            return

        for skill_name in skill_names:
            await self._inject_skill_content(skill_name, subtask_title)

    async def _inject_skill_content(self, skill_name: str, subtask_title: str) -> None:
        """将技能 body 注入对话历史（等效 load_skill，不消耗 tool 轮次）。"""
        skill = self._skill_loader.get_skill(skill_name)
        if skill:
            skill_content = (
                f'<skill-preloaded name="{skill_name}">\n'
                f"{skill.body}\n"
                f"</skill-preloaded>\n\n"
                f"技能已预加载：{skill.name}。请严格遵循上述技能说明完成任务。"
            )
        else:
            available = ", ".join(self._skill_loader.list_skills())
            skill_content = f"技能 '{skill_name}' 不存在。可用技能：{available}"
        logger.info(f"预加载技能: {skill_name} ({subtask_title})")
        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content=f"代码手预加载技能: {skill_name}"),
        )
        await trace_recorder.emit(
            self.task_id,
            "skill.load",
            agent=self.__class__.__name__,
            phase=subtask_title,
            skill_name=skill_name,
            found=skill is not None,
            body_chars=len(skill.body) if skill else 0,
            preloaded=True,
        )
        await self.append_chat_history({"role": "user", "content": skill_content})

    @staticmethod
    def _summarize_tool_args(tool_name: str, arguments: str) -> str:
        """生成工具参数摘要，避免 Trace 中写入完整代码。"""
        try:
            args = json.loads(arguments)
        except json.JSONDecodeError:
            return "invalid json"

        if tool_name == "execute_code":
            code = args.get("code", "")
            lines = code.count("\n") + 1 if code else 0
            return f"{lines} lines"
        if tool_name == "load_skill":
            return str(args.get("skill_name", ""))
        return ", ".join(f"{k}={v}" for k, v in list(args.items())[:3])

    async def _emit_subtask_summary(
        self,
        subtask_title: str,
        turns: int,
        retries: int,
        created_images: list[str],
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
            png_count=png_count,
            csv_count=csv_count,
        )
