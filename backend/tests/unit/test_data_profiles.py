"""M1.5 逐表只读 DataProfile 测试。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.data import (
    DataProfileInspectionError,
    M15ArtifactStore,
    inspect_data_profiles,
)
from app.domain.m15 import DataIssue, DataProfile

TASK_OUTLINE_ID = "task-outline:profile-test"


def _write_xlsx(
    path: Path,
    sheets: dict[str, list[list[object]]],
) -> None:
    """写入包含明确表头和数据行的测试工作簿。"""
    workbook = Workbook()
    active_sheet = workbook.active
    assert active_sheet is not None
    workbook.remove(active_sheet)
    for sheet_name, rows in sheets.items():
        sheet = workbook.create_sheet(sheet_name)
        for row in rows:
            sheet.append(row)
    workbook.save(path)


def _sha256(path: Path) -> str:
    """计算测试源文件指纹。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _profile_by_table(
    profiles: tuple[DataProfile, ...],
    table_id: str,
) -> DataProfile:
    """从画像集合中提取指定表。"""
    return next(profile for profile in profiles if profile.table_id == table_id)


def test_csv_profile_records_complete_statistics_and_source_sha256(
    tmp_path: Path,
):
    """CSV 画像记录原始/规范列、类型、缺失、唯一值和完整重复行。"""
    source = tmp_path / "history.csv"
    source.write_text(
        "编号 ,产量,备注\n1,10,a\n2,,b\n2,,b\n",
        encoding="utf-8",
    )

    (profile,) = inspect_data_profiles(tmp_path, TASK_OUTLINE_ID)

    assert profile.source_path == "history.csv"
    assert profile.source_sheet is None
    assert profile.source_sha256 == _sha256(source)
    assert profile.row_count == 3
    assert profile.column_count == 3
    assert profile.duplicate_row_count == 1
    assert [
        (
            column.name,
            column.source_name,
            column.raw_dtype,
            column.canonical_type,
            column.missing_count,
            column.missing_ratio,
            column.unique_count,
        )
        for column in profile.columns
    ] == [
        ("编号", "编号 ", "int64", "integer", 0, 0.0, 2),
        ("产量", "产量", "float64", "number", 2, 2 / 3, 1),
        ("备注", "备注", "object", "string", 0, 0.0, 2),
    ]

    restored = M15ArtifactStore(tmp_path).read_json(
        profile.artifact_path,
        DataProfile,
    )
    assert restored == profile


def test_csv_profile_uses_same_header_rules_as_catalog(tmp_path: Path):
    """画像全量读取与发现层都跳过 CSV 开头空行。"""
    source = tmp_path / "history.csv"
    source.write_text(
        "\n\n编号,产量\n1,10\n",
        encoding="utf-8",
    )

    (profile,) = inspect_data_profiles(tmp_path, TASK_OUTLINE_ID)

    assert profile.row_count == 1
    assert [column.name for column in profile.columns] == ["编号", "产量"]


def test_multi_sheet_profiles_exclude_templates_and_record_keys_and_joins(
    tmp_path: Path,
):
    """每个输入 Sheet 独立画像，模板排除，候选键和双向关联证据稳定。"""
    source = tmp_path / "附件.xlsx"
    _write_xlsx(
        source,
        {
            "客户": [
                ["客户编号", "客户名称"],
                [1, "甲"],
                [2, "乙"],
                [3, "丙"],
            ],
            "订单": [
                ["订单编号", "客户编号", "金额"],
                [10, 1, 20.5],
                [11, 1, 30.0],
                [12, 2, 40.5],
            ],
        },
    )
    _write_xlsx(
        tmp_path / "result1.xlsx",
        {"结果": [["客户编号"], [1]]},
    )

    profiles = inspect_data_profiles(tmp_path, TASK_OUTLINE_ID)

    assert [profile.table_id for profile in profiles] == [
        "附件.xlsx::客户",
        "附件.xlsx::订单",
    ]
    assert {profile.source_sha256 for profile in profiles} == {_sha256(source)}
    assert not any("result1.xlsx" in profile.table_id for profile in profiles)

    customers = _profile_by_table(profiles, "附件.xlsx::客户")
    orders = _profile_by_table(profiles, "附件.xlsx::订单")
    assert any(
        key.columns == ("客户编号",) and key.uniqueness_ratio == 1.0
        for key in customers.candidate_keys
    )
    assert any(
        key.columns == ("订单编号",) and key.non_null_ratio == 1.0
        for key in orders.candidate_keys
    )

    customer_join = next(
        evidence
        for evidence in customers.join_evidence
        if evidence.target_table_id == "附件.xlsx::订单"
        and evidence.source_columns == ("客户编号",)
    )
    order_join = next(
        evidence
        for evidence in orders.join_evidence
        if evidence.target_table_id == "附件.xlsx::客户"
        and evidence.source_columns == ("客户编号",)
    )
    assert customer_join.type_compatible is True
    assert customer_join.non_null_ratio == 1.0
    assert customer_join.overlap_ratio == 1.0
    assert order_join.overlap_ratio == customer_join.overlap_ratio


