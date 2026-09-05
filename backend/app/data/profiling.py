"""只读构造并持久化逐表 M1.5 DataProfile。"""

from __future__ import annotations

import hashlib
import itertools
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

import pandas as pd
from pandas.api import types as pandas_types

from app.data.artifact_store import M15ArtifactStore
from app.data.preparation import (
    DataCatalogInspectionError,
    compute_file_sha256,
    inspect_data_catalog,
)
from app.domain.m15 import (
    CandidateKey,
    CanonicalDataType,
    DataIssue,
    DataProfile,
    JoinEvidence,
    ProfileColumn,
)
from app.domain.problem import DataColumn, DataTable

_CSV_ENCODINGS = ("utf-8-sig", "gb18030")
_MAX_CANDIDATE_KEYS = 8
_MAX_SINGLE_KEY_COLUMNS = 64
_MAX_COMPOSITE_KEY_COLUMNS = 12
_MAX_COMPOSITE_KEY_CHECKS = 32
_MAX_JOIN_COLUMNS_PER_TABLE = 32
_MAX_JOIN_PAIRS_PER_TABLE_PAIR = 16
_MAX_JOIN_DISTINCT_VALUES = 10_000
_KEY_NAME_MARKERS = ("id", "编号", "编码", "代码", "序号", "名称", "name", "key")


class DataProfileInspectionError(RuntimeError):
    """画像阶段失败，且对应 DataIssue 已持久化。"""

    def __init__(self, issue: DataIssue) -> None:
        self.issue = issue
        super().__init__(
            f"数据画像失败 [{issue.rule_id}] {issue.table_id}: {issue.evidence_summary}"
        )


@dataclass
class _ProfileDraft:
    """完成单表统计、等待补充跨表关联证据的内存结果。"""

    table: DataTable
    source_sha256: str
    frame: pd.DataFrame
    columns: tuple[ProfileColumn, ...]
    candidate_keys: tuple[CandidateKey, ...]


def inspect_data_profiles(
    work_dir: str | Path,
    task_outline_id: str,
    *,
    artifact_store: M15ArtifactStore | None = None,
) -> tuple[DataProfile, ...]:
    """只读扫描全部输入表，并在全部成功后发布逐表画像。

    发现、加载或源文件完整性检查出现任一失败时，只持久化结构化
    `DataIssue` 并抛出异常，不返回或写入任何部分 `DataProfile`。

    Args:
        work_dir: 当前任务工作目录。
        task_outline_id: 画像直接依赖的 TaskOutline 产物标识。
        artifact_store: 可选的同工作目录产物存储，主要用于依赖注入测试。

    Returns:
        按现有 DataCatalog 顺序排列、且已经持久化的 DataProfile。

    Raises:
        DataProfileInspectionError: 任一输入无法形成完整可靠画像。
    """
    root = Path(work_dir)
    store = artifact_store or M15ArtifactStore(root)

    try:
        catalog = inspect_data_catalog(root)
    except DataCatalogInspectionError as exc:
        issue = _catalog_issue(task_outline_id, exc)
        store.write_json(issue)
        raise DataProfileInspectionError(issue) from exc

    input_files = tuple(sorted({table.file for table in catalog.input_tables}))
    initial_hashes: dict[str, str] = {}
    current_table_id = "data-profile-input"
    try:
        for filename in input_files:
            initial_hashes[filename] = compute_file_sha256(root / filename)
        draft_list: list[_ProfileDraft] = []
        for table in catalog.input_tables:
            current_table_id = table.table_id
            draft_list.append(
                _build_profile_draft(
                    root,
                    table,
                    initial_hashes[table.file],
                )
            )
        drafts = tuple(draft_list)
    except Exception as exc:
        issue = _profile_issue(
            task_outline_id=task_outline_id,
            table_id=current_table_id,
            rule_id="profile:readable",
            expected="readable complete table",
            actual=str(exc),
            evidence_summary=f"完整读取或统计输入表失败: {exc}",
        )
        store.write_json(issue)
        raise DataProfileInspectionError(issue) from exc

    for filename, expected_sha256 in initial_hashes.items():
        try:
            actual_sha256 = compute_file_sha256(root / filename)
        except OSError as exc:
            actual_sha256 = f"unreadable: {exc}"
        if actual_sha256 != expected_sha256:
            issue = _profile_issue(
                task_outline_id=task_outline_id,
                table_id=filename,
                rule_id="profile:source-sha256",
                expected=expected_sha256,
                actual=actual_sha256,
                evidence_summary="画像扫描期间源文件发生变化或变得不可读取。",
            )
            store.write_json(issue)
            raise DataProfileInspectionError(issue)

    try:
        joins_by_table = _build_join_evidence(drafts)
        profiles = tuple(
            _build_data_profile(
                draft,
                task_outline_id,
                joins_by_table[draft.table.table_id],
            )
            for draft in drafts
        )
    except Exception as exc:
        issue = _profile_issue(
            task_outline_id=task_outline_id,
            table_id="data-profile-set",
            rule_id="profile:complete",
            expected="all table profiles satisfy the M1.5 schema",
            actual=str(exc),
            evidence_summary=f"关联证据或画像契约构造失败: {exc}",
        )
        store.write_json(issue)
        raise DataProfileInspectionError(issue) from exc

    for profile in profiles:
        store.write_json(profile)
    return profiles


