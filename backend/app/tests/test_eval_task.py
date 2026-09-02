"""eval_task.py 单元测试。"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from scripts.eval_task import (
    check_regression,
    compare_scorecards,
    compute_handoff_quality,
    compute_llm_metrics,
    compute_manifest_quality,
    compute_paper_structure,
    compute_source_inspection_quality,
    compute_writer_leakage,
)

EXPECTED_BASELINE_PATH = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "baseline"
    / "2024高教杯C题"
    / "expected.json"
)
NEW_REGRESSION_KEYS = {
    "min_source_inspection_coverage",
    "max_writer_raw_ques_all_leaks",
    "max_writer_constraint_leaks",
    "max_manifest_missing_references",
    "max_handoff_budget_violations",
}


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


def test_quality_metrics_cover_inspection_handoffs_writer_and_manifest(
    tmp_path: Path,
):
    (tmp_path / "附件1.csv").write_text("x\n1\n", encoding="utf-8")
    (tmp_path / "source_inspection.json").write_text(
        json.dumps(
            {
                "status": "ready",
                "data_status": "source_data",
                "source_files": ["附件1.csv"],
                "files": [
                    {
                        "path": "附件1.csv",
                        "role": "source_data",
                        "warnings": ["Sheet1: duplicate_rows: 1"],
                        "sheets": [
                            {
                                "name": "Sheet1",
                                "readable": True,
                                "warnings": ["duplicate_rows: 1"],
                                "scan": {"complete": True},
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (tmp_path / "phase_results").mkdir()
    packet_path = tmp_path / "phase_results" / "ques1.json"
    packet_path.write_text(
        json.dumps(
            {
                "phase": "ques1",
                "status": "success",
                "artifacts": [],
            }
        ),
        encoding="utf-8",
    )
    events = [
        {
            "event": "source.inspection",
            "payload": {
                "rendered_chars": 120,
                "render_budget": 12000,
            },
        },
        {
            "event": "handoff.budget",
            "phase": "ques1",
            "payload": {
                "kind": "model_solution",
                "chars": 100,
                "budget": 6000,
                "truncated": False,
            },
        },
        {
            "event": "phase.result",
            "phase": "ques1",
            "payload": {
                "path": "phase_results/ques1.json",
                "packet_chars": 100,
                "packet_budget": 6000,
            },
        },
        {
            "event": "history.update",
            "phase": "ques1",
            "payload": {
                "history_chars": 500,
                "history_messages": 4,
                "history_budget_chars": 1000,
            },
        },
        {
            "event": "writer.handoff",
            "phase": "ques1",
            "payload": {
                "prompt_chars": 800,
                "prompt": "公开题面和结果材料",
                "public_context": "公开题面",
                "constraint_leakage": False,
                "constraint_echo_count": 0,
            },
        },
    ]

    inspection = compute_source_inspection_quality(events, tmp_path)
    handoff = compute_handoff_quality(events, tmp_path)
    writer = compute_writer_leakage(events)

    assert inspection["file_coverage"] == 1.0
    assert inspection["sheet_coverage"] == 1.0
    assert inspection["warning_count"] == 1
    assert handoff["budgets"]["model_solution"]["max_chars"] == 100
    assert handoff["budgets"]["phase_packet"]["max_chars"] == len(
        packet_path.read_text(encoding="utf-8")
    )
    assert handoff["history"]["max_chars"] == 500
    assert writer["status"] == "unavailable"
    assert writer["raw_ques_all_leaks"] is None
    assert writer["constraint_leaks"] == 0


def test_writer_leakage_detects_real_no_leakage():
    raw_problem = "原始题面：研究三种作物的种植安排并最小化成本。"
    events = [
        {
            "event": "writer.handoff",
            "payload": {
                "raw_ques_all": raw_problem,
                "prompt": "问题背景：公开背景。\n结果材料：成本为 12。",
                "material": "结果材料：成本为 12。",
                "public_context": "问题背景：公开背景。",
                "prompt_chars": 30,
                "constraint_leakage": False,
                "constraint_echo_count": 0,
            },
        }
    ]

    result = compute_writer_leakage(events)

    assert result["raw_ques_all_leaks"] == 0
    assert result["status"] == "available"


def test_writer_leakage_detects_real_leakage_in_prompt():
    raw_problem = "原始题面：研究三种作物的种植安排并最小化成本。"
    events = [
        {
            "event": "writer.handoff",
            "payload": {
                "raw_ques_all": raw_problem,
                "prompt": f"请撰写论文。\n{raw_problem}\n结果材料：成本为 12。",
                "public_context": "公开背景。",
                "prompt_chars": 60,
                "constraint_leakage": False,
                "constraint_echo_count": 0,
            },
        }
    ]

    result = compute_writer_leakage(events)

    assert result["raw_ques_all_leaks"] == 1
    assert result["status"] == "available"


def test_writer_leakage_is_unavailable_without_raw_reference_or_text():
    events = [
        {
            "event": "writer.handoff",
            "payload": {
                "prompt_chars": 20,
                "constraint_leakage": False,
                "constraint_echo_count": 0,
            },
        }
    ]

    result = compute_writer_leakage(events)

    assert result["raw_ques_all_leaks"] is None
    assert result["status"] == "unavailable"


def test_writer_leakage_reports_missing_raw_reference_with_actual_handoff():
    events = [
        {
            "event": "writer.handoff",
            "payload": {
                "prompt": "实际 Writer 交接材料：accuracy=0.91。",
                "public_context": "公开题面",
                "prompt_chars": 30,
                "constraint_leakage": False,
                "constraint_echo_count": 0,
            },
        }
    ]

    result = compute_writer_leakage(events)

    assert result["status"] == "unavailable"
    assert result["raw_ques_all_status"] == "unavailable"
    assert result["raw_ques_all_leaks"] is None
    assert result["raw_reference_count"] == 0
    assert result["raw_reference_missing_count"] == 1
    assert result["handoff_text_count"] == 1
    assert result["handoff_text_missing_count"] == 0
    assert "raw_problem_reference_unavailable" in result["evidence_gaps"]


def test_quality_metrics_do_not_invent_evidence(tmp_path: Path):
    inspection = compute_source_inspection_quality([], tmp_path)
    handoff = compute_handoff_quality([], tmp_path)
    writer = compute_writer_leakage([])
    manifest = compute_manifest_quality([], tmp_path)

    assert inspection["status"] == "not_applicable"
    assert inspection["file_coverage"] is None
    assert inspection["sheet_coverage"] is None
    assert handoff["status"] == "unavailable"
    assert handoff["budget_violation_count"] is None
    assert handoff["history"]["max_chars"] is None
    assert writer["status"] == "unavailable"
    assert writer["constraint_leaks"] is None
    assert manifest["status"] == "unavailable"
    assert manifest["completeness_pass"] is None


def test_regression_checks_missing_new_metric_evidence_as_not_pass():
    result = check_regression(
        {
            "source_inspection_quality": {
                "file_coverage": None,
                "sheet_coverage": None,
            },
            "writer_leakage": {
                "raw_ques_all_leaks": None,
                "constraint_leaks": None,
            },
            "manifest_quality": {"missing_references": None},
            "handoff_quality": {"budget_violation_count": None},
        },
        {
            "min_source_inspection_coverage": 1.0,
            "max_writer_raw_ques_all_leaks": 0,
            "max_writer_constraint_leaks": 0,
            "max_manifest_missing_references": 0,
            "max_handoff_budget_violations": 0,
        },
    )

    assert result["all_pass"] is False
    assert all(check["value"] is None for check in result["checks"])
    assert all(check["pass"] is False for check in result["checks"])


def test_expected_baseline_thresholds_are_executed():
    baseline = json.loads(EXPECTED_BASELINE_PATH.read_text(encoding="utf-8"))
    scorecard = {
        "source_inspection_quality": {
            "file_coverage": 1.0,
            "sheet_coverage": 1.0,
        },
        "writer_leakage": {
            "raw_ques_all_leaks": 0,
            "constraint_leaks": 0,
        },
        "manifest_quality": {"missing_references": []},
        "handoff_quality": {"budget_violation_count": 0},
    }

    result = check_regression(scorecard, baseline)
    checks = {check["metric"]: check for check in result["checks"]}

    assert NEW_REGRESSION_KEYS <= baseline.keys()
    assert NEW_REGRESSION_KEYS <= checks.keys()
    assert all(checks[key]["pass"] for key in NEW_REGRESSION_KEYS)


def test_expected_baseline_thresholds_fail_without_evidence():
    baseline = json.loads(EXPECTED_BASELINE_PATH.read_text(encoding="utf-8"))
    result = check_regression(
        {
            "source_inspection_quality": {
                "file_coverage": None,
                "sheet_coverage": None,
            },
            "writer_leakage": {
                "raw_ques_all_leaks": None,
                "constraint_leaks": None,
            },
            "manifest_quality": {"missing_references": None},
            "handoff_quality": {"budget_violation_count": None},
        },
        baseline,
    )
    checks = {
        check["metric"]: check
        for check in result["checks"]
        if check["metric"] in NEW_REGRESSION_KEYS
    }

    assert set(checks) == NEW_REGRESSION_KEYS
    assert all(check["value"] is None for check in checks.values())
    assert all(check["pass"] is False for check in checks.values())


def test_manifest_quality_detects_missing_packet_reference(tmp_path: Path):
    (tmp_path / "phase_results").mkdir()
    (tmp_path / "phase_results" / "ques1.json").write_text(
        json.dumps(
            {
                "phase": "ques1",
                "status": "success",
                "artifacts": [{"path": "missing.png", "kind": "image"}],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "task_manifest.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "artifacts": [
                    {
                        "path": "phase_results/ques1.json",
                        "kind": "phase_result",
                        "phase": "ques1",
                        "exists": True,
                    },
                    *[
                        {
                            "path": name,
                            "kind": "paper",
                            "phase": "writer",
                            "exists": False,
                        }
                        for name in ("res.json", "res.md", "res.docx")
                    ],
                ],
            }
        ),
        encoding="utf-8",
    )

    result = compute_manifest_quality([], tmp_path)

    assert result["exists"] is True
    assert "missing.png" in result["missing_references"]
    assert result["completeness_pass"] is False


def test_source_inspection_quality_marks_no_table_task_not_applicable(
    tmp_path: Path,
):
    (tmp_path / "result1.xlsx").write_bytes(b"template")

    result = compute_source_inspection_quality([], tmp_path)

    assert result["status"] == "not_applicable"
    assert result["data_status"] == "no_source_data"
    assert result["file_coverage"] is None


def test_eval_entrypoint_works_without_pythonpath(tmp_path: Path):
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "eval_task.py"
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, str(script_path), "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--task-id" in result.stdout
