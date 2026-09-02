"""现有任务产物路径和 Pandoc 导出的兼容性测试。"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
import pypandoc

from app.routers import files_router
from app.utils import common_utils
from app.utils.common_utils import get_current_files, transform_link
from app.utils.task_manifest import update_task_manifest


_MINIMAL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def test_transform_link_keeps_root_image_as_static_task_url():
    """根目录图片链接转换后仍指向既有 static 任务路径。"""
    content = "图表：![结果图](ques1_result.png)"

    transformed = transform_link("task-20", content)

    assert (
        "![结果图](http://localhost:8000/static/task-20/ques1_result.png)"
        in transformed
    )


@pytest.mark.asyncio
async def test_file_listing_and_manifest_keep_relative_artifact_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """任务文件列表和增量 manifest 不把现有路径改成绝对路径。"""
    root_image = tmp_path / "ques1_result.png"
    root_image.write_bytes(_MINIMAL_PNG)

    monkeypatch.setattr(files_router, "get_work_dir", lambda task_id: str(tmp_path))
    listed = await files_router.get_files("task-20")
    listed_names = {item["filename"] for item in listed}

    assert listed_names == {"ques1_result.png"}
    assert get_current_files(str(tmp_path), "image") == ["ques1_result.png"]
    assert all(not Path(name).is_absolute() for name in listed_names)

    first_manifest = update_task_manifest(
        tmp_path,
        task_id="task-20",
        status="running",
        current_phase="ques1",
        generated_at="2026-08-31T00:00:00+00:00",
    )

    cleaned_csv = tmp_path / "cleaned" / "附件1__Sheet1.csv"
    cleaned_csv.parent.mkdir()
    cleaned_csv.write_text("value\n1\n", encoding="utf-8")
    assert get_current_files(str(cleaned_csv.parent), "data") == [cleaned_csv.name]

    second_manifest = update_task_manifest(
        tmp_path,
        task_id="task-20",
        status="completed",
        current_phase=None,
        completed_phases=["eda", "ques1"],
        generated_at="2026-08-31T00:01:00+00:00",
    )

    first_paths = {item["path"] for item in first_manifest["artifacts"]}
    second_by_path = {item["path"]: item for item in second_manifest["artifacts"]}
    assert "ques1_result.png" in first_paths
    assert second_by_path["ques1_result.png"]["exists"] is True
    assert second_by_path["cleaned/附件1__Sheet1.csv"]["kind"] == "cleaned_csv"
    assert second_by_path["cleaned/附件1__Sheet1.csv"]["phase"] == "eda"
    assert all(not Path(path).is_absolute() for path in second_by_path)
    assert root_image.exists()
    assert cleaned_csv.exists()


def test_md_2_docx_exports_root_image_when_pandoc_is_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Pandoc 可用时，res.md 的根目录 PNG 能进入 DOCX 导出流程。"""
    try:
        pandoc_version = pypandoc.get_pandoc_version()
    except OSError as exc:
        pytest.skip(f"Pandoc 不可用，跳过 DOCX 导出验收: {exc}")

    work_dir = tmp_path / "work_dir"
    work_dir.mkdir()
    (work_dir / "ques1_result.png").write_bytes(_MINIMAL_PNG)
    (work_dir / "res.md").write_text(
        "# 最小论文\n\n![结果图](ques1_result.png)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(common_utils, "get_work_dir", lambda task_id: str(work_dir))

    common_utils.md_2_docx("task-20")

    docx_path = work_dir / "res.docx"
    assert pandoc_version is not None
    assert docx_path.is_file()
    assert docx_path.stat().st_size > 0
