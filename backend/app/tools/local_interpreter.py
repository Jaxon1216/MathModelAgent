"""本地代码解释器模块，通过本地 Jupyter 内核执行 Python 代码。"""

import os
import time

import jupyter_client

from app.schemas.response import (
    OutputItem,
    ResultModel,
    StdErrModel,
    SystemMessage,
)
from app.services.redis_manager import redis_manager
from app.services.trace_recorder import trace_recorder
from app.tools.base_interpreter import BaseCodeInterpreter
from app.tools.matplotlib_setup import build_matplotlib_init_code
from app.tools.notebook_serializer import NotebookSerializer
from app.utils.log_util import logger


class LocalCodeInterpreter(BaseCodeInterpreter):
    """基于本地 Jupyter 内核的代码解释器。"""

    def __init__(
        self,
        task_id: str,
        work_dir: str,
        notebook_serializer: NotebookSerializer,
    ):
        super().__init__(task_id, work_dir, notebook_serializer)
        self.km, self.kc = None, None
        self.interrupt_signal = False

    async def initialize(self):
        # 本地内核一般不需异步上传文件，直接切换目录即可
        # 初始化 Jupyter 内核管理器和客户端
        logger.info("初始化本地内核")
        # 设置 UTF-8 编码环境，避免 Windows 中文环境下 GBK 编码导致的乱码问题
        kernel_env = os.environ.copy()
        kernel_env["PYTHONIOENCODING"] = "utf-8"
        kernel_env["PYTHONUTF8"] = "1"
        self.km, self.kc = jupyter_client.manager.start_new_kernel(
            kernel_name="python3", env=kernel_env
        )
        font_msg, font_type = self._pre_execute_code()
        if font_msg:
            await redis_manager.publish_message(
                self.task_id,
                SystemMessage(content=font_msg, type=font_type),
            )

    def _pre_execute_code(self) -> tuple[str | None, str]:
        """执行 matplotlib 初始化，并解析字体加载结果供前端展示。

        Returns:
            (消息文案, SystemMessage.type)；无可用信息时文案为 None。
        """
        init_code = build_matplotlib_init_code(self.work_dir)
        execution = self.execute_code_(init_code)
        stdout = "\n".join(text for mark, text in execution if mark == "stdout")
        for line in stdout.splitlines():
            line = line.strip()
            if "中文字体已加载" in line:
                # 去掉日志前缀，前端只展示关键结论
                content = line.removeprefix("[matplotlib_setup] ").strip()
                return content, "success"
            if "未找到中文字体" in line:
                content = line.removeprefix("[matplotlib_setup] ").strip()
                return content, "warning"
        return None, "info"

    async def execute_code(self, code: str) -> tuple[str, bool, str]:
        logger.info(f"执行代码: {code}")
        before_artifacts = self._snapshot_artifacts()
        code_lines = code.count("\n") + 1 if code else 0
        await trace_recorder.emit(
            self.task_id,
            "execute.start",
            agent=self.__class__.__name__,
            code_lines=code_lines,
        )
        started_at = time.monotonic()

        #  添加代码到notebook
        self.notebook_serializer.add_code_cell_to_notebook(code)

        text_to_gpt: list[str] = []
        content_to_display: list[OutputItem] | None = []
        error_occurred: bool = False
        error_message: str = ""

        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content="开始执行代码"),
        )
        # 执行 Python 代码
        logger.info("开始在本地执行代码...")
        execution = self.execute_code_(code)
        logger.info("代码执行完成，开始处理结果...")

        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content="代码执行完成"),
        )

        for mark, out_str in execution:
            if mark in ("stdout", "execute_result_text", "display_text"):
                text_to_gpt.append(self._truncate_text(f"[{mark}]\n{out_str}"))
                #  添加text到notebook
                content_to_display.append(
                    ResultModel(res_type="result", format="text", msg=out_str)
                )
                self.notebook_serializer.add_code_cell_output_to_notebook(out_str)

            elif mark in (
                "execute_result_png",
                "execute_result_jpeg",
                "display_png",
                "display_jpeg",
            ):
                # TODO: 视觉模型解释图像
                text_to_gpt.append(f"[{mark} 图片已生成，内容为 base64，未展示]")

                #  添加image到notebook
                if "png" in mark:
                    self.notebook_serializer.add_image_to_notebook(out_str, "image/png")
                    content_to_display.append(
                        ResultModel(res_type="result", format="png", msg=out_str)
                    )
                else:
                    self.notebook_serializer.add_image_to_notebook(
                        out_str, "image/jpeg"
                    )
                    content_to_display.append(
                        ResultModel(res_type="result", format="jpeg", msg=out_str)
                    )

            elif mark == "error":
                error_occurred = True
                error_message = self.delete_color_control_char(out_str)
                error_message = self._truncate_text(error_message)
                logger.error(f"执行错误: {error_message}")
                text_to_gpt.append(error_message)
                #  添加error到notebook
                self.notebook_serializer.add_code_cell_error_to_notebook(out_str)
                content_to_display.append(StdErrModel(msg=out_str))

        logger.info(f"text_to_gpt: {text_to_gpt}")
        combined_text = "\n".join(text_to_gpt)

        await self._push_to_websocket(content_to_display)

        duration_ms = int((time.monotonic() - started_at) * 1000)
        after_artifacts = self._snapshot_artifacts()
        new_artifacts = after_artifacts - before_artifacts
        await trace_recorder.emit(
            self.task_id,
            "execute.done",
            agent=self.__class__.__name__,
            code_lines=code_lines,
            duration_ms=duration_ms,
            error=error_message if error_occurred else None,
            stdout_len=len(combined_text),
            new_artifacts=len(new_artifacts),
        )
        for artifact_path in sorted(new_artifacts):
            await trace_recorder.emit(
                self.task_id,
                "artifact.created",
                agent=self.__class__.__name__,
                path=artifact_path,
                kind=self._artifact_kind(artifact_path),
            )

        return (
            combined_text,
            error_occurred,
            error_message,
        )

    @staticmethod
    def _artifact_kind(filename: str) -> str:
        """根据扩展名返回 artifact 类型。"""
        lower = filename.lower()
        if lower.endswith(".png"):
            return "png"
        if lower.endswith(".csv"):
            return "csv"
        if lower.endswith(".npy"):
            return "npy"
        return "other"

    def _snapshot_artifacts(self) -> set[str]:
        """快照 work_dir 下 png/csv/npy 文件集合。"""
        if not os.path.isdir(self.work_dir):
            return set()
        return {
            name
            for name in os.listdir(self.work_dir)
            if name.lower().endswith((".png", ".csv", ".npy"))
        }

    def execute_code_(self, code) -> list[tuple[str, str]]:
        assert self.kc is not None
        assert self.km is not None
        self.kc.execute(code)
        logger.info(f"执行代码: {code}")
        # Get the output of the code
        msg_list = []
        while True:
            try:
                iopub_msg = self.kc.get_iopub_msg(timeout=1)
                msg_list.append(iopub_msg)
                if (
                    iopub_msg["msg_type"] == "status"
                    and iopub_msg["content"].get("execution_state") == "idle"
                ):
                    break
            except Exception:
                if self.interrupt_signal:
                    self.km.interrupt_kernel()
                    self.interrupt_signal = False
                continue

        all_output: list[tuple[str, str]] = []
        for iopub_msg in msg_list:
            if iopub_msg["msg_type"] == "stream":
                if iopub_msg["content"].get("name") == "stdout":
                    output = iopub_msg["content"]["text"]
                    all_output.append(("stdout", output))
            elif iopub_msg["msg_type"] == "execute_result":
                if "data" in iopub_msg["content"]:
                    if "text/plain" in iopub_msg["content"]["data"]:
                        output = iopub_msg["content"]["data"]["text/plain"]
                        all_output.append(("execute_result_text", output))
                    if "text/html" in iopub_msg["content"]["data"]:
                        output = iopub_msg["content"]["data"]["text/html"]
                        all_output.append(("execute_result_html", output))
                    if "image/png" in iopub_msg["content"]["data"]:
                        output = iopub_msg["content"]["data"]["image/png"]
                        all_output.append(("execute_result_png", output))
                    if "image/jpeg" in iopub_msg["content"]["data"]:
                        output = iopub_msg["content"]["data"]["image/jpeg"]
                        all_output.append(("execute_result_jpeg", output))
            elif iopub_msg["msg_type"] == "display_data":
                if "data" in iopub_msg["content"]:
                    if "text/plain" in iopub_msg["content"]["data"]:
                        output = iopub_msg["content"]["data"]["text/plain"]
                        all_output.append(("display_text", output))
                    if "text/html" in iopub_msg["content"]["data"]:
                        output = iopub_msg["content"]["data"]["text/html"]
                        all_output.append(("display_html", output))
                    if "image/png" in iopub_msg["content"]["data"]:
                        output = iopub_msg["content"]["data"]["image/png"]
                        all_output.append(("display_png", output))
                    if "image/jpeg" in iopub_msg["content"]["data"]:
                        output = iopub_msg["content"]["data"]["image/jpeg"]
                        all_output.append(("display_jpeg", output))
            elif iopub_msg["msg_type"] == "error":
                # TODO: 正确返回格式
                if "traceback" in iopub_msg["content"]:
                    output = "\n".join(iopub_msg["content"]["traceback"])
                    cleaned_output = self.delete_color_control_char(output)
                    all_output.append(("error", cleaned_output))
        return all_output

    def _snapshot_image_names(self) -> set[str]:
        """返回 work_dir 下当前的图片文件名集合，用作 section 基线。"""
        if not os.path.isdir(self.work_dir):
            return set()
        return {
            f
            for f in os.listdir(self.work_dir)
            if f.lower().endswith((".png", ".jpg", ".jpeg"))
        }

    async def get_created_images(self, section: str) -> list[str]:
        """获取本 section 期间新增的图片列表（非消费/幂等）。

        以 section 起始基线（add_section 时快照）为参照，返回 current - baseline。
        completion_check 与最终返回可多次调用且结果一致，不会互相"吃掉"对方的 diff，
        因此 writer 能正确拿到每问题图片，subtask.summary 的 png_count 也真实。
        """
        current_images = self._snapshot_image_names()
        baseline = self.section_baseline.get(section, self.last_created_images)
        new_images = current_images - baseline
        # 仅作为未登记 section 的回退基线，不影响已登记 section 的计算
        self.last_created_images = current_images
        logger.info(f"{section} 本阶段新创建的图片列表: {sorted(new_images)}")
        return sorted(new_images)

    async def cleanup(self):
        # 关闭内核
        assert self.kc is not None
        assert self.km is not None
        self.kc.shutdown()
        logger.info("关闭内核")
        self.km.shutdown_kernel()

    def send_interrupt_signal(self):
        self.interrupt_signal = True

    def restart_jupyter_kernel(self):
        """Restart the Jupyter kernel and recreate the work directory."""
        assert self.kc is not None
        self.kc.shutdown()
        # 设置 UTF-8 编码环境，避免 Windows 中文环境下 GBK 编码导致的乱码问题
        kernel_env = os.environ.copy()
        kernel_env["PYTHONIOENCODING"] = "utf-8"
        kernel_env["PYTHONUTF8"] = "1"
        self.km, self.kc = jupyter_client.manager.start_new_kernel(
            kernel_name="python3", env=kernel_env
        )
        self.interrupt_signal = False
        self._create_work_dir()
        self._pre_execute_code()

    def _create_work_dir(self):
        """Ensure the working directory exists after a restart."""
        os.makedirs(self.work_dir, exist_ok=True)
