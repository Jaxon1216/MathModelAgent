"""新 ModelPlan 到基线 Coder Flows 的桥接契约测试。"""

import json

from app.core.flows import Flows
from app.domain.model_plan import MAX_CODER_HANDOFF_CHARS, PlanValidation
from app.domain.problem import DataCatalog, DataColumn, DataTable, Problem, QuestionSet
from app.schemas.A2A import ModelerToCoder


def test_model_plan_handoff_feeds_legacy_solution_flows():
    """新计划必须完整、有限地进入旧 Coder 的每个求解节点。"""
    questions = {
        "background": "种植策略",
        "ques_count": 2,
        "ques1": "制定稳定情形方案",
        "ques2": "制定不确定情形方案",
    }
    catalog = DataCatalog(
        input_tables=(
            DataTable(
                table_id="附件.xlsx::统计",
                file="附件.xlsx",
                sheet="统计",
                columns=(
                    DataColumn(name="作物", source_name="作物"),
                    DataColumn(name="产量", source_name="产量"),
                ),
            ),
        ),
        output_templates=("result.xlsx",),
    )
    problem = Problem(
        task_id="bridge",
        question_set=QuestionSet.from_coordinator(questions, 2),
        data_catalog=catalog,
    )
    section = {
        "data": [{"table_id": "附件.xlsx::统计", "columns": ["作物", "产量"]}],
        "objective": "优化种植收益",
        "model": "线性规划",
        "method": "建立决策变量并求解",
        "constraints": ["面积守恒"],
        "validation": ["检查约束残差"],
        "figures": ["方案对比图"],
        "fallback": "使用保守可行解",
    }
    plan = PlanValidation.for_problem(problem).parse_json(
        json.dumps(
            {
                "version": "m1",
                "question_plans": {
                    "ques1": section,
                    "ques2": {**section, "objective": "优化风险调整收益"},
                },
                "sensitivity_analysis": {
                    **section,
                    "objective": "评估参数扰动",
                    "figures": ["敏感性曲线"],
                },
            },
            ensure_ascii=False,
        )
    )

    handoff = plan.to_coder_handoff(catalog)
    flows = Flows(questions).get_solution_flows(
        questions,
        ModelerToCoder(questions_solution=handoff),
    )

    assert set(handoff) == {"ques1", "ques2", "sensitivity_analysis"}
    assert set(flows) == {"eda", "ques1", "ques2", "sensitivity_analysis"}
    assert "优化种植收益" in flows["ques1"]["coder_prompt"]
    assert "优化风险调整收益" in flows["ques2"]["coder_prompt"]
    assert "评估参数扰动" in flows["sensitivity_analysis"]["coder_prompt"]
    assert all(len(text) <= MAX_CODER_HANDOFF_CHARS for text in handoff.values())