def _build_profile_draft(
    root: Path,
    table: DataTable,
    source_sha256: str,
) -> _ProfileDraft:
    """完整读取一张已发现表并计算列级与表级统计。"""
    path = root / table.file
    frame = _read_table(path, table.sheet)
    actual_source_names = tuple(str(column) for column in frame.columns)
    expected_source_names = tuple(column.source_name for column in table.columns)
    if actual_source_names != expected_source_names:
        raise ValueError(
            "完整读取后的表头与发现结果不一致: "
            f"expected={expected_source_names}, actual={actual_source_names}"
        )

    normalized_names = [column.name for column in table.columns]
    frame.columns = normalized_names
    row_count = len(frame)
    columns = tuple(
        _profile_column(_get_series(frame, column.name), column, row_count)
        for column in table.columns
    )
    return _ProfileDraft(
        table=table,
        source_sha256=source_sha256,
        frame=frame,
        columns=columns,
        candidate_keys=_candidate_keys(frame, columns),
    )


def _read_table(path: Path, sheet_name: str | None) -> pd.DataFrame:
    """按发现层相同的格式与编码规则读取完整表。"""
    if sheet_name is not None:
        return pd.read_excel(path, sheet_name=sheet_name)

    last_error: UnicodeDecodeError | None = None
    for encoding in _CSV_ENCODINGS:
        try:
            return pd.read_csv(path, encoding=encoding, low_memory=False)
        except UnicodeDecodeError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise ValueError("CSV 未能使用受支持编码读取")


def _profile_column(
    series: pd.Series,
    column: DataColumn,
    row_count: int,
) -> ProfileColumn:
    """计算单列完整统计并映射为规范类型。"""
    missing_count = int(series.isna().sum())
    return ProfileColumn(
        name=column.name,
        source_name=column.source_name,
        raw_dtype=str(series.dtype),
        canonical_type=_canonical_type(series),
        missing_count=missing_count,
        missing_ratio=missing_count / row_count if row_count else 0.0,
        unique_count=int(series.nunique(dropna=True)),
    )


def _canonical_type(series: pd.Series) -> CanonicalDataType:
    """以固定 pandas dtype/value 规则映射规范类型。"""
    dtype = series.dtype
    if pandas_types.is_bool_dtype(dtype):
        return "boolean"
    if pandas_types.is_integer_dtype(dtype):
        return "integer"
    if pandas_types.is_numeric_dtype(dtype):
        return "number"
    if pandas_types.is_datetime64_any_dtype(dtype):
        non_null = series.dropna()
        if non_null.empty or all(
            value.hour == value.minute == value.second == value.microsecond == 0
            for value in non_null
        ):
            return "date"
        return "datetime"
    if isinstance(dtype, pd.CategoricalDtype):
        return "category"

    inferred = pandas_types.infer_dtype(series.dropna(), skipna=True)
    if inferred == "date":
        return "date"
    if inferred in {"datetime", "datetime64"}:
        return "datetime"
    return "string"


