"""ModelPlan 领域契约测试。"""

import json

import pytest

from app.domain.model_plan import ModelPlanValidationError, PlanValidation
from app.domain.problem import DataCatalog, Problem, QuestionSet


def _problem() -> Problem:
    """构造带受控数据目录的领域输入。"""
    return Problem(
        task_id="modeler-unit",
        question_set=QuestionSet(
            question_count=2,
            questions={
                "ques1": "预测总产量。",
                "ques2": "制定种植策略。",
            },
            background="农作物种植策略",
        ),
        data_catalog=DataCatalog(
            files=("crop.csv", "land.csv"),
            columns_by_file={
                "crop.csv": ("crop", "yield"),
                "land.csv": ("land_type", "area"),
            },
        ),
    )


def _section(*, data: list[dict] | None = None) -> dict:
    """构造满足 M1 契约的最小计划段。"""
    return {
        "data": data or [],
        "objective": "确定可验证的种植决策目标",
        "model": "以线性规划构建决策模型",
        "method": "整理输入、求解并复核约束",
        "constraints": ["面积非负"],
        "validation": ["检查约束残差"],
        "figures": ["方案对比图"],
        "fallback": "使用保守基线方案",
    }


def _payload() -> dict:
    """构造覆盖全部问题的合法计划。"""
    return {
        "version": "m1",
        "question_plans": {
            "ques1": _section(
                data=[{"file": "crop.csv", "columns": ["crop", "yield"]}]
            ),
            "ques2": _section(
                data=[{"file": "land.csv", "columns": ["land_type", "area"]}]
            ),
        },
        "sensitivity_analysis": _section(),
    }


def test_plan_validation_requires_exact_questions_and_verified_references():
    """合法计划必须覆盖全部 quesN，且仅能引用数据目录中的事实。"""
    plan = PlanValidation.for_problem(_problem()).parse_json(
        json.dumps(_payload(), ensure_ascii=False)
    )

    assert set(plan.question_plans) == {"ques1", "ques2"}
    assert plan.question_plans["ques1"].data[0].file == "crop.csv"
    assert "ques2" in plan.to_coder_handoff()
    assert "sensitivity_analysis" in plan.to_coder_handoff()


def test_plan_validation_rejects_missing_question_plan():
    """缺失任一 Coordinator 声明的问题必须失败。"""
    payload = _payload()
    payload["question_plans"].pop("ques2")

    with pytest.raises(ModelPlanValidationError, match="question_keys_mismatch"):
        PlanValidation.for_problem(_problem()).parse_json(
            json.dumps(payload, ensure_ascii=False)
        )


def test_plan_validation_rejects_data_references_without_a_catalog():
    """未提供验证数据目录时，计划不能猜测文件或列。"""
    problem = _problem().model_copy(update={"data_catalog": DataCatalog()})

    with pytest.raises(ModelPlanValidationError, match="unknown_data_file"):
        PlanValidation.for_problem(problem).parse_json(
            json.dumps(_payload(), ensure_ascii=False)
        )


@pytest.mark.parametrize(
    ("reference", "reason"),
    [
        ({"file": "invented.csv", "columns": ["yield"]}, "unknown_data_file"),
        ({"file": "crop.csv", "columns": ["invented_column"]}, "unknown_data_columns"),
    ],
)
def test_plan_validation_rejects_invented_data_facts(
    reference: dict[str, object], reason: str
):
    """计划不得编造文件或列名。"""
    payload = _payload()
    payload["question_plans"]["ques1"]["data"] = [reference]

    with pytest.raises(ModelPlanValidationError, match=reason):
        PlanValidation.for_problem(_problem()).parse_json(
            json.dumps(payload, ensure_ascii=False)
        )