@pytest.mark.parametrize(
    ("filename", "content", "expected_rule"),
    [
        pytest.param(
            "broken.xlsx",
            b"not-an-xlsx",
            "profile:readable",
            id="corrupted-file",
        ),
        pytest.param(
            "empty-header.csv",
            b",value\n1,x\n",
            "profile:header-non-empty",
            id="empty-header",
        ),
        pytest.param(
            "empty.csv",
            b"",
            "profile:header-non-empty",
            id="missing-header",
        ),
        pytest.param(
            "conflict.csv",
            b"id,id \n1,2\n",
            "profile:column-name-unique",
            id="normalized-column-conflict",
        ),
    ],
)
def test_profile_failure_persists_issue_without_partial_profiles(
    tmp_path: Path,
    filename: str,
    content: bytes,
    expected_rule: str,
):
    """画像输入错误持久化为 DataIssue，且不发布任何部分画像。"""
    (tmp_path / "valid.csv").write_text("id,value\n1,ok\n", encoding="utf-8")
    (tmp_path / filename).write_bytes(content)

    with pytest.raises(DataProfileInspectionError) as captured:
        inspect_data_profiles(tmp_path, TASK_OUTLINE_ID)

    issue = captured.value.issue
    assert issue.rule_id == expected_rule
    assert issue.status == "unresolved"
    assert issue.validation_status == "validated"
    restored = M15ArtifactStore(tmp_path).read_json(issue.artifact_path, DataIssue)
    assert restored == issue
    assert not list((tmp_path / "m15").glob("profiles/*.json"))


def test_profile_rejects_supported_symlink_with_structured_issue(tmp_path: Path):
    """符号链接输入必须形成 DataIssue，不能静默发布空画像集合。"""
    source = tmp_path / "source.txt"
    source.write_text("id,value\n1,a\n", encoding="utf-8")
    (tmp_path / "linked.csv").symlink_to(source)

    with pytest.raises(DataProfileInspectionError) as captured:
        inspect_data_profiles(tmp_path, TASK_OUTLINE_ID)

    assert captured.value.issue.rule_id == "profile:readable"
    assert captured.value.issue.table_id == "linked.csv"


def test_profile_scan_does_not_change_source_bytes_or_mtime(tmp_path: Path):
    """完整 Excel 扫描不得改写原始附件内容或修改时间。"""
    source = tmp_path / "source.xlsx"
    _write_xlsx(
        source,
        {
            "数据": [
                ["编号", "日期"],
                [1, "2026-01-01"],
                [2, "2026-01-02"],
            ]
        },
    )
    before_bytes = source.read_bytes()
    before_mtime_ns = source.stat().st_mtime_ns

    profiles = inspect_data_profiles(tmp_path, TASK_OUTLINE_ID)

    assert len(profiles) == 1
    assert source.read_bytes() == before_bytes
    assert source.stat().st_mtime_ns == before_mtime_ns
    assert profiles[0].source_sha256 == hashlib.sha256(before_bytes).hexdigest()