def _candidate_keys(
    frame: pd.DataFrame,
    columns: tuple[ProfileColumn, ...],
) -> tuple[CandidateKey, ...]:
    """按固定上限识别严格非空且唯一的单列/双列候选键。"""
    row_count = len(frame)
    if row_count == 0:
        return ()

    column_order = {column.name: index for index, column in enumerate(columns)}
    ordered_columns = sorted(
        columns,
        key=lambda column: (
            not any(marker in column.name.casefold() for marker in _KEY_NAME_MARKERS),
            column_order[column.name],
        ),
    )
    candidates: list[CandidateKey] = []
    single_key_names: set[str] = set()
    for column in ordered_columns[:_MAX_SINGLE_KEY_COLUMNS]:
        non_null_count = row_count - column.missing_count
        if non_null_count == row_count and column.unique_count == row_count:
            candidates.append(
                CandidateKey(
                    columns=(column.name,),
                    uniqueness_ratio=1.0,
                    non_null_ratio=1.0,
                )
            )
            single_key_names.add(column.name)
            if len(candidates) == _MAX_CANDIDATE_KEYS:
                return tuple(candidates)

    composite_columns = [
        column.name for column in ordered_columns if column.name not in single_key_names
    ][:_MAX_COMPOSITE_KEY_COLUMNS]
    checks = 0
    for first, second in itertools.combinations(composite_columns, 2):
        if checks == _MAX_COMPOSITE_KEY_CHECKS:
            break
        checks += 1
        values = frame[[first, second]]
        non_null_rows = int(values.dropna().shape[0])
        if non_null_rows != row_count:
            continue
        unique_rows = int(values.drop_duplicates().shape[0])
        if unique_rows == row_count:
            candidates.append(
                CandidateKey(
                    columns=(first, second),
                    uniqueness_ratio=1.0,
                    non_null_ratio=1.0,
                )
            )
            if len(candidates) == _MAX_CANDIDATE_KEYS:
                break
    return tuple(candidates)


def _build_join_evidence(
    drafts: tuple[_ProfileDraft, ...],
) -> dict[str, tuple[JoinEvidence, ...]]:
    """按列名、类型、非空率和有界精确值集合构造双向关联证据。"""
    evidence: dict[str, list[JoinEvidence]] = {
        draft.table.table_id: [] for draft in drafts
    }
    for source_index, source in enumerate(drafts):
        for target in drafts[source_index + 1 :]:
            matches = 0
            for source_column in source.columns[:_MAX_JOIN_COLUMNS_PER_TABLE]:
                for target_column in target.columns[:_MAX_JOIN_COLUMNS_PER_TABLE]:
                    if matches == _MAX_JOIN_PAIRS_PER_TABLE_PAIR:
                        break
                    if _normalized_join_name(
                        source_column.name
                    ) != _normalized_join_name(target_column.name):
                        continue
                    if not _types_compatible(
                        source_column.canonical_type,
                        target_column.canonical_type,
                    ):
                        continue
                    source_values = _bounded_join_values(
                        _get_series(source.frame, source_column.name),
                        source_column,
                    )
                    target_values = _bounded_join_values(
                        _get_series(target.frame, target_column.name),
                        target_column,
                    )
                    if not source_values or not target_values:
                        continue
                    overlap = len(source_values & target_values) / min(
                        len(source_values),
                        len(target_values),
                    )
                    if overlap == 0.0:
                        continue
                    non_null_ratio = min(
                        1.0 - source_column.missing_ratio,
                        1.0 - target_column.missing_ratio,
                    )
                    evidence[source.table.table_id].append(
                        JoinEvidence(
                            target_table_id=target.table.table_id,
                            source_columns=(source_column.name,),
                            target_columns=(target_column.name,),
                            type_compatible=True,
                            non_null_ratio=non_null_ratio,
                            overlap_ratio=overlap,
                        )
                    )
                    evidence[target.table.table_id].append(
                        JoinEvidence(
                            target_table_id=source.table.table_id,
                            source_columns=(target_column.name,),
                            target_columns=(source_column.name,),
                            type_compatible=True,
                            non_null_ratio=non_null_ratio,
                            overlap_ratio=overlap,
                        )
                    )
                    matches += 1
                if matches == _MAX_JOIN_PAIRS_PER_TABLE_PAIR:
                    break

    return {
        table_id: tuple(
            sorted(
                items,
                key=lambda item: (
                    item.target_table_id,
                    item.source_columns,
                    item.target_columns,
                ),
            )
        )
        for table_id, items in evidence.items()
    }


