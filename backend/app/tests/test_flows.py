"""Flows：数据准备与求解阶段拆分。"""

from app.core.flows import Flows
from app.schemas.A2A import ModelerToCoder
from app.core.agents.coder_agent import CoderAgent


def test_get_solution_flows_omits_eda():
    questions = {
        "background": "背景",
        "ques1": "问题1",
        "ques2": "问题2",
        "ques_count": 2,
    }
    modeler = ModelerToCoder(
        questions_solution={
            "ques1": "线性规划",
            "ques2": "鲁棒优化",
            "sensitivity_analysis": "±20%",
        }
    )
    flows = Flows(questions)
    result = flows.get_solution_flows(questions, modeler)
    assert "eda" not in result
    assert "ques1" in result
    assert "ques2" in result
    assert "sensitivity_analysis" in result
    assert "cleaned/" in result["ques1"]["coder_prompt"]


def test_get_data_prep_prompt_encodes_naming():
    flows = Flows({"background": "种植策略", "ques1": "q1"})
    prompt = flows.get_data_prep_prompt()
    assert "cleaned/" in prompt
    assert "一 sheet 一文件" in prompt
    assert "result*" in prompt
    assert "不要画论文图" in prompt


def test_eda_phase_does_not_require_figures():
    assert CoderAgent._min_figures_for_phase("eda") == 0
    assert CoderAgent._min_figures_for_phase("ques1") == 2
    assert CoderAgent._min_figures_for_phase("sensitivity_analysis") == 1
