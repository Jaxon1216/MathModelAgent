"""ResultPackage 到 Writer prompt 的非阻塞交接测试。"""

from __future__ import annotations

from pathlib import Path

from app.core.flows import Flows
from app.results import persist_result_package


def test_partial_result_package_is_the_writer_source_without_blocking(tmp_path: Path):
    """partial package 进入 Writer prompt，而不是被工作流丢弃。"""
    package = persist_result_package(
        tmp_path,
        task_id="task",
        phase="ques1",
        status="partial",
        executed_code=("print('objective=12.5')",),
        stdout="objective=12.5\n",
        artifact_paths=(),
        metric_statements=("objective=12.5",),
        limitations=("retry_exhausted: 1",),
        summary="已完成部分可验证计算。",
        failure_kind="retry_exhausted",
        failure_message="KeyError: missing",
    )
    flows = Flows(
        {
            "background": "背景",
            "ques_count": 1,
            "ques1": "问题一",
        }
    )

    prompt = flows.get_writer_prompt(
        "ques1",
        package,
        {
            "eda": "EDA 模板",
            "ques1": "结果章节模板",
            "sensitivity_analysis": "敏感性模板",
        },
    )

    assert "以下 ResultPackage 是阶段结果的唯一来源" in prompt
    assert "objective=12.5" in prompt
    assert "partial 原因：retry_exhausted" in prompt
    assert "不得将本阶段描述为完全收敛" in prompt
    assert "结果章节模板" in prompt
