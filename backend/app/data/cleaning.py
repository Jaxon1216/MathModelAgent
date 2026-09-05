"""M1.5 白名单清洗的确定性执行与验证。"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

import pandas as pd

from app.data.artifact_store import (
    ArtifactStorageError,
    resolve_work_dir_path,
    write_utf8_atomic,
)
from app.data.preparation import compute_file_sha256
from app.domain.m15 import (
    CanonicalDataType,
    CleaningOperation,
    CleaningPlan,
    DataProfile,
    ValidationExpectation,
)

_CSV_ENCODINGS = ("utf-8-sig", "gb18030")
_BOOLEAN_VALUES = {
    "true": True,
    "false": False,
    "1": True,
    "0": False,
    "yes": True,
    "no": False,
    "y": True,
    "n": False,
}


@dataclass(frozen=True)
class CleanedTable:
    """一张已原子发布但尚未冻结为 DataContract 的 cleaned 表。"""

    table_id: str
    data_profile_id: str
    cleaning_plan_id: str
    relative_path: str
    sha256: str
    row_count: int
    canonical_types: tuple[tuple[str, CanonicalDataType], ...]


@dataclass(frozen=True)
class VerificationFailure:
    """可转换为 DataIssue 的单条确定性验证失败。"""

    table_id: str
    rule_id: str
    expected: Any
    actual: Any
    evidence_summary: str
    repairable: bool = True


class TableCleaningError(RuntimeError):
    """单表执行失败，携带唯一确定性失败证据。"""

    def __init__(self, failure: VerificationFailure) -> None:
        self.failure = failure
        super().__init__(
            f"清洗执行失败 [{failure.rule_id}] {failure.table_id}: "
            f"{failure.evidence_summary}"
        )


def cleaned_table_path(table_id: str) -> str:
    """返回与文件名和 Sheet 显示形式无关的稳定 cleaned 路径。"""
    digest = hashlib.sha256(table_id.encode("utf-8")).hexdigest()[:16]
    return f"cleaned/{digest}.csv"


def execute_cleaning_plan(
    work_dir: str | Path,
    profile: DataProfile,
    plan: CleaningPlan,
    *,
    source_profiles: tuple[DataProfile, ...] | None = None,
) -> CleanedTable:
    """逐表执行已校验白名单计划并原子发布 UTF-8 CSV。

    Args:
        work_dir: 当前任务工作目录。
        profile: 当前源表画像。
        plan: 当前表的严格 CleaningPlan。
        source_profiles: 本轮全部源画像；提供时前后复核全部源文件。

    Returns:
        cleaned 文件的稳定定位和指纹。

    Raises:
        TableCleaningError: 来源漂移、读取失败或某项白名单操作失败。
    """
    if profile.validation_status != "validated":
        raise ValueError("DataProfile 未通过校验，不能执行清洗")
    if plan.validation_status != "validated":
        raise ValueError("CleaningPlan 未通过校验，不能执行")
    if plan.task_outline_id != profile.task_outline_id:
        raise ValueError("CleaningPlan 与 DataProfile 不属于同一 TaskOutline")
    if plan.table_id != profile.table_id:
        raise ValueError("CleaningPlan 与 DataProfile 不属于同一表")
    if plan.data_profile_id != profile.artifact_id:
        raise ValueError("CleaningPlan 未引用当前 DataProfile")

    profiles = source_profiles or (profile,)
    _assert_source_integrity(work_dir, profiles, profile.table_id)
    try:
        frame = _read_source_frame(work_dir, profile)
    except Exception as exc:
        raise TableCleaningError(
            _failure(
                profile.table_id,
                "clean:source-readable",
                "readable source table matching DataProfile",
                f"{type(exc).__name__}: {exc}",
                f"读取源表失败: {exc}",
                repairable=False,
            )
        ) from exc

    for operation in plan.operations:
        try:
            frame = _apply_operation(frame, operation)
        except Exception as exc:
            condition = operation.postconditions[0]
            raise TableCleaningError(
                VerificationFailure(
                    table_id=profile.table_id,
                    rule_id=condition.rule_id,
                    expected=condition.expected,
                    actual=f"{type(exc).__name__}: {exc}",
                    evidence_summary=(
                        f"操作 {operation.operation_id} "
                        f"({operation.operation_type}) 执行失败: {exc}"
                    ),
                )
            ) from exc

    _assert_source_integrity(work_dir, profiles, profile.table_id)
    relative_path = cleaned_table_path(profile.table_id)
    csv_text = frame.to_csv(
        index=False,
        lineterminator="\n",
        date_format="%Y-%m-%dT%H:%M:%S",
    )
    try:
        target = write_utf8_atomic(work_dir, relative_path, csv_text)
    except ArtifactStorageError as exc:
        raise TableCleaningError(
            _failure(
                profile.table_id,
                "clean:output-write",
                "atomic UTF-8 cleaned file",
                str(exc),
                f"cleaned 文件发布失败: {exc}",
                repairable=False,
            )
        ) from exc

    try:
        _assert_source_integrity(work_dir, profiles, profile.table_id)
    except TableCleaningError:
        target.unlink(missing_ok=True)
        raise

    return CleanedTable(
        table_id=profile.table_id,
        data_profile_id=profile.artifact_id,
        cleaning_plan_id=plan.artifact_id,
        relative_path=relative_path,
        sha256=compute_file_sha256(target),
        row_count=len(frame),
        canonical_types=tuple(expected_canonical_types(profile, plan).items()),
    )


def verify_cleaned_table(
    work_dir: str | Path,
    profile: DataProfile,
    plan: CleaningPlan,
    cleaned: CleanedTable,
    *,
    profiles_by_table: dict[str, DataProfile] | None = None,
    cleaned_by_table: dict[str, CleanedTable] | None = None,
) -> tuple[VerificationFailure, ...]:
    """验证字段、类型、行数、键、缺失、重复、关联和来源完整性。"""
    failures: list[VerificationFailure] = []
    source_failure = _source_integrity_failure(work_dir, profile)
    if source_failure is not None:
        failures.append(source_failure)

    if (
        cleaned.table_id != profile.table_id
        or cleaned.data_profile_id != profile.artifact_id
        or cleaned.cleaning_plan_id != plan.artifact_id
        or cleaned.relative_path != cleaned_table_path(profile.table_id)
    ):
        failures.append(
            _failure(
                profile.table_id,
                "verify:lineage",
                {
                    "table_id": profile.table_id,
                    "profile_id": profile.artifact_id,
                    "plan_id": plan.artifact_id,
                    "path": cleaned_table_path(profile.table_id),
                },
                {
                    "table_id": cleaned.table_id,
                    "profile_id": cleaned.data_profile_id,
                    "plan_id": cleaned.cleaning_plan_id,
                    "path": cleaned.relative_path,
                },
                "cleaned 表的来源或稳定路径与当前计划不一致。",
                repairable=False,
            )
        )
        return tuple(failures)

    try:
        cleaned_path = resolve_work_dir_path(
            work_dir,
            cleaned.relative_path,
            must_exist=True,
        )
        actual_sha256 = compute_file_sha256(cleaned_path)
        if actual_sha256 != cleaned.sha256:
            failures.append(
                _failure(
                    profile.table_id,
                    "verify:cleaned-sha256",
                    cleaned.sha256,
                    actual_sha256,
                    "cleaned 文件在执行后发生变化。",
                    repairable=False,
                )
            )
        raw_frame = pd.read_csv(
            cleaned_path,
            encoding="utf-8",
            dtype="string",
            keep_default_na=True,
        )
    except Exception as exc:
        failures.append(
            _failure(
                profile.table_id,
                "verify:cleaned-readable",
                "reloadable UTF-8 CSV",
                f"{type(exc).__name__}: {exc}",
                f"cleaned 文件无法重载: {exc}",
                repairable=False,
            )
        )
        return tuple(failures)

    expected_columns = [column.name for column in profile.columns]
    actual_columns = [str(column) for column in raw_frame.columns]
    if actual_columns != expected_columns:
        failures.append(
            _failure(
                profile.table_id,
                "verify:columns",
                expected_columns,
                actual_columns,
                "cleaned 字段或字段顺序与 DataProfile 不一致。",
            )
        )
        return tuple(failures)

    expected_types = expected_canonical_types(profile, plan)
    expected_type_items = tuple(expected_types.items())
    if cleaned.canonical_types != expected_type_items:
        failures.append(
            _failure(
                profile.table_id,
                "verify:canonical-type-metadata",
                expected_type_items,
                cleaned.canonical_types,
                "cleaned 类型元数据与 DataProfile/CleaningPlan 推导结果不一致。",
                repairable=False,
            )
        )
    typed_frame = raw_frame.copy()
    type_reload_failed = False
    for column_name, expected_type in expected_types.items():
        try:
            typed_frame[column_name] = cast_series_to_canonical(
                _series(typed_frame, column_name),
                expected_type,
            )
        except Exception as exc:
            type_reload_failed = True
            failures.append(
                _failure(
                    profile.table_id,
                    validation_rule_id("verify-type", column_name),
                    expected_type,
                    f"{type(exc).__name__}: {exc}",
                    f"字段 {column_name} 无法按 {expected_type} 严格重载。",
                )
            )

    if type_reload_failed:
        return tuple(failures)

    row_count = len(typed_frame)
    if cleaned.row_count != row_count:
        failures.append(
            _failure(
                profile.table_id,
                "verify:row-count-metadata",
                row_count,
                cleaned.row_count,
                "cleaned 行数元数据与实际文件不一致。",
                repairable=False,
            )
        )
    if row_count > profile.row_count:
        failures.append(
            _failure(
                profile.table_id,
                "verify:row-count",
                {"operator": "le", "value": profile.row_count},
                row_count,
                "白名单清洗不能增加源表行数。",
            )
        )
    row_removal_declared = any(
        operation.operation_type in {"drop_empty_rows", "drop_duplicates"}
        for operation in plan.operations
    )
    if not row_removal_declared and row_count != profile.row_count:
        failures.append(
            _failure(
                profile.table_id,
                "verify:row-count-preserved",
                profile.row_count,
                row_count,
                "计划未声明删行操作，cleaned 行数必须与源表一致。",
            )
        )

    profile_column_by_name = {column.name: column for column in profile.columns}
    for column_name in expected_columns:
        actual_missing = int(_series(typed_frame, column_name).isna().sum())
        expected_max = profile_column_by_name[column_name].missing_count
        if actual_missing > expected_max:
            failures.append(
                _failure(
                    profile.table_id,
                    validation_rule_id("verify-missing", column_name),
                    {"operator": "le", "value": expected_max},
                    actual_missing,
                    f"字段 {column_name} 的缺失数不能因清洗增加。",
                )
            )

    duplicate_count = int(typed_frame.duplicated().sum())
    if duplicate_count > profile.duplicate_row_count:
        failures.append(
            _failure(
                profile.table_id,
                "verify:duplicate-count",
                {"operator": "le", "value": profile.duplicate_row_count},
                duplicate_count,
                "完整重复行数量不能因清洗增加。",
            )
        )

    for key in profile.candidate_keys:
        key_frame = typed_frame[list(key.columns)]
        if bool(key_frame.isna().to_numpy().any()):
            failures.append(
                _failure(
                    profile.table_id,
                    validation_rule_id("verify-key-non-null", *key.columns),
                    True,
                    False,
                    f"候选键 {list(key.columns)} 含缺失值。",
                )
            )
        if bool(key_frame.duplicated().any()):
            failures.append(
                _failure(
                    profile.table_id,
                    validation_rule_id("verify-key-unique", *key.columns),
                    True,
                    False,
                    f"候选键 {list(key.columns)} 不唯一。",
                )
            )

    context_profiles = profiles_by_table or {profile.table_id: profile}
    context_cleaned = cleaned_by_table or {cleaned.table_id: cleaned}
    failures.extend(
        _verify_profile_relations(
            work_dir,
            profile,
            typed_frame,
            expected_types,
            context_profiles,
            context_cleaned,
        )
    )

    for operation in plan.operations:
        for condition in operation.postconditions:
            failure = _evaluate_expectation(
                work_dir,
                profile,
                typed_frame,
                expected_types,
                condition,
                context_profiles,
                context_cleaned,
            )
            if failure is not None:
                failures.append(failure)
    return tuple(_deduplicate_failures(failures))


def _read_source_frame(work_dir: str | Path, profile: DataProfile) -> pd.DataFrame:
    """按画像阶段相同规则读取源表并恢复规范列名。"""
    path = resolve_work_dir_path(work_dir, profile.source_path, must_exist=True)
    if profile.source_sheet is not None:
        frame = pd.read_excel(path, sheet_name=profile.source_sheet)
    else:
        last_error: UnicodeDecodeError | None = None
        frame = None
        for encoding in _CSV_ENCODINGS:
            try:
                frame = pd.read_csv(path, encoding=encoding, low_memory=False)
                break
            except UnicodeDecodeError as exc:
                last_error = exc
        if frame is None:
            if last_error is not None:
                raise last_error
            raise ValueError("CSV 未能使用受支持编码读取")

    actual_source_names = tuple(str(column) for column in frame.columns)
    expected_source_names = tuple(column.source_name for column in profile.columns)
    if actual_source_names != expected_source_names:
        raise ValueError(
            "源表字段与 DataProfile 不一致: "
            f"expected={expected_source_names}, actual={actual_source_names}"
        )
    frame.columns = [column.name for column in profile.columns]
    return frame


def _apply_operation(
    frame: pd.DataFrame,
    operation: CleaningOperation,
) -> pd.DataFrame:
    """执行一项已通过 schema 的有限操作。"""
    result = frame.copy()
    columns = list(operation.target_columns)
    missing = set(columns) - set(str(column) for column in result.columns)
    if missing:
        raise ValueError(f"目标字段不存在: {sorted(missing)}")

    if operation.operation_type == "cast_type":
        assert operation.target_type is not None
        for column in columns:
            result[column] = cast_series_to_canonical(
                _series(result, column),
                operation.target_type,
            )
        return result

    if operation.operation_type == "fill_missing":
        for column in columns:
            series = _series(result, column)
            if operation.strategy == "forward_fill":
                result[column] = series.ffill()
            elif operation.strategy == "backward_fill":
                result[column] = series.bfill()
            elif operation.strategy == "constant":
                result[column] = series.fillna(cast(Any, operation.fill_value))
            elif operation.strategy == "mean":
                result[column] = series.fillna(series.mean())
            elif operation.strategy == "median":
                result[column] = series.fillna(series.median())
            else:
                mode = series.mode(dropna=True)
                if mode.empty:
                    raise ValueError(f"字段 {column} 没有可用于 mode 填充的值")
                result[column] = series.fillna(mode.iloc[0])
        return result

    if operation.operation_type == "drop_empty_rows":
        return result.dropna(axis=0, how="all", subset=columns).reset_index(drop=True)

    if operation.operation_type == "drop_duplicates":
        assert operation.keep is not None
        return result.drop_duplicates(
            subset=columns,
            keep=operation.keep,
        ).reset_index(drop=True)

    for column in columns:
        result[column] = _normalize_series(
            _series(result, column),
            operation.normalizations,
        )
    return result


def cast_series_to_canonical(
    series: pd.Series,
    target_type: CanonicalDataType,
) -> pd.Series:
    """使用固定规则进行严格且有限的类型转换。"""
    if target_type == "string":
        return series.astype("string")
    if target_type == "category":
        return series.astype("string").astype("category")
    if target_type in {"integer", "number"}:
        numeric = cast(pd.Series, pd.to_numeric(series, errors="raise"))
        if target_type == "integer":
            non_null = numeric.dropna()
            is_integral = cast(Any, non_null % 1 == 0)
            if not bool(is_integral.all()):
                raise ValueError("存在非整数值")
            return numeric.astype("Int64")
        return numeric.astype("Float64")
    if target_type == "boolean":

        def parse_boolean(value: object) -> object:
            if bool(cast(Any, pd.isna(value))):
                return pd.NA
            if isinstance(value, bool):
                return value
            normalized = unicodedata.normalize("NFKC", str(value)).strip().casefold()
            if normalized not in _BOOLEAN_VALUES:
                raise ValueError(f"不支持的布尔值: {value}")
            return _BOOLEAN_VALUES[normalized]

        return series.map(parse_boolean).astype("boolean")
    parsed = pd.to_datetime(series, errors="raise", format="mixed")
    if target_type == "date":
        return parsed.dt.normalize()
    return parsed


def _normalize_series(
    series: pd.Series,
    normalizations: tuple[str, ...],
) -> pd.Series:
    """按计划顺序规范化关联键，同时保持缺失值。"""
    normalized = series.astype("string")
    for operation in normalizations:
        if operation == "unicode_nfkc":
            normalized = normalized.map(
                lambda value: (
                    pd.NA
                    if pd.isna(value)
                    else unicodedata.normalize("NFKC", str(value))
                )
            ).astype("string")
        elif operation == "strip":
            normalized = normalized.str.strip()
        elif operation == "casefold":
            normalized = normalized.str.casefold()
        else:
            normalized = normalized.str.replace(r"\s+", " ", regex=True)
    return normalized


def expected_canonical_types(
    profile: DataProfile,
    plan: CleaningPlan,
) -> dict[str, CanonicalDataType]:
    """从画像类型和有序操作推导 cleaned 类型。"""
    result: dict[str, CanonicalDataType] = {
        column.name: column.canonical_type for column in profile.columns
    }
    for operation in plan.operations:
        if operation.operation_type == "cast_type":
            assert operation.target_type is not None
            for column in operation.target_columns:
                result[column] = operation.target_type
        elif operation.operation_type == "normalize_join_key":
            for column in operation.target_columns:
                result[column] = "string"
        elif operation.operation_type == "fill_missing" and operation.strategy in {
            "mean",
            "median",
        }:
            for column in operation.target_columns:
                result[column] = "number"
    return result


def _evaluate_expectation(
    work_dir: str | Path,
    profile: DataProfile,
    frame: pd.DataFrame,
    expected_types: dict[str, CanonicalDataType],
    condition: ValidationExpectation,
    profiles_by_table: dict[str, DataProfile],
    cleaned_by_table: dict[str, CleanedTable],
) -> VerificationFailure | None:
    """执行一条声明式后置条件。"""
    if condition.rule_type == "columns_exist":
        actual: Any = all(column in frame.columns for column in condition.columns)
    elif condition.rule_type == "canonical_type":
        actual = (
            expected_types[condition.columns[0]]
            if len(condition.columns) == 1
            else [expected_types[column] for column in condition.columns]
        )
    elif condition.rule_type == "row_count":
        actual = len(frame)
    elif condition.rule_type == "key_unique":
        actual = not bool(frame[list(condition.columns)].duplicated().any())
    elif condition.rule_type == "key_non_null":
        actual = not bool(frame[list(condition.columns)].isna().to_numpy().any())
    elif condition.rule_type == "missing_count":
        actual = int(frame[list(condition.columns)].isna().sum().sum())
    elif condition.rule_type == "duplicate_count":
        subset = list(condition.columns) or None
        actual = int(frame.duplicated(subset=subset).sum())
    elif condition.rule_type == "source_sha256":
        source_failure = _source_integrity_failure(work_dir, profile)
        actual = (
            profile.source_sha256 if source_failure is None else source_failure.actual
        )
    else:
        actual = _relation_condition_actual(
            work_dir,
            profile,
            frame,
            expected_types,
            condition,
            profiles_by_table,
            cleaned_by_table,
        )

    if _compare(actual, condition.operator, condition.expected):
        return None
    return VerificationFailure(
        table_id=profile.table_id,
        rule_id=condition.rule_id,
        expected=condition.expected,
        actual=actual,
        evidence_summary=(
            f"后置条件 {condition.rule_type} 未满足: "
            f"actual={actual!r}, operator={condition.operator}, "
            f"expected={condition.expected!r}"
        ),
        repairable=condition.rule_type != "source_sha256",
    )


def _verify_profile_relations(
    work_dir: str | Path,
    profile: DataProfile,
    frame: pd.DataFrame,
    expected_types: dict[str, CanonicalDataType],
    profiles_by_table: dict[str, DataProfile],
    cleaned_by_table: dict[str, CleanedTable],
) -> list[VerificationFailure]:
    """确认画像中已有的关联证据未被清洗破坏。"""
    failures: list[VerificationFailure] = []
    for evidence in profile.join_evidence:
        target_profile = profiles_by_table.get(evidence.target_table_id)
        target_cleaned = cleaned_by_table.get(evidence.target_table_id)
        if target_profile is None or target_cleaned is None:
            failures.append(
                _failure(
                    profile.table_id,
                    validation_rule_id(
                        "verify-join-target",
                        evidence.target_table_id,
                        *evidence.source_columns,
                    ),
                    evidence.target_table_id,
                    None,
                    "关联目标没有对应的 cleaned 表。",
                )
            )
            continue
        actual = _relation_compatible(
            work_dir,
            frame,
            expected_types,
            evidence.source_columns,
            target_profile,
            target_cleaned,
            evidence.target_columns,
            minimum_overlap=evidence.overlap_ratio,
        )
        if not actual:
            failures.append(
                _failure(
                    profile.table_id,
                    validation_rule_id(
                        "verify-join-compatible",
                        evidence.target_table_id,
                        *evidence.source_columns,
                    ),
                    True,
                    False,
                    (
                        f"与 {evidence.target_table_id} 的关联类型或取值重叠"
                        "低于画像证据。"
                    ),
                )
            )
    return failures


def _relation_condition_actual(
    work_dir: str | Path,
    profile: DataProfile,
    frame: pd.DataFrame,
    expected_types: dict[str, CanonicalDataType],
    condition: ValidationExpectation,
    profiles_by_table: dict[str, DataProfile],
    cleaned_by_table: dict[str, CleanedTable],
) -> bool:
    """求值一条显式 join_compatible 后置条件。"""
    assert condition.target_table_id is not None
    target_profile = profiles_by_table.get(condition.target_table_id)
    target_cleaned = cleaned_by_table.get(condition.target_table_id)
    if target_profile is None or target_cleaned is None:
        return False
    return _relation_compatible(
        work_dir,
        frame,
        expected_types,
        condition.columns,
        target_profile,
        target_cleaned,
        condition.target_columns,
        minimum_overlap=0.0,
    )


def _relation_compatible(
    work_dir: str | Path,
    source_frame: pd.DataFrame,
    source_types: dict[str, CanonicalDataType],
    source_columns: tuple[str, ...],
    target_profile: DataProfile,
    target_cleaned: CleanedTable,
    target_columns: tuple[str, ...],
    *,
    minimum_overlap: float,
) -> bool:
    """验证关联列类型兼容且规范值集合保持重叠。"""
    try:
        if (
            target_cleaned.table_id != target_profile.table_id
            or target_cleaned.data_profile_id != target_profile.artifact_id
            or target_cleaned.relative_path
            != cleaned_table_path(target_profile.table_id)
        ):
            return False
        path = resolve_work_dir_path(
            work_dir,
            target_cleaned.relative_path,
            must_exist=True,
        )
        if compute_file_sha256(path) != target_cleaned.sha256:
            return False
        target_frame = pd.read_csv(
            path,
            encoding="utf-8",
            dtype="string",
            keep_default_na=True,
        )
        target_types = dict(target_cleaned.canonical_types)
        for source_column, target_column in zip(
            source_columns,
            target_columns,
            strict=True,
        ):
            if not _types_compatible(
                source_types[source_column],
                target_types[target_column],
            ):
                return False
            target_series = cast_series_to_canonical(
                _series(target_frame, target_column),
                target_types[target_column],
            )
            source_values = _join_values(
                _series(source_frame, source_column),
                source_types[source_column],
            )
            target_values = _join_values(
                target_series,
                target_types[target_column],
            )
            if not source_values or not target_values:
                return False
            overlap = len(source_values & target_values) / min(
                len(source_values),
                len(target_values),
            )
            if overlap == 0.0 or overlap + 1e-12 < minimum_overlap:
                return False
        return True
    except (ArtifactStorageError, KeyError, OSError, ValueError):
        return False


def _assert_source_integrity(
    work_dir: str | Path,
    profiles: tuple[DataProfile, ...],
    current_table_id: str,
) -> None:
    """复核所有唯一源文件指纹，失败时立即阻断。"""
    seen: set[str] = set()
    for profile in profiles:
        if profile.source_path in seen:
            continue
        seen.add(profile.source_path)
        failure = _source_integrity_failure(work_dir, profile)
        if failure is not None:
            raise TableCleaningError(
                VerificationFailure(
                    table_id=current_table_id,
                    rule_id=failure.rule_id,
                    expected=failure.expected,
                    actual=failure.actual,
                    evidence_summary=(
                        f"源文件 {profile.source_path} 的指纹与画像不一致。"
                    ),
                    repairable=False,
                )
            )


def _source_integrity_failure(
    work_dir: str | Path,
    profile: DataProfile,
) -> VerificationFailure | None:
    """返回源文件漂移证据，不将读取异常伪装成相同指纹。"""
    try:
        path = resolve_work_dir_path(
            work_dir,
            profile.source_path,
            must_exist=True,
        )
        actual = compute_file_sha256(path)
    except (ArtifactStorageError, OSError) as exc:
        actual = f"{type(exc).__name__}: {exc}"
    if actual == profile.source_sha256:
        return None
    return _failure(
        profile.table_id,
        "source_modified",
        profile.source_sha256,
        actual,
        "源文件 SHA-256 与 DataProfile 不一致。",
        repairable=False,
    )


def _compare(actual: Any, operator: str, expected: Any) -> bool:
    """执行有限比较运算，不解释任意表达式。"""
    if operator == "eq":
        return bool(actual == expected)
    if operator == "le":
        return bool(actual <= expected)
    return bool(actual >= expected)


def _types_compatible(
    source_type: CanonicalDataType,
    target_type: CanonicalDataType,
) -> bool:
    """定义关联字段的窄类型兼容关系。"""
    if source_type == target_type:
        return True
    return any(
        source_type in family and target_type in family
        for family in (
            {"integer", "number"},
            {"string", "category"},
            {"date", "datetime"},
        )
    )


def _join_values(
    series: pd.Series,
    canonical_type: CanonicalDataType,
) -> frozenset[str]:
    """将关联值规范化为稳定集合。"""
    values: set[str] = set()
    for value in series.dropna().tolist():
        if canonical_type in {"integer", "number"}:
            try:
                values.add(format(Decimal(str(value)).normalize(), "f"))
            except InvalidOperation:
                values.add(str(value))
        elif canonical_type in {"date", "datetime"}:
            timestamp = pd.Timestamp(value)
            values.add(str(cast(Any, timestamp).isoformat()))
        elif canonical_type == "boolean":
            values.add("true" if bool(value) else "false")
        else:
            values.add(unicodedata.normalize("NFKC", str(value)).strip().casefold())
    return frozenset(values)


def _failure(
    table_id: str,
    rule_id: str,
    expected: Any,
    actual: Any,
    evidence_summary: str,
    *,
    repairable: bool = True,
) -> VerificationFailure:
    """构造验证失败。"""
    return VerificationFailure(
        table_id=table_id,
        rule_id=rule_id,
        expected=expected,
        actual=actual,
        evidence_summary=evidence_summary,
        repairable=repairable,
    )


def validation_rule_id(prefix: str, *parts: str) -> str:
    """为含任意 Unicode 表名/列名的内部规则生成合法稳定标识。"""
    identity = "\0".join((prefix, *parts)).encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:16]
    normalized_prefix = re.sub(r"[^a-z0-9]+", "-", prefix.casefold()).strip("-")
    return f"rule:{normalized_prefix}-{digest}"


def _series(frame: pd.DataFrame, column_name: str) -> pd.Series:
    """读取唯一列并为 pandas 返回值收窄类型。"""
    result = frame[column_name]
    if isinstance(result, pd.DataFrame):
        raise ValueError(f"字段不唯一: {column_name}")
    return result


def _deduplicate_failures(
    failures: list[VerificationFailure],
) -> list[VerificationFailure]:
    """同一规则只保留第一条确定性证据。"""
    result: list[VerificationFailure] = []
    seen: set[str] = set()
    for failure in failures:
        if failure.rule_id in seen:
            continue
        seen.add(failure.rule_id)
        result.append(failure)
    return result
