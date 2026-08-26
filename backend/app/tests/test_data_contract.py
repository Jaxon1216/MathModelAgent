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
    list_source_data_files,
    render_contract_for_llm,
    render_contract_summary_for_writer,
    sanitize_filename_token,
    save_data_contract,
    truncate_cleaning_notes,
    validate_data_contract,
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
