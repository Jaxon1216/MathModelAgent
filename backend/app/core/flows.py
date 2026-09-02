"""工作流程定义模块，管理建模任务的求解和写作流程。"""

from collections.abc import Sequence
from typing import Any

from app.models.user_output import UserOutput
from app.tools.base_interpreter import BaseCodeInterpreter
from app.schemas.A2A import ModelerToCoder
from app.utils.problem_context import (
    normalize_constraints,
    redact_execution_constraints,
    redact_raw_problem_echoes,
    render_execution_constraints,
    render_public_problem_context,
    split_public_questions,
    truncate_handoff_text,
)
from app.utils.phase_results import (
    DATA_INVENTORY_BUDGET,
    WRITER_MATERIAL_BUDGET,
    bound_data_inventory,
    bound_writer_material,
    render_phase_result_for_writer,
)


class Flows:
    """管理数学建模任务的求解流程和写作流程。"""

    def __init__(
        self,
        questions: dict[str, Any],
        constraints: Sequence[Any] | None = None,
        raw_problem: str | None = None,
    ):
        self.flows: dict[str, dict] = {}
        self.handoff_metrics: dict[str, dict[str, int | bool]] = {}
        self._raw_problem = str(raw_problem or "")
        payload = dict(questions)
        if constraints is not None:
            payload["constraints"] = list(constraints)
        self.questions, inferred_constraints = split_public_questions(payload)
        self.constraints = normalize_constraints(inferred_constraints)

    def get_public_problem_context(self) -> str:
        """返回只包含题面事实的公共上下文。"""
        return redact_raw_problem_echoes(
            render_public_problem_context(self.questions),
            self._raw_problem,
        )

    def set_flows(self, ques_count: int):
        """根据问题数量设置流程节点。

        Args:
            ques_count: 问题数量。
        """
        ques_str = [f"ques{i}" for i in range(1, ques_count + 1)]
        seq = [
            "firstPage",
            "RepeatQues",
            "analysisQues",
            "modelAssumption",
            "symbol",
            "eda",
            *ques_str,
            "sensitivity_analysis",
            "judge",
        ]
        self.flows = {key: {} for key in seq}

    def get_data_prep_prompt(self, inspection_text: str = "") -> str:
        """生成建模前数据准备阶段的 Coder 提示和探查交接材料。"""
        background = redact_execution_constraints(
            str(self.questions.get("background", "")), self.constraints
        )
        constraint_prompt = render_execution_constraints(self.constraints)
        inspection_material = (inspection_text or "").strip()
        if len(inspection_material) > 12000:
            inspection_material = inspection_material[:11992] + "\n…（已截断）"
        inspection_section = (
            "以下是后端生成的 bounded source_inspection 材料；"
            "完整报告位于 source_inspection.json：\n"
            f"{inspection_material}"
            if inspection_material
            else "先读取后端生成的 source_inspection.json，再开始处理附件。"
        )
        return f"""
对当前目录下的原始附件做数据清洗并落盘。问题背景：{background}

## 源数据探查
{inspection_section}

必须遵守用户消息里的清洗口径：
- 原始附件只读；忽略 result*.xlsx / result*.csv
- 输出到 cleaned/，UTF-8 csv，一 sheet 一文件
- 文件名 cleaned/{{附件主名}}__{{sheet名}}.csv（原 csv 的 sheet 用 Sheet1）
- 不要画论文图，不要复杂建模
- 逐个源文件和 sheet 明确决定：清洗动作、输出路径，或跳过原因；同时记录警告和限制
- 结束前 print 已写出的 cleaned/ 文件列表、每表行数、列名、清洗动作、警告和限制
- 不可读、空表、重复列名、混合类型或有限扫描的风险不得静默覆盖或忽略
{constraint_prompt}
"""

    def get_solution_flows(
        self, questions: dict[str, Any], modeler_response: ModelerToCoder
    ):
        """生成求解阶段的流程配置（不含数据准备，准备已在建模前完成）。

        Args:
            questions: 包含各问题描述的字典。
            modeler_response: 建模手的响应，包含各问题的解决方案。

        Returns:
            求解流程配置字典，键为任务名，值包含 coder_prompt 等信息。
        """
        public_questions, question_constraints = split_public_questions(questions)
        constraints = normalize_constraints(
            [*self.constraints, *question_constraints, *modeler_response.constraints]
        )
        self.constraints = constraints
        constraint_prompt = render_execution_constraints(constraints)
        questions_quesx = {
            key: value
            for key, value in public_questions.items()
            if key.startswith("ques") and key != "ques_count"
        }
        solutions = {
            key: truncate_handoff_text(str(value), 6000)
            for key, value in modeler_response.questions_solution.items()
        }
        self.handoff_metrics = {
            key: {
                "chars": len(solution),
                "budget": 6000,
                "truncated": solution.endswith("...[内容已截断]"),
            }
            for key, solution in solutions.items()
        }
        ques_flow = {
            key: {
                "coder_prompt": f"""
                        只读取 data_contract 中的 cleaned/ 路径，先校验行数与列名再求解。
                        {constraint_prompt}
                        参考建模手给出的解决方案（最多 6000 字符）：
                        {solutions.get(key, "")}
                        完成如下问题{value}
                    """,
            }
            for key, value in questions_quesx.items()
        }
        flows = {
            **ques_flow,
            "sensitivity_analysis": {
                "coder_prompt": f"""
                        只读取 cleaned/ 下已校验的表。
                        {constraint_prompt}
                        参考建模手给出的解决方案{solutions.get("sensitivity_analysis", "对模型进行灵敏度分析")}
                        完成敏感性分析
                    """,
            },
        }
        return flows

    def get_write_flows(
        self,
        user_output: UserOutput,
        config_template: dict,
        phase_results: dict[str, dict] | None = None,
    ):
        """生成写作阶段的流程配置。

        Args:
            user_output: 用户输出对象，包含已求解的结果。
            config_template: 论文模板配置。
            phase_results: 各阶段的结构化结果包。

        Returns:
            写作流程配置字典，键为章节名，值为写作提示。
        """
        # Writer 只从已分流的结构化公共字段取题面。
        public_context = self.get_public_problem_context()
        packets = phase_results or {}
        if packets:
            model_build_solve = bound_writer_material(
                "\n\n".join(
                    render_phase_result_for_writer(
                        packet,
                        budget=WRITER_MATERIAL_BUDGET,
                        constraints=self.constraints,
                        raw_problem=self._raw_problem,
                    )
                    for packet in packets.values()
                ),
                WRITER_MATERIAL_BUDGET,
            )
        else:
            model_build_solve = (
                "阶段结果包：unavailable。不得依赖前文章节记忆或编造精确结果。"
            )
        flows = {
            "firstPage": f"""问题背景{public_context}。根据模型的求解信息{model_build_solve}，按照如下模板撰写：{config_template["firstPage"]}，撰写标题、摘要、关键词""",
            "RepeatQues": f"""问题背景{public_context}。根据模型的求解信息{model_build_solve}，按照如下模板撰写：{config_template["RepeatQues"]}，撰写问题重述""",
            "analysisQues": f"""问题背景{public_context}。根据模型的求解信息{model_build_solve}，按照如下模板撰写：{config_template["analysisQues"]}，撰写问题分析""",
            "modelAssumption": f"""问题背景{public_context}。根据模型的求解信息{model_build_solve}，按照如下模板撰写：{config_template["modelAssumption"]}，撰写模型假设""",
            "symbol": f"""根据模型的求解信息{model_build_solve}，按照如下模板撰写：{config_template["symbol"]}，撰写符号说明部分""",
            "judge": f"""问题背景{public_context}。根据模型的求解信息{model_build_solve}，按照如下模板撰写：{config_template["judge"]}，撰写模型的评价部分""",
        }
        return flows

    def get_writer_prompt(
        self,
        key: str,
        coder_response: str,
        code_interpreter: BaseCodeInterpreter,
        config_template: dict,
        data_inventory: str = "",
        phase_result: dict[str, Any] | None = None,
    ) -> str:
        """根据不同的key生成对应的writer_prompt

        Args:
            key: 任务类型
            coder_response: 代码执行结果
            code_interpreter: 代码解释器，用于取该段 stdout。
            config_template: 论文章节模板。
            data_inventory: 预处理章用的表清单（不含整份 JSON / sample）。

        Returns:
            str: 生成的writer_prompt
        """
        if key in getattr(code_interpreter, "section_output", {}):
            code_output = code_interpreter.get_code_output(key)
        else:
            code_output = ""

        questions_quesx_keys = self.get_questions_quesx_keys()
        bgc = self.get_public_problem_context()
        safe_coder_response = redact_execution_constraints(
            coder_response, self.constraints
        )
        safe_coder_response = redact_raw_problem_echoes(
            safe_coder_response,
            self._raw_problem,
        )
        safe_code_output = redact_execution_constraints(code_output, self.constraints)
        safe_code_output = redact_raw_problem_echoes(
            safe_code_output,
            self._raw_problem,
        )
        inventory = bound_data_inventory(
            redact_execution_constraints(data_inventory or "", self.constraints),
            DATA_INVENTORY_BUDGET,
        )
        inventory = redact_raw_problem_echoes(inventory, self._raw_problem)
        phase_material = render_phase_result_for_writer(
            phase_result,
            budget=WRITER_MATERIAL_BUDGET,
            constraints=self.constraints,
            raw_problem=self._raw_problem,
        )
        if phase_result is None:
            fallback_material = bound_writer_material(
                f"兼容性收工摘要：{safe_coder_response}\n"
                f"有限解释器输出：{safe_code_output}",
                3000,
            )
            phase_material = f"{phase_material}\n{fallback_material}"
        quesx_writer_prompt = {
            key: f"""
                    问题背景{bgc}。
                    以下 bounded phase result 是结果数字和图文件的首选来源：
                    {phase_material}
                    按照如下模板撰写：{config_template[key]}
                """
            for key in questions_quesx_keys
        }

        writer_prompt = {
            "eda": f"""
                    问题背景{bgc}。
                    数据准备阶段 bounded result：
                    {phase_material}
                    {inventory}
                    不要粘贴整份 JSON 或大段 sample。
                    按照如下模板撰写：{config_template["eda"]}
                """,
            **quesx_writer_prompt,
            "sensitivity_analysis": f"""
                    问题背景{bgc}。
                    以下 bounded phase result 是结果数字和图文件的首选来源：
                    {phase_material}
                    按照如下模板撰写：{config_template["sensitivity_analysis"]}
                """,
        }

        if key in writer_prompt:
            return bound_writer_material(writer_prompt[key], WRITER_MATERIAL_BUDGET)
        else:
            raise ValueError(f"未知的任务类型: {key}")

    def get_questions_quesx_keys(self) -> list[str]:
        """获取问题1,2...的键"""
        return list(self.get_questions_quesx().keys())

    def get_questions_quesx(self) -> dict[str, Any]:
        """获取问题1,2,3...的键值对"""
        # 获取所有以 "ques" 开头的键值对
        questions_quesx = {
            key: value
            for key, value in self.questions.items()
            if key.startswith("ques") and key != "ques_count"
        }
        return questions_quesx

    def get_seq(self, ques_count: int) -> dict[str, str]:
        """获取论文章节顺序。

        Args:
            ques_count: 问题数量。

        Returns:
            以章节名为键的有序字典。
        """
        ques_str = [f"ques{i}" for i in range(1, ques_count + 1)]
        seq = [
            "firstPage",
            "RepeatQues",
            "analysisQues",
            "modelAssumption",
            "symbol",
            "eda",
            *ques_str,
            "sensitivity_analysis",
            "judge",
        ]
        return {key: "" for key in seq}
