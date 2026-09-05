"""工作流程定义模块，管理建模任务的求解和写作流程。"""

from app.domain.m15 import DataContract
from app.domain.result_package import ResultPackage
from app.models.user_output import UserOutput
from app.orchestration.task_outline import TaskSchedule
from app.results import render_result_package_for_writer
from app.schemas.A2A import ModelerToCoder


class Flows:
    """管理数学建模任务的求解流程和写作流程。"""

    def __init__(
        self,
        questions: dict[str, str | int],
        schedule: TaskSchedule | None = None,
    ):
        self.flows: dict[str, dict] = {}
        self.questions: dict[str, str | int] = questions
        self.schedule = schedule
        if schedule is not None:
            missing = [
                question_id
                for question_id in schedule.execution_order
                if question_id not in questions
            ]
            if missing:
                raise ValueError(f"TaskSchedule 包含缺失问题: {missing}")

    def set_flows(self, ques_count: int):
        """根据问题数量设置流程节点。

        Args:
            ques_count: 问题数量。
        """
        ques_str = self._question_order(ques_count)
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

    def get_solution_flows(
        self,
        questions: dict[str, str | int],
        modeler_response: ModelerToCoder,
        data_contract: DataContract | None = None,
    ):
        """生成求解阶段的流程配置。

        Args:
            questions: 包含各问题描述的字典。
            modeler_response: 建模手的响应，包含各问题的解决方案。

        Returns:
            求解流程配置字典，键为任务名，值包含 coder_prompt 等信息。
        """
        question_order = self._question_order()
        solutions = modeler_response.questions_solution
        ques_flow = {
            key: {
                "coder_prompt": f"""
                        参考建模手给出的解决方案{solutions.get(key, "")}
                        完成如下问题{questions[key]}
                    """,
            }
            for key in question_order
        }
        flows = {
            "eda": {
                "coder_prompt": f"""
                        数据契约：{_render_eda_contract(data_contract)}
                        只读分析冻结契约声明的 cleaned 数据，完成数据分布、质量诊断和必要图表。
                        严禁修改原始附件或 cleaned 文件，严禁承担数据清洗、填充、去重或类型转换。
                        分析和图表输出可写入当前工作目录，**不需要复杂的模型**。
                    """,
            },
            **ques_flow,
            "sensitivity_analysis": {
                "coder_prompt": f"""
                        参考建模手给出的解决方案{solutions.get("sensitivity_analysis", "对模型进行灵敏度分析")}
                        完成敏感性分析
                    """,
            },
        }
        return flows

    def get_write_flows(
        self, user_output: UserOutput, config_template: dict, bg_ques_all: str
    ):
        """生成写作阶段的流程配置。

        Args:
            user_output: 用户输出对象，包含已求解的结果。
            config_template: 论文模板配置。
            bg_ques_all: 问题背景和题目信息。

        Returns:
            写作流程配置字典，键为章节名，值为写作提示。
        """
        model_build_solve = user_output.get_model_build_solve()
        flows = {
            "firstPage": f"""问题背景{bg_ques_all},不需要编写代码,根据模型的求解的信息{model_build_solve}，按照如下模板撰写：{config_template["firstPage"]}，撰写标题，摘要，关键词""",
            "RepeatQues": f"""问题背景{bg_ques_all},不需要编写代码,根据模型的求解的信息{model_build_solve}，按照如下模板撰写：{config_template["RepeatQues"]}，撰写问题重述""",
            "analysisQues": f"""问题背景{bg_ques_all},不需要编写代码,根据模型的求解的信息{model_build_solve}，按照如下模板撰写：{config_template["analysisQues"]}，撰写问题分析""",
            "modelAssumption": f"""问题背景{bg_ques_all},不需要编写代码,根据模型的求解的信息{model_build_solve}，按照如下模板撰写：{config_template["modelAssumption"]}，撰写模型假设""",
            "symbol": f"""不需要编写代码,根据模型的求解的信息{model_build_solve}，按照如下模板撰写：{config_template["symbol"]}，撰写符号说明部分""",
            "judge": f"""不需要编写代码,根据模型的求解的信息{model_build_solve}，按照如下模板撰写：{config_template["judge"]}，撰写模型的评价部分""",
        }
        return flows

    def get_writer_prompt(
        self,
        key: str,
        result_package: ResultPackage | None,
        config_template: dict,
        fallback_summary: str = "",
    ) -> str:
        """根据不同的key生成对应的writer_prompt

        Args:
            key: 任务类型
            result_package: 当前 phase 的终态结果包。
            config_template: 论文模板。
            fallback_summary: package 落盘失败时保留的受限摘要。

        Returns:
            str: 生成的writer_prompt
        """
        questions_quesx_keys = self.get_questions_quesx_keys()
        bgc = self.questions["background"]
        package_material = (
            render_result_package_for_writer(result_package)
            if result_package is not None
            else (
                "ResultPackage 落盘失败。不得编造精确数字、图表或结论。"
                f"仅可谨慎使用以下兼容摘要：{fallback_summary}"
            )
        )
        quesx_writer_prompt = {
            key: f"""
                    问题背景{bgc},不需要编写代码。
                    以下 ResultPackage 是阶段结果的唯一来源：
                    {package_material}
                    按照如下模板撰写：{config_template[key]}
                """
            for key in questions_quesx_keys
        }

        writer_prompt = {
            "eda": f"""
                    问题背景{bgc},不需要编写代码。
                    以下 ResultPackage 是阶段结果的唯一来源：
                    {package_material}
                    按照如下模板撰写：{config_template["eda"]}
                """,
            **quesx_writer_prompt,
            "sensitivity_analysis": f"""
                    问题背景{bgc},不需要编写代码。
                    以下 ResultPackage 是阶段结果的唯一来源：
                    {package_material}
                    按照如下模板撰写：{config_template["sensitivity_analysis"]}
                """,
        }

        if key in writer_prompt:
            return writer_prompt[key]
        else:
            raise ValueError(f"未知的任务类型: {key}")

    def get_questions_quesx_keys(self) -> list[str]:
        """获取问题1,2...的键"""
        return list(self.get_questions_quesx().keys())

    def get_questions_quesx(self) -> dict[str, str | int]:
        """获取问题1,2,3...的键值对"""
        return {
            question_id: self.questions[question_id]
            for question_id in self._question_order()
        }

    def get_seq(self, ques_count: int) -> dict[str, str]:
        """获取论文章节顺序。

        Args:
            ques_count: 问题数量。

        Returns:
            以章节名为键的有序字典。
        """
        ques_str = self._question_order(ques_count)
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

    def _question_order(self, ques_count: int | None = None) -> list[str]:
        """优先使用依赖调度顺序，否则保留旧的连续编号顺序。"""
        if self.schedule is not None:
            return list(self.schedule.execution_order)
        if ques_count is None:
            raw_count = self.questions.get("ques_count")
            if not isinstance(raw_count, int):
                raise ValueError("缺少可用的 ques_count")
            ques_count = raw_count
        return [f"ques{i}" for i in range(1, ques_count + 1)]


def _render_eda_contract(data_contract: DataContract | None) -> str:
    """仅向 EDA 暴露冻结 cleaned 路径和已验证字段。"""
    if data_contract is None:
        return "调用方未提供冻结契约；不得执行数据清洗或修改数据文件。"
    if data_contract.status == "no_data":
        return (
            f"artifact_id={data_contract.artifact_id}, status=no_data，"
            "本任务没有可分析的数据表。"
        )
    tables = "；".join(
        (
            f"{table.table_id}: cleaned_path={table.cleaned_path!r}, "
            "verified_columns="
            + ", ".join(
                f"{column.name}:{column.canonical_type}" for column in table.columns
            )
        )
        for table in data_contract.tables
    )
    return (
        f"artifact_id={data_contract.artifact_id}, status={data_contract.status}；"
        f"{tables}"
    )
