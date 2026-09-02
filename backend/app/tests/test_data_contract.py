"""data_contract 口径：命名、跳过 result 模板、构建/校验、限长渲染。"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from app.utils.data_contract import (
    CLEANED_DIR_NAME,
    DataContractError,
    build_data_contract,
    cleaned_relpath_for,
    format_data_prep_brief,
    has_source_data,
    infer_source_filename,
    list_source_data_files,
    render_contract_for_llm,
    render_contract_summary_for_writer,
    sanitize_filename_token,
    save_data_contract,
    truncate_cleaning_notes,
    validate_data_contract,
)
from app.utils.source_inspection import (
    build_source_inspection,
    render_source_inspection,
    save_source_inspection,
)


def _write_cleaned_csv(work_dir: Path, filename: str, df: pd.DataFrame) -> None:
    cleaned = work_dir / CLEANED_DIR_NAME
    cleaned.mkdir(parents=True, exist_ok=True)
    df.to_csv(cleaned / filename, index=False, encoding="utf-8")


def test_sanitize_filename_token_strips_illegal_and_spaces():
    assert sanitize_filename_token("乡村 现有/耕地") == "乡村_现有_耕地"
    assert sanitize_filename_token("a:b*c?") == "a_b_c"


def test_cleaned_relpath_for_uses_stem_and_sheet():
    assert (
        cleaned_relpath_for("附件1.xlsx", "乡村现有耕地")
        == "cleaned/附件1__乡村现有耕地.csv"
    )
    assert cleaned_relpath_for("附件2.csv", "Sheet1") == "cleaned/附件2__Sheet1.csv"


def test_cleaned_relpath_for_sanitizes_special_source_and_sheet_names():
    assert cleaned_relpath_for("附件 1__原始 数据.csv", "明细 Sheet") == (
        "cleaned/附件_1__原始_数据__明细_Sheet.csv"
    )


def test_infer_source_filename_matches_sanitized_stem(tmp_path: Path):
    source = tmp_path / "附件 1 (原始).csv"
    source.write_text("x\n1\n", encoding="utf-8")

    assert (
        infer_source_filename(str(tmp_path), "附件_1_(原始)")
        == source.name
    )


def test_list_source_data_files_skips_result_templates_and_cleaned(tmp_path: Path):
    (tmp_path / "附件1.xlsx").write_bytes(b"x")
    (tmp_path / "附件2.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (tmp_path / "result1_1.xlsx").write_bytes(b"x")
    (tmp_path / "result2.csv").write_text("x", encoding="utf-8")
    cleaned = tmp_path / CLEANED_DIR_NAME
    cleaned.mkdir()
    (cleaned / "附件1__Sheet1.csv").write_text("a\n1\n", encoding="utf-8")

    files = list_source_data_files(str(tmp_path))
    assert files == ["附件1.xlsx", "附件2.csv"]
    assert has_source_data(str(tmp_path)) is True


def test_has_source_data_false_when_only_result_templates(tmp_path: Path):
    (tmp_path / "result1_1.xlsx").write_bytes(b"x")
    assert has_source_data(str(tmp_path)) is False


def test_build_data_contract_profiles_cleaned_csv_and_infers_source(tmp_path: Path):
    (tmp_path / "附件1.xlsx").write_bytes(b"x")
    df = pd.DataFrame(
        {
            "地块名称": ["地块1", "地块2"],
            "地块类型": ["平旱地", "梯田"],
            "面积": [23.5, 18.2],
        }
    )
    _write_cleaned_csv(tmp_path, "附件1__乡村现有耕地.csv", df)

    contract = build_data_contract(str(tmp_path), cleaning_notes="缺失已填中位数")
    assert contract["version"] == 1
    assert contract["cleaning_notes"] == "缺失已填中位数"
    assert len(contract["tables"]) == 1
    table = contract["tables"][0]
    assert table["path"] == "cleaned/附件1__乡村现有耕地.csv"
    assert table["source"] == "附件1.xlsx"
    assert table["sheet"] == "乡村现有耕地"
    assert table["rows"] == 2
    names = [c["name"] for c in table["columns"]]
    assert names == ["地块名称", "地块类型", "面积"]
    type_col = next(c for c in table["columns"] if c["name"] == "地块类型")
    assert type_col["dtype"] == "string"
    assert set(type_col["values"]) == {"平旱地", "梯田"}
    assert len(table["sample_rows"]) == 2


def test_contract_provenance_tracks_source_and_allowed_cleaning_change(
    tmp_path: Path,
):
    source = tmp_path / "附件1.csv"
    source.write_text("编号,类别\n1,甲\n2,乙\n2,乙\n", encoding="utf-8")
    inspection = build_source_inspection(
        tmp_path,
        task_id="task-provenance",
        generated_at="2026-08-30T00:00:00Z",
    )
    save_source_inspection(tmp_path, inspection)
    _write_cleaned_csv(
        tmp_path,
        "附件1__Sheet1.csv",
        pd.DataFrame({"编号": [1, 2], "类别": ["甲", "乙"]}),
    )

    contract = build_data_contract(
        str(tmp_path),
        cleaning_notes="动作：删除 1 行重复记录\n限制：未改变原始附件",
        source_inspection=inspection,
    )
    table = contract["tables"][0]
    provenance = table["provenance"]

    assert contract["version"] == 1
    assert contract["source_inspection"]["path"] == "source_inspection.json"
    assert provenance["source_file"] == "附件1.csv"
    assert provenance["source_sheet"] == "Sheet1"
    assert provenance["inspection_entry_id"] == "附件1.csv::Sheet1"
    assert provenance["before"]["rows"] == 3
    assert provenance["after"]["rows"] == 2
    assert provenance["source_sha256"] == inspection["files"][0]["sha256"]
    assert (
        provenance["inspection_file_sha256"]
        == contract["source_inspection"]["sha256"]
    )
    assert provenance["cleaned_sha256"]
    assert provenance["actions"]
    validate_data_contract(str(tmp_path), contract)


def test_contract_provenance_resolves_special_source_and_sheet_names(
    tmp_path: Path,
):
    source = tmp_path / "附件 1__原始 数据.xlsx"
    pd.DataFrame({"编号": [1, 2], "类别": ["甲", "乙"]}).to_excel(
        source,
        sheet_name="明细 Sheet",
        index=False,
    )
    inspection = build_source_inspection(
        tmp_path,
        task_id="task-special-provenance",
        generated_at="2026-08-30T00:00:00Z",
    )
    save_source_inspection(tmp_path, inspection)
    cleaned_name = Path(
        cleaned_relpath_for(source.name, "明细 Sheet")
    ).name
    _write_cleaned_csv(
        tmp_path,
        cleaned_name,
        pd.DataFrame({"编号": [1, 2], "类别": ["甲", "乙"]}),
    )

    contract = build_data_contract(
        str(tmp_path),
        cleaning_notes="保留全部记录",
        source_inspection=inspection,
    )
    table = contract["tables"][0]
    provenance = table["provenance"]

    assert table["source"] == source.name
    assert table["sheet"] == "明细 Sheet"
    assert provenance["source_file"] == source.name
    assert provenance["source_sheet"] == "明细 Sheet"
    assert (
        provenance["inspection_entry_id"]
        == f"{source.name}::明细 Sheet"
    )
    validate_data_contract(str(tmp_path), contract)


def test_contract_resolves_same_stem_and_multi_sheet_collision_targets(
    tmp_path: Path,
):
    first_source = tmp_path / "sales data.xlsx"
    second_source = tmp_path / "sales_data.xlsx"
    with pd.ExcelWriter(first_source) as writer:
        pd.DataFrame({"value": [1, 2]}).to_excel(
            writer, sheet_name="Sheet1", index=False
        )
        pd.DataFrame({"value": [3]}).to_excel(
            writer, sheet_name="Data Sheet", index=False
        )
    with pd.ExcelWriter(second_source) as writer:
        pd.DataFrame({"value": [4, 5, 6]}).to_excel(
            writer, sheet_name="Sheet1", index=False
        )

    inspection = build_source_inspection(
        tmp_path,
        task_id="task-collision-provenance",
        generated_at="2026-08-30T00:00:00Z",
    )
    save_source_inspection(tmp_path, inspection)

    source_sheets: list[tuple[str, str, str]] = []
    for file_entry in inspection["files"]:
        if file_entry["role"] != "source_data":
            continue
        for sheet in file_entry["sheets"]:
            source_sheets.append(
                (
                    file_entry["path"],
                    sheet["name"],
                    sheet["cleaned_path"],
                )
            )
    assert len(source_sheets) == 3
    assert len({path for _, _, path in source_sheets}) == 3
    collision_paths = [
        path
        for source, sheet, path in source_sheets
        if sheet == "Sheet1"
    ]
    assert len(collision_paths) == 2
    assert all("__collision-" in path for path in collision_paths)
    assert all("__source_sha256-" not in path for path in collision_paths)

    for source_file, sheet_name, cleaned_path in source_sheets:
        source_entry = next(
            entry
            for entry in inspection["files"]
            if entry["path"] == source_file
        )
        sheet_entry = next(
            sheet
            for sheet in source_entry["sheets"]
            if sheet["name"] == sheet_name
        )
        _write_cleaned_csv(
            tmp_path,
            Path(cleaned_path).name,
            pd.DataFrame({"value": list(range(sheet_entry["rows"]))}),
        )

    contract = build_data_contract(
        str(tmp_path),
        cleaning_notes="保留全部记录",
        source_inspection=inspection,
    )
    table_sources = {
        table["path"]: (table["source"], table["sheet"])
        for table in contract["tables"]
    }
    assert table_sources == {
        cleaned_path: (source_file, sheet_name)
        for source_file, sheet_name, cleaned_path in source_sheets
    }
    assert len({table["id"] for table in contract["tables"]}) == len(
        contract["tables"]
    )
    assert {
        table["provenance"]["inspection_entry_id"]
        for table in contract["tables"]
    } == {
        sheet["id"]
        for entry in inspection["files"]
        if entry["role"] == "source_data"
        for sheet in entry["sheets"]
    }

    save_data_contract(str(tmp_path), contract)
    validate_data_contract(str(tmp_path))

    second_source.write_bytes(second_source.read_bytes() + b"changed")
    with pytest.raises(DataContractError, match="源文件 hash 不一致"):
        validate_data_contract(str(tmp_path))


def test_csv_collision_paths_keep_end_to_end_provenance(
    tmp_path: Path,
):
    sources = {
        "a b.csv": "value\n1\n2\n",
        "a_b.csv": "value\n3\n4\n",
    }
    for filename, content in sources.items():
        (tmp_path / filename).write_text(content, encoding="utf-8")

    inspection = build_source_inspection(
        tmp_path,
        task_id="task-csv-collision-provenance",
        generated_at="2026-08-30T00:00:00Z",
    )
    save_source_inspection(tmp_path, inspection)

    source_sheets = [
        (entry, sheet)
        for entry in inspection["files"]
        if entry["role"] == "source_data"
        for sheet in entry["sheets"]
    ]
    declared_paths = [sheet["cleaned_path"] for _, sheet in source_sheets]
    assert len(set(declared_paths)) == 2
    assert all("__collision-" in path for path in declared_paths)
    assert all("__source_sha256-" not in path for path in declared_paths)

    for entry, sheet in source_sheets:
        _write_cleaned_csv(
            tmp_path,
            Path(sheet["cleaned_path"]).name,
            pd.DataFrame({"value": [1, 2]}),
        )

    contract = build_data_contract(
        str(tmp_path),
        cleaning_notes="保留全部记录",
        source_inspection=inspection,
    )
    table_by_source = {
        (table["source"], table["sheet"]): table
        for table in contract["tables"]
    }
    for entry, sheet in source_sheets:
        table = table_by_source[(entry["path"], sheet["name"])]
        assert table["path"] == sheet["cleaned_path"]
        assert table["provenance"]["inspection_entry_id"] == sheet["id"]
        assert table["provenance"]["source_sha256"] == entry["sha256"]

    save_data_contract(str(tmp_path), contract)
    validate_data_contract(str(tmp_path))


def test_contract_rejects_cleaned_path_not_declared_by_inspection(
    tmp_path: Path,
):
    (tmp_path / "a b.csv").write_text("value\n1\n", encoding="utf-8")
    (tmp_path / "a_b.csv").write_text("value\n2\n", encoding="utf-8")
    inspection = build_source_inspection(
        tmp_path,
        task_id="task-undeclared-path",
        generated_at="2026-08-30T00:00:00Z",
    )
    _write_cleaned_csv(
        tmp_path,
        "a_b__Sheet1.csv",
        pd.DataFrame({"value": [1]}),
    )

    with pytest.raises(DataContractError, match="未在 source inspection 中声明"):
        build_data_contract(str(tmp_path), source_inspection=inspection)


def test_contract_rejects_multiple_declared_sources_for_one_path(
    tmp_path: Path,
):
    (tmp_path / "a b.csv").write_text("value\n1\n", encoding="utf-8")
    (tmp_path / "a_b.csv").write_text("value\n2\n", encoding="utf-8")
    inspection = build_source_inspection(
        tmp_path,
        task_id="task-ambiguous-declaration",
        generated_at="2026-08-30T00:00:00Z",
    )
    for entry in inspection["files"]:
        if entry["role"] == "source_data":
            entry["sheets"][0]["cleaned_path"] = "cleaned/shared.csv"
    _write_cleaned_csv(
        tmp_path,
        "shared.csv",
        pd.DataFrame({"value": [1]}),
    )

    with pytest.raises(DataContractError, match="来源映射不唯一"):
        build_data_contract(str(tmp_path), source_inspection=inspection)


def test_contract_rejects_uncovered_readable_sheet(tmp_path: Path):
    source = tmp_path / "附件.xlsx"
    with pd.ExcelWriter(source) as writer:
        pd.DataFrame({"value": [1]}).to_excel(
            writer, sheet_name="数据", index=False
        )
        pd.DataFrame({"value": [2]}).to_excel(
            writer, sheet_name="附表", index=False
        )
    inspection = build_source_inspection(
        tmp_path,
        task_id="task-coverage",
        generated_at="2026-08-30T00:00:00Z",
    )
    data_sheet = next(
        sheet
        for entry in inspection["files"]
        for sheet in entry["sheets"]
        if sheet["name"] == "数据"
    )
    _write_cleaned_csv(
        tmp_path,
        Path(data_sheet["cleaned_path"]).name,
        pd.DataFrame({"value": [1]}),
    )
    save_source_inspection(tmp_path, inspection)

    with pytest.raises(
        DataContractError,
        match="readable sheet coverage 不完整",
    ):
        build_data_contract(str(tmp_path), source_inspection=inspection)


def test_contract_validation_rejects_removed_readable_sheet_table(
    tmp_path: Path,
):
    source = tmp_path / "附件.xlsx"
    with pd.ExcelWriter(source) as writer:
        pd.DataFrame({"value": [1]}).to_excel(
            writer, sheet_name="数据", index=False
        )
        pd.DataFrame({"value": [2]}).to_excel(
            writer, sheet_name="附表", index=False
        )
    inspection = build_source_inspection(
        tmp_path,
        task_id="task-coverage-validation",
        generated_at="2026-08-30T00:00:00Z",
    )
    for entry in inspection["files"]:
        for sheet in entry["sheets"]:
            _write_cleaned_csv(
                tmp_path,
                Path(sheet["cleaned_path"]).name,
                pd.DataFrame({"value": [sheet["rows"]]}),
            )
    save_source_inspection(tmp_path, inspection)
    contract = build_data_contract(
        str(tmp_path),
        source_inspection=inspection,
        cleaning_notes="保留全部记录",
    )
    contract["tables"].pop()

    with pytest.raises(
        DataContractError,
        match="readable sheet coverage 不完整",
    ):
        validate_data_contract(str(tmp_path), contract)


def test_contract_allows_explicit_machine_readable_skip_reason(
    tmp_path: Path,
):
    source = tmp_path / "附件.xlsx"
    with pd.ExcelWriter(source) as writer:
        pd.DataFrame({"value": [1]}).to_excel(
            writer, sheet_name="数据", index=False
        )
        pd.DataFrame({"metadata": ["附表"]}).to_excel(
            writer, sheet_name="附表", index=False
        )
    inspection = build_source_inspection(
        tmp_path,
        task_id="task-coverage-skip",
        generated_at="2026-08-30T00:00:00Z",
    )
    skipped_sheet = next(
        sheet
        for entry in inspection["files"]
        for sheet in entry["sheets"]
        if sheet["name"] == "附表"
    )
    skipped_sheet["skip_reason"] = "not_model_data: metadata-only sheet"
    data_sheet = next(
        sheet
        for entry in inspection["files"]
        for sheet in entry["sheets"]
        if sheet["name"] == "数据"
    )
    _write_cleaned_csv(
        tmp_path,
        Path(data_sheet["cleaned_path"]).name,
        pd.DataFrame({"value": [1]}),
    )
    save_source_inspection(tmp_path, inspection)

    contract = build_data_contract(
        str(tmp_path),
        source_inspection=inspection,
        cleaning_notes="跳过元数据表",
    )
    save_data_contract(str(tmp_path), contract)
    validate_data_contract(str(tmp_path))


@pytest.mark.parametrize("skip_reason", [None, "", 0, [], " \t"])
def test_contract_rejects_invalid_skip_reason_even_with_cleaned_csv(
    tmp_path: Path,
    skip_reason: object,
):
    source = tmp_path / "附件.csv"
    source.write_text("value\n1\n", encoding="utf-8")
    inspection = build_source_inspection(
        tmp_path,
        task_id="task-invalid-skip-with-cleaned",
        generated_at="2026-08-30T00:00:00Z",
    )
    sheet = inspection["files"][0]["sheets"][0]
    sheet["skip_reason"] = skip_reason
    _write_cleaned_csv(
        tmp_path,
        Path(sheet["cleaned_path"]).name,
        pd.DataFrame({"value": [1]}),
    )

    with pytest.raises(
        DataContractError,
        match="skip_reason 必须是非空字符串",
    ):
        build_data_contract(str(tmp_path), source_inspection=inspection)


def test_validate_rejects_invalid_skip_reason_even_with_cleaned_csv(
    tmp_path: Path,
):
    source = tmp_path / "附件.csv"
    source.write_text("value\n1\n", encoding="utf-8")
    inspection = build_source_inspection(
        tmp_path,
        task_id="task-invalid-skip-validation",
        generated_at="2026-08-30T00:00:00Z",
    )
    save_source_inspection(tmp_path, inspection)
    sheet = inspection["files"][0]["sheets"][0]
    _write_cleaned_csv(
        tmp_path,
        Path(sheet["cleaned_path"]).name,
        pd.DataFrame({"value": [1]}),
    )
    contract = build_data_contract(str(tmp_path), source_inspection=inspection)
    save_data_contract(str(tmp_path), contract)

    sheet["skip_reason"] = None
    save_source_inspection(tmp_path, inspection)
    contract["source_inspection"]["sha256"] = None

    with pytest.raises(
        DataContractError,
        match="skip_reason 必须是非空字符串",
    ):
        validate_data_contract(str(tmp_path), contract)


def test_contract_allows_no_cleaned_csv_when_all_readable_sheets_are_skipped(
    tmp_path: Path,
):
    source = tmp_path / "附件.xlsx"
    with pd.ExcelWriter(source) as writer:
        pd.DataFrame({"value": [1]}).to_excel(
            writer, sheet_name="数据", index=False
        )
        pd.DataFrame({"metadata": ["附表"]}).to_excel(
            writer, sheet_name="附表", index=False
        )
    inspection = build_source_inspection(
        tmp_path,
        task_id="task-no-data-skipped",
        generated_at="2026-08-30T00:00:00Z",
    )
    for entry in inspection["files"]:
        if entry["role"] != "source_data":
            continue
        for sheet in entry["sheets"]:
            sheet["skip_reason"] = f"not_model_data: {sheet['name']}"
    save_source_inspection(tmp_path, inspection)

    contract = build_data_contract(
        str(tmp_path),
        cleaning_notes="所有源 sheet 均明确标记为非建模数据",
        source_inspection=inspection,
    )

    assert contract["status"] == "skipped"
    assert contract["data_status"] == "no_data_skipped"
    assert contract["tables"] == []
    assert len(contract["skipped_sheets"]) == 2
    assert "附表" in render_contract_for_llm(contract)
    assert "无可用清洗数据表" in render_contract_summary_for_writer(contract)

    save_data_contract(str(tmp_path), contract)
    validate_data_contract(str(tmp_path))


@pytest.mark.parametrize("skip_reason", [None, ""])
def test_contract_rejects_no_cleaned_csv_without_skip_reason(
    tmp_path: Path,
    skip_reason: str | None,
):
    source = tmp_path / "附件.csv"
    source.write_text("value\n1\n", encoding="utf-8")
    inspection = build_source_inspection(
        tmp_path,
        task_id="task-no-data-unskipped",
        generated_at="2026-08-30T00:00:00Z",
    )
    if skip_reason is not None:
        inspection["files"][0]["sheets"][0]["skip_reason"] = skip_reason
    save_source_inspection(tmp_path, inspection)

    with pytest.raises(
        DataContractError,
        match="readable sheet coverage 不完整",
    ):
        build_data_contract(str(tmp_path), source_inspection=inspection)


def test_validate_legacy_version_one_contract_without_provenance(
    tmp_path: Path,
):
    _write_cleaned_csv(
        tmp_path,
        "附件1__Sheet1.csv",
        pd.DataFrame({"value": [1]}),
    )
    contract = {
        "version": 1,
        "tables": [
            {
                "id": "附件1__Sheet1",
                "path": "cleaned/附件1__Sheet1.csv",
                "source": "附件1.csv",
                "sheet": "Sheet1",
                "rows": 1,
                "columns": [
                    {
                        "name": "value",
                        "dtype": "int",
                        "missing_pct": 0.0,
                        "nunique": 1,
                    }
                ],
                "sample_rows": [[1]],
            }
        ],
    }
    save_data_contract(str(tmp_path), contract)

    validate_data_contract(str(tmp_path))


def test_contract_validation_rejects_changed_source_hash(tmp_path: Path):
    source = tmp_path / "附件1.csv"
    source.write_text("x\n1\n", encoding="utf-8")
    inspection = build_source_inspection(
        tmp_path,
        task_id="task-hash",
        generated_at="2026-08-30T00:00:00Z",
    )
    save_source_inspection(tmp_path, inspection)
    _write_cleaned_csv(tmp_path, "附件1__Sheet1.csv", pd.DataFrame({"x": [1]}))
    contract = build_data_contract(
        str(tmp_path), cleaning_notes="保留全部记录", source_inspection=inspection
    )
    source.write_text("x\n999\n", encoding="utf-8")

    with pytest.raises(DataContractError, match="源文件 hash 不一致"):
        validate_data_contract(str(tmp_path), contract)


def test_contract_validation_rejects_missing_inspection_entry(tmp_path: Path):
    source = tmp_path / "附件1.csv"
    source.write_text("x\n1\n", encoding="utf-8")
    inspection = build_source_inspection(
        tmp_path,
        task_id="task-entry",
        generated_at="2026-08-30T00:00:00Z",
    )
    save_source_inspection(tmp_path, inspection)
    _write_cleaned_csv(tmp_path, "附件1__Sheet1.csv", pd.DataFrame({"x": [1]}))
    contract = build_data_contract(
        str(tmp_path), cleaning_notes="保留全部记录", source_inspection=inspection
    )

    inspection["files"][0]["sheets"] = []
    save_source_inspection(tmp_path, inspection)
    contract["source_inspection"]["sha256"] = None

    with pytest.raises(
        DataContractError, match="未在 source inspection 中声明 cleaned_path"
    ):
        validate_data_contract(str(tmp_path), contract)


def test_contract_validation_rejects_unexplained_row_change(tmp_path: Path):
    source = tmp_path / "附件1.csv"
    source.write_text("x\n1\n2\n", encoding="utf-8")
    inspection = build_source_inspection(
        tmp_path,
        task_id="task-change",
        generated_at="2026-08-30T00:00:00Z",
    )
    save_source_inspection(tmp_path, inspection)
    _write_cleaned_csv(tmp_path, "附件1__Sheet1.csv", pd.DataFrame({"x": [1]}))
    contract = build_data_contract(
        str(tmp_path), source_inspection=inspection
    )

    with pytest.raises(DataContractError, match="缺少动作或限制说明"):
        validate_data_contract(str(tmp_path), contract)


def test_bounded_scan_does_not_explain_unreported_row_change(tmp_path: Path):
    source = tmp_path / "附件1.csv"
    source.write_text("x\n1\n2\n", encoding="utf-8")
    inspection = build_source_inspection(
        tmp_path,
        task_id="task-bounded-change",
        generated_at="2026-08-30T00:00:00Z",
        max_rows=1,
    )
    save_source_inspection(tmp_path, inspection)
    _write_cleaned_csv(
        tmp_path,
        "附件1__Sheet1.csv",
        pd.DataFrame({"x": [1, 2]}),
    )

    contract = build_data_contract(
        str(tmp_path),
        cleaning_notes="source inspection scan is bounded",
        source_inspection=inspection,
    )

    with pytest.raises(DataContractError, match="行数变化"):
        validate_data_contract(str(tmp_path), contract)


def test_build_data_contract_empty_cleaned_raises(tmp_path: Path):
    (tmp_path / CLEANED_DIR_NAME).mkdir()
    with pytest.raises(DataContractError, match="没有 csv"):
        build_data_contract(str(tmp_path))


def test_validate_data_contract_passes_then_fails_on_row_mismatch(tmp_path: Path):
    df = pd.DataFrame({"x": [1, 2, 3], "y": ["a", "b", "c"]})
    _write_cleaned_csv(tmp_path, "附件2__Sheet1.csv", df)
    contract = build_data_contract(str(tmp_path))
    save_data_contract(str(tmp_path), contract)
    validate_data_contract(str(tmp_path))

    df2 = pd.DataFrame({"x": [1], "y": ["a"]})
    _write_cleaned_csv(tmp_path, "附件2__Sheet1.csv", df2)
    with pytest.raises(DataContractError, match="行数不一致"):
        validate_data_contract(str(tmp_path), contract)


def test_validate_data_contract_fails_on_missing_file(tmp_path: Path):
    df = pd.DataFrame({"x": [1]})
    _write_cleaned_csv(tmp_path, "附件1__Sheet1.csv", df)
    contract = build_data_contract(str(tmp_path))
    (tmp_path / CLEANED_DIR_NAME / "附件1__Sheet1.csv").unlink()
    with pytest.raises(DataContractError, match="缺少清洗表"):
        validate_data_contract(str(tmp_path), contract)


def test_render_contract_for_llm_drops_samples_when_over_budget(tmp_path: Path):
    df = pd.DataFrame(
        {
            "地块名称": [f"地块{i}" for i in range(3)],
            "类型": ["平旱地", "梯田", "水浇地"],
        }
    )
    _write_cleaned_csv(tmp_path, "附件1__耕地.csv", df)
    contract = build_data_contract(str(tmp_path), cleaning_notes="ok")
    full = render_contract_for_llm(contract, budget=8000)
    assert "cleaned/附件1__耕地.csv" in full
    assert "sample:" in full
    assert "清洗摘要" in full

    short = render_contract_for_llm(contract, budget=max(180, len(full) // 3))
    assert "cleaned/附件1__耕地.csv" in short
    assert "sample:" not in short


def test_render_contract_summary_for_writer_omits_samples(tmp_path: Path):
    df = pd.DataFrame({"面积": [1.0, 2.0], "类型": ["平旱地", "梯田"]})
    _write_cleaned_csv(tmp_path, "附件1__Sheet1.csv", df)
    contract = build_data_contract(str(tmp_path), cleaning_notes="填了缺失")
    summary = render_contract_summary_for_writer(contract)
    assert "cleaned/附件1__Sheet1.csv" in summary
    assert "面积" in summary
    assert "填了缺失" in summary
    assert "sample" not in summary.lower()
    dumped = json.dumps(contract)
    assert dumped not in summary


def test_truncate_cleaning_notes_caps_length():
    notes = "字" * 2000
    out = truncate_cleaning_notes(notes, max_chars=100)
    assert len(out) <= 101
    assert out.endswith("…")


def test_format_data_prep_brief_lists_files_and_naming():
    brief = format_data_prep_brief(["附件1.xlsx", "附件2.csv"])
    assert "附件1.xlsx" in brief
    assert "cleaned/" in brief
    assert "{附件主名}__{sheet名}.csv" in brief
    assert "result*" in brief
    assert "源数据探查报告已落盘" in brief
    assert "source_inspection.json" not in brief
    assert "warning=mixed_types" not in brief
    assert "必须严格使用该声明路径" in brief


def test_format_data_prep_brief_includes_declared_cleaned_paths(tmp_path: Path):
    (tmp_path / "a b.csv").write_text("value\n1\n", encoding="utf-8")
    (tmp_path / "a_b.csv").write_text("value\n2\n", encoding="utf-8")
    inspection = build_source_inspection(
        tmp_path,
        task_id="task-brief-paths",
        generated_at="2026-08-30T00:00:00Z",
    )
    brief = format_data_prep_brief(["a b.csv", "a_b.csv"])

    for entry in inspection["files"]:
        for sheet in entry["sheets"]:
            assert f"cleaned_path={sheet['cleaned_path']}" not in brief
