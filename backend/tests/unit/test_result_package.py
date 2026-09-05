"""M2 ResultPackage 的持久化、partial 语义和 Writer 材料测试。"""

from __future__ import annotations

from pathlib import Path

from app.results import (
    load_result_package,
    persist_result_package,
    render_result_package_for_writer,
)


def test_persist_success_package_with_fingerprinted_artifacts(tmp_path: Path):
    """成功 package 必须保存代码、stdout、图表及其指纹。"""
    image = tmp_path / "figure.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\ncontent")
    table = tmp_path / "result.csv"
    table.write_text("value\n1\n", encoding="utf-8")

    package = persist_result_package(
        tmp_path,
        task_id="task",
        phase="ques1",
        status="success",
        executed_code=("print('objective=10')",),
        stdout="objective=10\n",
        artifact_paths=("figure.png", "result.csv"),
        metric_statements=("objective=10",),
        limitations=(),
        summary="已完成。",
    )

    loaded = load_result_package(tmp_path, package.package_path)

    assert loaded == package
    assert package.code.path == "result_packages/ques1.py"
    assert package.stdout.path == "result_packages/ques1.stdout.txt"
    assert [artifact.path for artifact in package.artifacts] == [
        "figure.png",
        "result.csv",
    ]
    assert [figure.path for figure in package.figures] == ["figure.png"]
    assert package.metrics[0].source_path == package.stdout.path
    assert package.metrics[0].source_line == 1
    assert (tmp_path / package.package_path).is_file()
    assert (tmp_path / package.code.path).read_text(encoding="utf-8").startswith(
        "# execute_code #1"
    )


def test_partial_package_keeps_evidence_and_writer_continues(tmp_path: Path):
    """Repair 耗尽后的 partial package 仍保留产物并可供 Writer 使用。"""
    table = tmp_path / "partial.csv"
    table.write_text("value\n2\n", encoding="utf-8")

    package = persist_result_package(
        tmp_path,
        task_id="task",
        phase="ques2",
        status="partial",
        executed_code=("print('profit=88.2')", "raise KeyError('x')"),
        stdout="profit=88.2\n",
        artifact_paths=("partial.csv",),
        metric_statements=("profit=88.2",),
        limitations=("retry_exhausted: 2",),
        summary="仅保留已验证结果。",
        failure_kind="retry_exhausted",
        failure_message="KeyError: x",
    )

    material = render_result_package_for_writer(package)

    assert package.status == "partial"
    assert package.failure is not None
    assert package.failure.kind == "retry_exhausted"
    assert [artifact.path for artifact in package.artifacts] == ["partial.csv"]
    assert "状态：partial" in material
    assert "profit=88.2" in material
    assert "partial 原因：retry_exhausted" in material
    assert "不得将本阶段描述为完全收敛" in material
