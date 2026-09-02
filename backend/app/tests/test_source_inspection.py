"""源数据探查契约、风险识别和限长渲染测试。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import openpyxl

import app.utils.source_inspection as source_inspection
from app.utils.source_inspection import (
    DEFAULT_RENDER_BUDGET,
    build_source_inspection,
    inspect_source_file,
    load_source_inspection,
    render_source_inspection,
    save_source_inspection,
)


def _write_workbook(path: Path) -> None:
    workbook = openpyxl.Workbook()
    visible = workbook.active
    assert visible is not None
    visible.title = "数据"
    visible.append(["编号", "类别", "数值"])
    visible.append([1, "甲", 1.5])
    visible.append([2, "乙", None])
    hidden = workbook.create_sheet("隐藏表")
    hidden.sheet_state = "hidden"
    hidden.append(["只读"])
    hidden.append(["模板"])
    workbook.save(path)


def test_build_inspection_profiles_csv_and_workbook_deterministically(
    tmp_path: Path,
):
    csv_path = tmp_path / "附件.csv"
    csv_path.write_text(
        "编号,编号,,混合\n1,1,甲,1\n1,1,甲,异常\n1,1,甲,异常\n,,,\n",
        encoding="utf-8",
    )
    workbook_path = tmp_path / "附件.xlsx"
    _write_workbook(workbook_path)
    (tmp_path / "result1.xlsx").write_bytes(b"template")

    first = build_source_inspection(
        tmp_path,
        task_id="task-1",
        generated_at="2026-08-30T00:00:00Z",
    )
    second = build_source_inspection(
        tmp_path,
        task_id="task-1",
        generated_at="2026-08-30T00:00:00Z",
    )

    assert first == second
    assert first["data_status"] == "source_data"
    assert first["source_files"] == ["附件.csv", "附件.xlsx"]
    assert first["skipped_files"] == ["result1.xlsx"]

    csv_entry = next(entry for entry in first["files"] if entry["path"] == "附件.csv")
    assert csv_entry["role"] == "source_data"
    assert csv_entry["status"] == "readable"
    assert csv_entry["sha256"] == hashlib.sha256(csv_path.read_bytes()).hexdigest()
    csv_sheet = csv_entry["sheets"][0]
    assert csv_sheet["name"] == "Sheet1"
    assert csv_sheet["rows"] == 4
    assert "编号" in csv_sheet["duplicate_columns"]
    assert "empty_header" in csv_sheet["warnings"]
    assert "mixed_types: 混合" in csv_sheet["warnings"]
    assert "duplicate_rows: 1" in csv_sheet["warnings"]
    assert csv_sheet["duplicate_rows"] == 1
    assert csv_sheet["scan"]["complete"] is True

    workbook_entry = next(
        entry for entry in first["files"] if entry["path"] == "附件.xlsx"
    )
    sheets = {sheet["name"]: sheet for sheet in workbook_entry["sheets"]}
    assert sheets["数据"]["hidden"] is False
    assert sheets["隐藏表"]["hidden"] is True
    assert sheets["数据"]["rows"] == 2
    assert sheets["数据"]["columns_meta"][2]["missing"] == 1


def test_inspection_records_fallback_encoding_and_bounded_scan(tmp_path: Path):
    csv_path = tmp_path / "编码.csv"
    csv_path.write_bytes("名称,数值\n甲,1\n乙,2\n".encode("gb18030"))

    entry = inspect_source_file(csv_path, max_rows=1)

    assert entry["status"] == "readable"
    sheet = entry["sheets"][0]
    assert sheet["read_method"].endswith("encoding=gb18030")
    assert "encoding_fallback: gb18030" in sheet["warnings"]
    assert sheet["scan"] == {
        "mode": "bounded",
        "rows_scanned": 1,
        "complete": False,
        "chunked": True,
    }
    assert any(warning.startswith("scan_incomplete") for warning in sheet["warnings"])


def test_bounded_sets_keep_scanning_and_clear_inexact_statistics(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(source_inspection, "MAX_SEEN_ROWS", 2)
    monkeypatch.setattr(source_inspection, "MAX_UNIQUE_VALUES", 2)
    path = tmp_path / "large.csv"
    path.write_text("编号,类别\n1,甲\n2,乙\n3,丙\n4,丁\n", encoding="utf-8")

    entry = inspect_source_file(path)
    sheet = entry["sheets"][0]

    assert sheet["rows"] == 4
    assert sheet["scan"]["rows_scanned"] == 4
    assert sheet["scan"]["complete"] is True
    assert sheet["duplicate_rows"] is None
    assert any(warning.startswith("seen_rows_capped:") for warning in sheet["warnings"])
    assert "duplicate_rows_unavailable: seen_rows capped" in sheet["warnings"]
    assert any(
        warning.startswith("unique_values_capped: 编号")
        for warning in sheet["warnings"]
    )
    assert all(
        column["unique"] is None
        and column["unique_count"] is None
        and column["values"] is None
        for column in sheet["columns_meta"]
    )


def test_scan_parameter_limits_are_applied_before_reading(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(source_inspection, "MAX_SCAN_ROWS", 1)
    monkeypatch.setattr(source_inspection, "MAX_CHUNK_SIZE", 1)
    path = tmp_path / "bounded.csv"
    path.write_text("编号\n1\n2\n", encoding="utf-8")

    entry = inspect_source_file(path, max_rows=100, chunk_size=100)

    assert entry["sheets"][0]["rows"] == 1
    assert entry["sheets"][0]["scan"]["rows_scanned"] == 1
    assert entry["sheets"][0]["scan"]["complete"] is False


def test_empty_csv_is_readable_with_explicit_empty_warning(tmp_path: Path):
    path = tmp_path / "空.csv"
    path.write_bytes(b"")

    entry = inspect_source_file(path)

    assert entry["status"] == "readable"
    assert entry["sheets"][0]["rows"] == 0
    assert "empty_table" in entry["warnings"][0]


def test_inspection_can_be_saved_and_loaded_without_changing_entries(
    tmp_path: Path,
):
    (tmp_path / "附件.csv").write_text("x\n1\n", encoding="utf-8")
    inspection = build_source_inspection(
        tmp_path,
        task_id="task-save",
        generated_at="2026-08-30T00:00:00Z",
    )

    path = save_source_inspection(tmp_path, inspection)

    assert Path(path).name == "source_inspection.json"
    assert load_source_inspection(tmp_path) == inspection


def test_inspection_assigns_collision_safe_targets_for_same_stem(
    tmp_path: Path,
):
    contents = "value\n1\n"
    (tmp_path / "sales data.csv").write_text(contents, encoding="utf-8")
    (tmp_path / "sales_data.csv").write_text(contents, encoding="utf-8")

    inspection = build_source_inspection(
        tmp_path,
        task_id="task-collision-targets",
        generated_at="2026-08-30T00:00:00Z",
    )

    source_entries = [
        entry for entry in inspection["files"] if entry["role"] == "source_data"
    ]
    targets = [
        sheet["cleaned_path"]
        for entry in source_entries
        for sheet in entry["sheets"]
    ]
    assert len(targets) == 2
    assert len(set(targets)) == 2
    assert all("__collision-" in target for target in targets)
    assert all(len(Path(target).stem.rsplit("__collision-", 1)[1]) == 8 for target in targets)
    assert all("__source_sha256-" not in target for target in targets)

    rendered = render_source_inspection(inspection, budget=12_000)
    assert all(target in rendered for target in targets)


def test_inspection_keeps_legacy_path_when_base_is_unique(tmp_path: Path):
    (tmp_path / "unique.csv").write_text("value\n1\n", encoding="utf-8")

    inspection = build_source_inspection(
        tmp_path,
        task_id="task-unique-target",
        generated_at="2026-08-30T00:00:00Z",
    )

    sheet = inspection["files"][0]["sheets"][0]
    assert sheet["cleaned_path"] == "cleaned/unique__Sheet1.csv"
    rendered = render_source_inspection(inspection)
    assert "cleaned_path=cleaned/unique__Sheet1.csv" in rendered


def test_only_result_templates_keep_no_data_branch(tmp_path: Path):
    (tmp_path / "result1_1.xlsx").write_bytes(b"x")
    (tmp_path / "RESULT2.CSV").write_text("a,b\n1,2\n", encoding="utf-8")

    inspection = build_source_inspection(
        tmp_path,
        task_id="task-no-data",
        generated_at="2026-08-30T00:00:00Z",
    )

    assert inspection["status"] == "not_applicable"
    assert inspection["data_status"] == "no_source_data"
    assert inspection["source_files"] == []
    assert inspection["skipped_files"] == ["RESULT2.CSV", "result1_1.xlsx"]
    assert all(entry["status"] == "skipped" for entry in inspection["files"])


def test_no_tabular_input_does_not_invent_sources(tmp_path: Path):
    (tmp_path / "questions.txt").write_text("机理题", encoding="utf-8")

    inspection = build_source_inspection(
        tmp_path,
        task_id="task-mechanism",
        generated_at="2026-08-30T00:00:00Z",
    )

    assert inspection["data_status"] == "no_source_data"
    assert inspection["files"] == []
    assert inspection["source_files"] == []


def test_renderer_keeps_high_priority_material_within_budget():
    inspection = {
        "version": 1,
        "task_id": "task-render",
        "status": "ready",
        "data_status": "source_data",
        "files": [
            {
                "path": "附件1.xlsx",
                "role": "source_data",
                "status": "readable",
                "bytes": 100,
                "sha256": "a" * 64,
                "warnings": [],
                "sheets": [
                    {
                        "name": "明细",
                        "hidden": False,
                        "status": "readable",
                        "rows": 100,
                        "columns": 2,
                        "columns_meta": [
                            {
                                "name": "重要列",
                                "dtype": "int",
                                "values": ["1", "2"],
                            },
                            {
                                "name": "风险列",
                                "dtype": "mixed",
                                "values": ["甲", "乙"],
                            },
                        ],
                        "warnings": ["mixed_types: 风险列"],
                        "scan": {
                            "mode": "full",
                            "complete": True,
                        },
                        "sample_rows": [["1", "甲"]] * 20,
                    }
                ],
            }
        ],
    }

    rendered = render_source_inspection(inspection, budget=300)

    assert len(rendered) <= 300
    assert "附件1.xlsx" in rendered
    assert "明细" in rendered
    assert "rows=100" in rendered
    assert "columns:" in rendered
    assert "sample:" not in rendered


def test_renderer_prioritizes_every_sheet_summary_across_files():
    files = []
    for file_index in range(2):
        sheets = []
        for sheet_index in range(2):
            sheets.append(
                {
                    "name": f"sheet-{file_index}-{sheet_index}",
                    "status": "readable",
                    "readable": True,
                    "rows": 10 + sheet_index,
                    "columns": 2,
                    "cleaned_path": (
                        f"cleaned/file-{file_index}__sheet-{sheet_index}.csv"
                    ),
                    "warnings": [f"warning-{file_index}-{sheet_index}"],
                    "scan": {"mode": "full", "complete": True},
                    "columns_meta": [
                        {"name": "value", "values": [str(sheet_index)]}
                    ],
                    "sample_rows": [["low-priority-sample"]],
                }
            )
        files.append(
            {
                "path": f"file-{file_index}.xlsx",
                "role": "source_data",
                "status": "readable",
                "bytes": 100,
                "sha256": "a" * 64,
                "sheets": sheets,
            }
        )
    inspection = {
        "version": 1,
        "task_id": "task-render-many",
        "status": "ready",
        "data_status": "source_data",
        "files": files,
    }

    rendered = render_source_inspection(inspection, budget=720)

    assert len(rendered) <= 720
    for file_index in range(2):
        for sheet_index in range(2):
            assert f"file-{file_index}.xlsx::sheet-{file_index}-{sheet_index}" in rendered
            assert f"rows={10 + sheet_index}" in rendered
            assert "cols=2" in rendered
            assert f"warning=warning-{file_index}-{sheet_index}" in rendered
    assert "low-priority-sample" not in rendered


def test_renderer_keeps_many_long_sheet_summaries_under_default_budget():
    sheets = []
    for index in range(60):
        sheets.append(
            {
                "name": f"sheet-{index}",
                "status": "readable",
                "readable": True,
                "rows": index + 1,
                "columns": 3,
                "cleaned_path": f"cleaned/source-{index}__sheet-{index}.csv",
                "warnings": [
                    f"warning-{index}-" + ("long-risk-detail " * 80)
                ],
                "scan": {"mode": "bounded", "complete": False},
                "columns_meta": [],
                "sample_rows": [],
            }
        )
    inspection = {
        "version": 1,
        "task_id": "task-render-long-many",
        "status": "ready",
        "data_status": "source_data",
        "files": [
            {
                "path": "source-with-a-very-long-name-" + ("x" * 180) + ".xlsx",
                "role": "source_data",
                "status": "readable",
                "warnings": [],
                "sheets": sheets,
            }
        ],
    }

    rendered = render_source_inspection(inspection)

    assert len(rendered) <= DEFAULT_RENDER_BUDGET
    for index in range(60):
        assert f"sheet-{index}" in rendered
        assert f"rows={index + 1}" in rendered
        assert "cols=3" in rendered
        assert f"warning=warning-{index}-" in rendered


def test_renderer_extreme_budget_keeps_each_sheet_locator_and_summary():
    sheets = [
        {
            "id": f"source-entry-{index}",
            "name": f"sheet-{index}",
            "status": "readable",
            "readable": True,
            "rows": index + 10,
            "columns": 2,
            "warnings": [f"warning-{index}: " + ("detail " * 20)],
            "scan": {"mode": "bounded", "complete": False},
            "columns_meta": [{"name": "hidden-detail"}],
            "sample_rows": [["low-priority-sample"]],
        }
        for index in range(6)
    ]
    inspection = {
        "version": 1,
        "task_id": "task-render-extreme",
        "status": "ready",
        "data_status": "source_data",
        "files": [
            {
                "path": "source.xlsx",
                "role": "source_data",
                "status": "readable",
                "sheets": sheets,
            }
        ],
    }

    rendered = render_source_inspection(inspection, budget=520)

    assert len(rendered) <= 520
    for index in range(6):
        assert f"source-entry-{index}" in rendered
        assert "status=readable" in rendered
        assert f"rows={index + 10}" in rendered
        assert "cols=2" in rendered
        assert f"warning=warning-{index}" in rendered
    assert "columns:" not in rendered
    assert "low-priority-sample" not in rendered