def _bounded_join_values(
    series: pd.Series,
    column: ProfileColumn,
) -> frozenset[str]:
    """仅对基数不超过固定阈值的列构造精确规范值集合。"""
    if column.unique_count > _MAX_JOIN_DISTINCT_VALUES:
        return frozenset()
    return frozenset(
        _join_value(value, column.canonical_type) for value in series.dropna().tolist()
    )


def _join_value(value: object, canonical_type: CanonicalDataType) -> str:
    """将关联比较值稳定规范化，不修改 DataFrame。"""
    if canonical_type in {"integer", "number"}:
        try:
            return format(Decimal(str(value)).normalize(), "f")
        except InvalidOperation:
            return str(value)
    if canonical_type in {"date", "datetime"}:
        return str(pd.Timestamp(cast(Any, value)))
    if canonical_type == "boolean":
        return "true" if bool(value) else "false"
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()


def _types_compatible(
    source_type: CanonicalDataType,
    target_type: CanonicalDataType,
) -> bool:
    """定义关联字段允许的窄类型兼容关系。"""
    if source_type == target_type:
        return True
    compatible_families = (
        {"integer", "number"},
        {"string", "category"},
        {"date", "datetime"},
    )
    return any(
        source_type in family and target_type in family
        for family in compatible_families
    )


def _normalized_join_name(name: str) -> str:
    """折叠大小写、Unicode 和空白，作为列名匹配证据。"""
    normalized = unicodedata.normalize("NFKC", name).casefold()
    return "".join(normalized.split())


def _build_data_profile(
    draft: _ProfileDraft,
    task_outline_id: str,
    join_evidence: tuple[JoinEvidence, ...],
) -> DataProfile:
    """将已完成的统计与关联证据封装为严格领域产物。"""
    digest = hashlib.sha256(draft.table.table_id.encode("utf-8")).hexdigest()[:16]
    return DataProfile(
        schema_version="m1.5",
        artifact_id=f"data-profile:{digest}",
        source_artifact_ids=(task_outline_id,),
        validation_status="validated",
        artifact_path=f"m15/profiles/{digest}.json",
        task_outline_id=task_outline_id,
        table_id=draft.table.table_id,
        source_path=draft.table.file,
        source_sheet=draft.table.sheet,
        source_sha256=draft.source_sha256,
        row_count=len(draft.frame),
        column_count=len(draft.columns),
        columns=draft.columns,
        duplicate_row_count=int(draft.frame.duplicated().sum()),
        candidate_keys=draft.candidate_keys,
        join_evidence=join_evidence,
    )


def _catalog_issue(
    task_outline_id: str,
    error: DataCatalogInspectionError,
) -> DataIssue:
    """将发现层错误转换为可持久化画像问题。"""
    rule_by_kind = {
        "read_failure": "profile:readable",
        "empty_header": "profile:header-non-empty",
        "column_conflict": "profile:column-name-unique",
    }
    expected_by_kind = {
        "read_failure": "readable supported data table",
        "empty_header": "all source headers are non-empty",
        "column_conflict": "normalized column names are unique",
    }
    return _profile_issue(
        task_outline_id=task_outline_id,
        table_id=error.table_id,
        rule_id=rule_by_kind[error.failure_kind],
        expected=expected_by_kind[error.failure_kind],
        actual=error.reason,
        evidence_summary=error.reason,
    )


def _profile_issue(
    *,
    task_outline_id: str,
    table_id: str,
    rule_id: str,
    expected: str,
    actual: str,
    evidence_summary: str,
) -> DataIssue:
    """构造稳定标识的未解决画像 DataIssue。"""
    identity = f"{rule_id}\0{table_id}".encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:16]
    issue_id = f"data-issue:{digest}"
    return DataIssue(
        schema_version="m1.5",
        artifact_id=issue_id,
        source_artifact_ids=(task_outline_id,),
        validation_status="validated",
        artifact_path=f"m15/issues/{digest}.json",
        issue_id=issue_id,
        table_id=table_id,
        rule_id=rule_id,
        expected=expected,
        actual=actual,
        evidence_summary=evidence_summary,
        repair_attempt=0,
        status="unresolved",
    )


def _get_series(frame: pd.DataFrame, column_name: str) -> pd.Series:
    """读取已验证唯一的规范列，并为类型检查器收窄 pandas 返回值。"""
    values = frame[column_name]
    if isinstance(values, pd.DataFrame):
        raise ValueError(f"规范列名不唯一: {column_name}")
    return values
