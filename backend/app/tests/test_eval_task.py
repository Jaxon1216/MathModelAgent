"""eval_task.py 单元测试。"""

from __future__ import annotations

from pathlib import Path

from scripts.eval_task import (
    compare_scorecards,
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
    result = compute_paper_structure(tmp_path / "res.json", md_path, tmp_path / "res.docx")
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
