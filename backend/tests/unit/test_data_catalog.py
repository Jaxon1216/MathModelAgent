"""只读附件扫描与 DataCatalog 测试。"""

from pathlib import Path

import pytest
from openpyxl import Workbook
from pydantic import ValidationError

from app.data.preparation import (
    DataCatalogInspectionError,
    extract_expected_question_ids,
    inspect_data_catalog,
)
from app.domain.problem import DataColumn, DataTable, Problem

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _write_xlsx(path: Path, sheets: dict[str, list[str]]) -> None:
    """写入仅含表头的测试工作簿。"""
    workbook = Workbook()
    active_sheet = workbook.active
    assert active_sheet is not None
    workbook.remove(active_sheet)
    for sheet_name, columns in sheets.items():
        sheet = workbook.create_sheet(sheet_name)
        sheet.append(columns)
    workbook.save(path)


def test_inspection_is_sheet_aware_and_excludes_output_templates(tmp_path: Path):
    """Excel 每个 sheet 独立成表，result 模板不能成为输入数据。"""
    _write_xlsx(
        tmp_path / "附件1.xlsx",
        {
            "现有耕地": ["地块名称", "说明 "],
            "农作物": ["作物编号", "作物名称"],
        },
    )
    _write_xlsx(tmp_path / "result1.xlsx", {"2024": ["地块名", "小麦"]})
    (tmp_path / "history.csv").write_text("year,yield\n", encoding="utf-8")
    (tmp_path / "ignore.txt").write_text("not data", encoding="utf-8")

    catalog = inspect_data_catalog(tmp_path)

    assert [table.table_id for table in catalog.input_tables] == [
        "history.csv",
        "附件1.xlsx::现有耕地",
        "附件1.xlsx::农作物",
    ]
    assert catalog.output_templates == ("result1.xlsx",)
    land_table = catalog.get_table("附件1.xlsx::现有耕地")
    assert land_table is not None
    assert [(column.name, column.source_name) for column in land_table.columns] == [
        ("地块名称", "地块名称"),
        ("说明", "说明 "),
    ]


def test_inspection_rejects_columns_that_collide_after_normalization(
    tmp_path: Path,
):
    """原始表头规范化后重名时必须失败，不能生成歧义 table。"""
    (tmp_path / "duplicate.csv").write_text('"作物","作物 "\n', encoding="utf-8")

    with pytest.raises(DataCatalogInspectionError, match="规范列名不能重复"):
        inspect_data_catalog(tmp_path)


def test_inspection_rejects_unreadable_supported_file(tmp_path: Path):
    """损坏的受支持附件不能被静默忽略。"""
    (tmp_path / "broken.xlsx").write_bytes(b"not-an-xlsx")

    with pytest.raises(DataCatalogInspectionError, match="broken.xlsx"):
        inspect_data_catalog(tmp_path)


def test_inspection_preserves_csv_skip_blank_lines_behavior(tmp_path: Path):
    """原始表头读取应与 pandas 一致地跳过文件开头的空行。"""
    (tmp_path / "history.csv").write_text(
        "\n\nyear,yield\n2024,10\n",
        encoding="utf-8",
    )

    catalog = inspect_data_catalog(tmp_path)

    assert [column.name for column in catalog.input_tables[0].columns] == [
        "year",
        "yield",
    ]


def test_inspection_rejects_supported_symlink(tmp_path: Path):
    """受支持格式的符号链接不能被静默当作无数据。"""
    outside = tmp_path.parent / f"{tmp_path.name}-outside.csv"
    outside.write_text("id,value\n1,a\n", encoding="utf-8")
    (tmp_path / "linked.csv").symlink_to(outside)

    with pytest.raises(DataCatalogInspectionError, match="符号链接"):
        inspect_data_catalog(tmp_path)


def test_inspection_rejects_missing_work_dir(tmp_path: Path):
    """不存在的工作目录应给出明确输入错误。"""
    missing = tmp_path / "missing"

    with pytest.raises(DataCatalogInspectionError, match="工作目录不存在"):
        inspect_data_catalog(missing)


@pytest.mark.parametrize(
    ("problem_text", "expected"),
    [
        (
            "问题1：预测产量。问题2：优化方案。",
            ("ques1", "ques2"),
        ),
        (
            "请研究下列问题：\n问题 1 建立模型。\n问题 2 验证模型。",
            ("ques1", "ques2"),
        ),
        (
            "第 1 问：建立模型。\n第 2 问：验证模型。",
            ("ques1", "ques2"),
        ),
    ],
)
def test_question_heading_extraction_is_deterministic(
    problem_text: str,
    expected: tuple[str, ...],
):
    """明确且连续的编号题头形成独立预期问题集合。"""
    assert extract_expected_question_ids(problem_text) == expected


@pytest.mark.parametrize(
    "problem_text",
    [
        "请根据附件建立数学模型并给出完整方案。",
        "问题 1 建立模型。\n问题 3 验证模型。",
        "问题 1 建立模型。\n问题 1 再次建立模型。",
    ],
)
def test_question_heading_extraction_returns_none_when_boundary_is_unreliable(
    problem_text: str,
):
    """无题头、编号跳跃或重复时不能猜测问题数量。"""
    assert extract_expected_question_ids(problem_text) is None


def test_data_table_rejects_unstable_table_id():
    """table_id 必须能由真实文件和 sheet 唯一重建。"""
    with pytest.raises(ValidationError, match="table_id 必须等于"):
        DataTable(
            table_id="自定义别名",
            file="附件.xlsx",
            sheet="统计",
            columns=(DataColumn(name="产量", source_name="产量"),),
        )


def test_modeler_fixture_catalog_matches_repository_example():
    """冻结 smoke fixture 必须与仓库基线附件的真实表头一致。"""
    fixture = Problem.model_validate_json(
        (BACKEND_ROOT / "fixtures" / "modeler" / "2024高教杯C题.json").read_text(
            encoding="utf-8"
        )
    )
    actual = inspect_data_catalog(
        BACKEND_ROOT / "app" / "example" / "example" / "2024高教杯C题"
    )

    assert actual == fixture.data_catalog
