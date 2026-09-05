"""eval_task.py 单元测试。"""

from __future__ import annotations

from pathlib import Path

from scripts.eval_task import (
    check_regression,
    compare_scorecards,
    compute_agent_quality,
    compute_figure_quality,
    compute_llm_metrics,
    compute_paper_structure,
)


def test_compute_llm_metrics_aggregates_tokens():
    events = [
        {
            "event": "llm.response",
            "agent": "CoderAgent",
            "payload": {
                "model": "gpt-4",
                "latency_ms": 100,
                "prompt_tokens": 50,
                "completion_tokens": 30,
                "total_tokens": 80,
                "cache_read_tokens": 10,
                "reasoning_tokens": 5,
            },
        },
        {
            "event": "llm.response",
            "agent": "WriterAgent",
            "payload": {
                "model": "gpt-4",
                "latency_ms": 200,
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
            },
        },
    ]
    result = compute_llm_metrics(events)
    assert result["llm_call_count"] == 2
    assert result["total_tokens"] == 230
    assert result["total_latency_ms"] == 300
    assert result["by_agent"]["CoderAgent"]["calls"] == 1
    assert result["by_agent"]["WriterAgent"]["tokens"] == 150


def test_in_text_cite_count_excludes_footnote_definitions(tmp_path: Path):
    md_path = tmp_path / "res.md"
    md_path.write_text(
        "正文引用[^1]和[^2]\n\n[^1]: Author A\n[^2]: Author B\n",
        encoding="utf-8",
    )
    result = compute_paper_structure(
        tmp_path / "res.json", md_path, tmp_path / "res.docx"
    )
    assert result["ref_count"] == 2
    assert result["in_text_cite_count"] == 2


def test_compare_scorecards_reports_deltas():
    old = {
        "task_id": "old-task",
        "agent_quality": {"execute_error_rate": 0.1},
        "llm_metrics": {"total_tokens": 1000},
    }
    new = {
        "task_id": "new-task",
        "agent_quality": {"execute_error_rate": 0.05},
        "llm_metrics": {"total_tokens": 1200},
    }
    diff = compare_scorecards(new, old)
    assert diff["old_task_id"] == "old-task"
    assert diff["new_task_id"] == "new-task"
    metrics = {d["metric"]: d for d in diff["diffs"]}
    assert "agent_quality.execute_error_rate" in metrics
    assert metrics["agent_quality.execute_error_rate"]["delta"] == -0.05


def test_m1_tool_gate_requires_execute_code_but_not_load_skill():
    """M1 预注入技能正文时，只把真实代码执行作为工具硬门禁。"""
    baseline = {"must_call_tools": ["execute_code"]}

    passing = check_regression(
        {"agent_quality": {"tool_calls_by_name": {"execute_code": 2}}},
        baseline,
    )
    failing = check_regression(
        {"agent_quality": {"tool_calls_by_name": {"load_skill": 2}}},
        baseline,
    )

    assert passing["all_pass"] is True
    assert failing["all_pass"] is False
    assert failing["checks"] == [
        {
            "metric": "must_call_tools.execute_code",
            "value": "missing",
            "threshold": "called",
            "pass": False,
        }
    ]


def test_missing_expected_summary_is_reported_and_counts_as_zero_images(
    tmp_path: Path,
):
    """预期问题缺 summary 时不能从图片门禁中消失。"""
    events = [
        {"event": "phase.start", "phase": "ques1", "payload": {}},
        {
            "event": "phase.end",
            "phase": "ques1",
            "payload": {"success": True},
        },
        {
            "event": "subtask.summary",
            "phase": "ques2",
            "payload": {"turns": 2, "png_count": 2},
        },
    ]
    required = ["ques1", "ques2"]

    agent = compute_agent_quality(events, required_phases=required)
    figures = compute_figure_quality(
        events,
        tmp_path,
        tmp_path / "res.md",
        expected_question_phases=required,
    )

    assert agent["missing_subtask_summaries"] == ["ques1"]
    assert agent["missing_subtask_summary_count"] == 1
    assert figures["png_per_phase"] == {"ques1": 0, "ques2": 2}


def test_missing_phase_end_and_degraded_status_are_distinct():
    """缺结束事件与显式 degraded 必须分别计数。"""
    events = [
        {"event": "phase.start", "phase": "ques1", "payload": {}},
        {"event": "phase.start", "phase": "ques2", "payload": {}},
        {
            "event": "phase.end",
            "phase": "ques2",
            "payload": {"status": "degraded", "success": False},
        },
    ]

    result = compute_agent_quality(events, required_phases=["ques1", "ques2"])

    assert result["missing_phase_ends"] == ["ques1"]
    assert result["missing_phase_end_count"] == 1
    assert result["degraded_phases"] == ["ques2"]
    assert result["degraded_phase_count"] == 1
    assert result["phase_fail"] == 0


def test_required_paper_sections_detect_missing_and_failure_placeholder(
    tmp_path: Path,
):
    """缺失 key 和失败占位都不能算作完整章节。"""
    res_json = tmp_path / "res.json"
    res_json.write_text(
        '{"ques1":{"response_content":"本阶段未完成：timeout"}}',
        encoding="utf-8",
    )

    result = compute_paper_structure(
        res_json,
        tmp_path / "res.md",
        tmp_path / "res.docx",
        required_sections=["ques1", "ques2"],
    )

    assert result["missing_sections"] == ["ques2"]
    assert result["invalid_sections"] == ["ques1"]
    assert result["empty_section_count"] == 2


def test_phase_completeness_and_degradation_remain_diagnostics_only():
    """新增状态指标用于定位，不自动成为阻塞门禁。"""
    baseline = {
        "max_phase_fail": 0,
        "max_missing_phase_end": 0,
        "max_missing_subtask_summary": 0,
        "max_degraded_phases": 0,
    }
    scorecard = {
        "agent_quality": {
            "phase_fail": 0,
            "missing_phase_end_count": 1,
            "missing_subtask_summary_count": 1,
            "degraded_phase_count": 1,
        }
    }

    result = check_regression(scorecard, baseline)

    assert result["all_pass"] is None
    assert result["checks"] == []
