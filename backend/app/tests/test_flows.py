"""Flows：数据准备与求解阶段拆分。"""

import inspect
from collections.abc import Callable
from typing import cast

import pytest

from app.core.flows import Flows
from app.schemas.A2A import ModelerToCoder
from app.core.agents.coder_agent import CoderAgent
from app.models.user_output import UserOutput
from app.tools.base_interpreter import BaseCodeInterpreter
from app.utils.data_contract import format_data_prep_brief


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
    prompt = flows.get_data_prep_prompt("文件 附件1.csv\nsheet Sheet1 rows=10")
    assert "cleaned/" in prompt
    assert "一 sheet 一文件" in prompt
    assert "result*" in prompt
    assert "不要画论文图" in prompt
    assert "source_inspection.json" in prompt
    assert "sheet Sheet1 rows=10" in prompt
    assert "清洗动作" in prompt


def test_eda_inspection_is_injected_once_and_within_handoff_budgets():
    inspection = (
        "inspection-entry: source.csv / Sheet1 rows=10 columns=2 "
        "warning=hidden details"
    )
    flows = Flows({"background": "种植策略", "ques1": "q1"})

    brief = format_data_prep_brief(["source.csv"])
    prompt = flows.get_data_prep_prompt(inspection)

    assert inspection not in brief
    assert "源数据探查报告已落盘" in brief
    assert "source_inspection.json" not in brief
    assert "warning=hidden details" not in brief
    assert prompt.count(inspection) == 1
    assert len(prompt) <= 12000
    assert len(brief) <= 12000


def test_eda_phase_does_not_require_figures():
    assert CoderAgent._min_figures_for_phase("eda") == 0
    assert CoderAgent._min_figures_for_phase("ques1") == 2
    assert CoderAgent._min_figures_for_phase("sensitivity_analysis") == 1


def test_writer_prefers_phase_packet_over_legacy_coder_summary():
    flows = Flows(
        {
            "title": "题目标题",
            "background": "公开背景",
            "ques1": "问题1",
            "ques_count": 1,
        }
    )
    packet = {
        "phase": "ques1",
        "status": "success",
        "evidence_status": "available",
        "model_reference": "modeler_solution:ques1",
        "artifacts": [{"path": "ques1_plot.png", "kind": "image"}],
        "metrics": ["accuracy=0.92"],
        "limitations": [],
        "stdout": "accuracy=0.92",
    }
    config = {
        key: "模板"
        for key in (
            "firstPage",
            "RepeatQues",
            "analysisQues",
            "modelAssumption",
            "symbol",
            "judge",
            "eda",
            "sensitivity_analysis",
        )
    }
    config["ques1"] = "模板"

    result = flows.get_write_flows(
        UserOutput("/tmp", 1),
        config,
        phase_results={"ques1": packet},
    )

    assert "公开背景" in result["firstPage"]
    assert "问题1" in result["firstPage"]
    assert "ques1_plot.png" in result["firstPage"]
    assert "阶段结果包：unavailable" not in result["firstPage"]


def test_get_write_flows_api_only_accepts_structured_context():
    parameters = inspect.signature(Flows.get_write_flows).parameters

    assert "bg_ques_all" not in parameters
    assert "ques_all" not in parameters
    assert "problem" not in parameters


def test_get_write_flows_rejects_raw_problem_argument():
    flows = Flows(
        {
            "background": "公开背景",
            "ques_count": 1,
            "ques1": "求解目标",
        }
    )
    config = {
        key: "模板"
        for key in (
            "firstPage",
            "RepeatQues",
            "analysisQues",
            "modelAssumption",
            "symbol",
            "judge",
        )
    }

    with pytest.raises(TypeError):
        legacy_call = cast(Callable[..., object], flows.get_write_flows)
        legacy_call(
            UserOutput("/tmp", 1),
            config,
            bg_ques_all="原始题面",
        )


def test_flows_uses_raw_problem_for_comparison_only():
    raw_problem = (
        "研究三种作物的种植安排并最小化总成本与资源浪费同时满足土地和水资源约束"
    )
    flows = Flows(
        {
            "background": "公开背景",
            "ques_count": 1,
            "ques1": "求解最优方案",
        },
        raw_problem=raw_problem,
    )
    templates = {
        "ques1": "模板",
        "eda": "模板",
        "sensitivity_analysis": "模板",
    }

    prompt = flows.get_writer_prompt(
        "ques1",
        f"{raw_problem}。accuracy=0.91。",
        cast(BaseCodeInterpreter, type("Interpreter", (), {"section_output": {}})()),
        templates,
    )

    assert raw_problem not in prompt
    assert "accuracy=0.91" in prompt
