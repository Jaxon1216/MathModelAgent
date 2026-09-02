"""源数据探查契约与限长渲染。

本模块只负责读取原始 csv/xls/xlsx 并返回可序列化的探查结构，不写入任务目录。
调用方可以将返回值落盘为 ``source_inspection.json``，或直接渲染为有限长度的
数据准备材料。所有排序、抽样和截断规则都是确定性的。
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from app.utils.data_contract import is_result_template

INSPECTION_VERSION = 1
INSPECTION_FILENAME = "source_inspection.json"
SUPPORTED_SUFFIXES = (".csv", ".xls", ".xlsx")
DEFAULT_RENDER_BUDGET = 12_000
DEFAULT_CHUNK_SIZE = 10_000
DEFAULT_SAMPLE_ROWS = 3
DEFAULT_HEADER_CANDIDATE_ROWS = 5
DEFAULT_MAX_CATEGORY_VALUES = 20
MAX_SEEN_ROWS = 100_000
MAX_UNIQUE_VALUES = 10_000
MAX_SCAN_ROWS = 1_000_000
MAX_CHUNK_SIZE = 100_000
MAX_SAMPLE_ROWS = 100
MAX_RENDER_BUDGET = 100_000

_INTEGER_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(?:(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?)$")
_DATE_RE = re.compile(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}")
_INSPECTION_ECHO_MARKERS = (
    "source_inspection.json",
    "source_inspection",
    "inspection-entry",
    "source inspection",
    "源数据探查",
    "探查报告",
)
_INSPECTION_FACT_PATTERNS = (
    re.compile(
        r"(?:cleaned_path|cleaned/|results?/|figures?/)\s*[:=：]?\s*"
        r"[^\s,;，；)\]}]+",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:rows?|columns?|cols?|行数|列数)\s*[:=：]\s*[-+]?\d+",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:accuracy|precision|recall|f1|rmse|mae|mse|r2|auc|score|loss|"
        r"指标|结果|结论|警告|限制|清洗动作|cleaning action)\s*[:=：]\s*"
        r"[^;\n,，；]+",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:readable sheet coverage|coverage|execution_error|error|"
        r"缺少|失败原因|失败|未完成)\s*[:=：]?\s*[^;\n,，；]+",
        re.IGNORECASE,
    ),
)


def _bounded_nonnegative(value: int, maximum: int) -> int:
    """将可配置的非负整数限制在固定范围内。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"参数必须是整数: {value}") from exc
    return min(max(parsed, 0), maximum)


def redact_source_inspection_echoes(
    text: Any,
    inspection_text: str | None = None,
) -> str:
    """删除探查材料回声，同时保留合法清洗/求解事实。

    ``inspection_text`` 只作为内存中的 exact-match 比较基准，不会进入返回值。
    探查 marker 所在的混合行仅提取 cleaned 路径、规模、指标、结论、警告和
    限制，避免 Coder 的收工输出把完整 source inspection 复制到后续交接。

    Args:
        text: 待过滤的工具输出、摘要或交接材料。
        inspection_text: 本阶段唯一注入的 bounded inspection body。

    Returns:
        过滤后的文本。
    """
    safe_text = str(text or "")
    exact_body = str(inspection_text or "").strip()
    if exact_body:
        safe_text = safe_text.replace(exact_body, "")

    filtered: list[str] = []
    for raw_line in safe_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.casefold()
        if any(marker.casefold() in lowered for marker in _INSPECTION_ECHO_MARKERS):
            facts: list[str] = []
            for pattern in _INSPECTION_FACT_PATTERNS:
                for match in pattern.finditer(line):
                    fact = match.group(0).strip(" \t,;，；")
                    if fact and fact not in facts:
                        facts.append(fact)
            if facts:
                filtered.append("；".join(facts))
            continue
        filtered.append(line)
    return "\n".join(filtered)


def _bounded_positive(value: int, maximum: int) -> int:
    """将可配置的正整数限制在固定范围内。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"参数必须是整数: {value}") from exc
    return min(max(parsed, 1), maximum)


def _bounded_max_rows(value: int | None) -> int | None:
    """限制扫描行数；``None`` 仍表示由调用方选择完整扫描。"""
    if value is None:
        return None
    return _bounded_nonnegative(value, MAX_SCAN_ROWS)


def _is_missing(value: Any) -> bool:
    """判断单元格是否为空，额外把纯空白字符串视为空。"""
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    try:
        result = pd.isna(value)
        return bool(result) if not hasattr(result, "__len__") else False
    except (TypeError, ValueError):
        return False


def _json_value(value: Any) -> Any:
    """把 pandas/numpy 单元格转换成稳定的 JSON 值。"""
    if _is_missing(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (AttributeError, ValueError):
            pass
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _display_value(value: Any) -> str:
    """返回用于探查文本和唯一值统计的稳定字符串。"""
    normalized = _json_value(value)
    return "" if normalized is None else str(normalized)


def _value_kind(value: Any) -> str:
    """推断单个非空单元格的原始语义类型。"""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, pd.Timestamp):
        return "datetime"
    if isinstance(value, int) and not isinstance(value, bool):
        return "int"
    if isinstance(value, float):
        return "float"

    text = str(value).strip()
    lowered = text.lower()
    if lowered in {"true", "false", "yes", "no"}:
        return "bool"
    if _INTEGER_RE.fullmatch(text):
        return "int"
    if _FLOAT_RE.fullmatch(text):
        return "float"
    if _DATE_RE.match(text):
        try:
            pd.to_datetime(text)
        except (TypeError, ValueError):
            pass
        else:
            return "datetime"
    return "string"


def _merge_value_kinds(kinds: set[str]) -> str:
    """把一列观察到的单元格类型合并为一类。"""
    if not kinds:
        return "empty"
    if kinds <= {"int", "float"}:
        return "float" if "float" in kinds else "int"
    if len(kinds) == 1:
        return next(iter(kinds))
    return "mixed"


def _raw_text(value: Any) -> str:
    """把表头候选中的单元格转成短文本。"""
    return _display_value(value).strip()


def _non_empty_count(row: Iterable[Any]) -> int:
    """统计一行中的非空单元格数量。"""
    return sum(not _is_missing(value) for value in row)


def _choose_header_row(rows: list[list[Any]]) -> int:
    """从有限预览中选择表头候选，标题行稀疏时向后寻找表头。"""
    if not rows:
        return 0
    counts = [_non_empty_count(row) for row in rows]
    first_count = counts[0]
    best_row = max(range(len(rows)), key=lambda index: (counts[index], -index))
    if best_row > 0 and first_count <= 1:
        return best_row
    return 0


def _header_candidates(rows: list[list[Any]]) -> list[dict[str, Any]]:
    """生成有限的表头候选结构。"""
    candidates: list[dict[str, Any]] = []
    for index, row in enumerate(rows[:DEFAULT_HEADER_CANDIDATE_ROWS]):
        values = [_json_value(value) for value in row[:100]]
        candidates.append(
            {
                "row": index,
                "non_empty": _non_empty_count(row),
                "values": values,
            }
        )
    return candidates


def _duplicate_headers(headers: list[Any]) -> tuple[list[str], list[str]]:
    """返回重复表头和空表头，保持首次出现顺序。"""
    counts: dict[str, int] = {}
    for header in headers:
        name = _raw_text(header)
        counts[name] = counts.get(name, 0) + 1
    duplicates = [name for name, count in counts.items() if name and count > 1]
    empty = [name for name, count in counts.items() if not name and count > 0]
    return duplicates, empty


def _profile_frames(
    frames: Iterable[pd.DataFrame],
    *,
    raw_headers: list[Any],
    raw_rows: list[list[Any]],
    header_row: int,
    scan_complete: bool,
    chunked: bool,
    read_method: str,
    sample_rows: int = DEFAULT_SAMPLE_ROWS,
    encoding_warning: str | None = None,
) -> dict[str, Any]:
    """从一个或多个 DataFrame 分块汇总 sheet 画像。"""
    stats: list[dict[str, Any]] = []
    column_names: list[str] = []
    row_count = 0
    blank_rows = 0
    duplicate_rows = 0
    seen_rows: set[tuple[str, ...]] = set()
    seen_rows_capped = False
    samples: list[list[Any]] = []
    sample_limit = _bounded_nonnegative(sample_rows, MAX_SAMPLE_ROWS)

    for frame in frames:
        if not column_names:
            column_names = [str(column) for column in frame.columns.tolist()]
            stats = [
                {
                    "name": name,
                    "non_empty": 0,
                    "missing": 0,
                    "unique_values": set(),
                    "unique_values_capped": False,
                    "kinds": set(),
                }
                for name in column_names
            ]

        for row in frame.itertuples(index=False, name=None):
            row_count += 1
            normalized_row = tuple(_display_value(value) for value in row)
            if not seen_rows_capped:
                if normalized_row in seen_rows:
                    duplicate_rows += 1
                elif len(seen_rows) < MAX_SEEN_ROWS:
                    seen_rows.add(normalized_row)
                else:
                    seen_rows_capped = True
            else:
                # 集合已经达到上限，继续扫描但不再声称重复行统计精确。
                pass
            if all(_is_missing(value) for value in row):
                blank_rows += 1
            if len(samples) < sample_limit:
                samples.append([_json_value(value) for value in row])

            for index, value in enumerate(row):
                if index >= len(stats):
                    continue
                column = stats[index]
                if _is_missing(value):
                    column["missing"] += 1
                    continue
                column["non_empty"] += 1
                value_text = _display_value(value)
                if not column["unique_values_capped"]:
                    if value_text not in column["unique_values"]:
                        if len(column["unique_values"]) < MAX_UNIQUE_VALUES:
                            column["unique_values"].add(value_text)
                        else:
                            column["unique_values_capped"] = True
                column["kinds"].add(_value_kind(value))

    duplicate_columns, empty_headers = _duplicate_headers(
        raw_headers if raw_headers else column_names
    )
    warnings: list[str] = []
    columns: list[dict[str, Any]] = []
    for column in stats:
        unique_values = sorted(column["unique_values"])
        columns.append(
            {
                "name": column["name"],
                "dtype": _merge_value_kinds(column["kinds"]),
                "non_empty": column["non_empty"],
                "non_empty_count": column["non_empty"],
                "missing": column["missing"],
                "missing_count": column["missing"],
                "missing_ratio": round(column["missing"] / row_count, 4)
                if row_count
                else 0.0,
                "unique": (
                    None
                    if column["unique_values_capped"]
                    else len(column["unique_values"])
                ),
                "unique_count": (
                    None
                    if column["unique_values_capped"]
                    else len(column["unique_values"])
                ),
                "values": (
                    None
                    if column["unique_values_capped"]
                    else unique_values[:DEFAULT_MAX_CATEGORY_VALUES]
                ),
            }
        )
        if column["unique_values_capped"]:
            warnings.append(
                f"unique_values_capped: {column['name']} limit={MAX_UNIQUE_VALUES}"
            )

    if encoding_warning:
        warnings.append(encoding_warning)
    if header_row > 0:
        warnings.append(f"header_offset: row {header_row}")
    if duplicate_columns:
        warnings.append("duplicate_column_names: " + ", ".join(duplicate_columns))
    if empty_headers:
        warnings.append("empty_header")
    if len(raw_rows) > 1 and _non_empty_count(raw_rows[0]) < _non_empty_count(
        raw_rows[1]
    ):
        warnings.append("possible_multirow_header")
    if blank_rows:
        warnings.append(f"blank_rows: {blank_rows}")
    blank_columns = [column["name"] for column in columns if column["non_empty"] == 0]
    if blank_columns:
        warnings.append("blank_columns: " + ", ".join(blank_columns))
    if duplicate_rows:
        if not seen_rows_capped:
            warnings.append(f"duplicate_rows: {duplicate_rows}")
    if seen_rows_capped:
        duplicate_rows = None
        warnings.append(f"seen_rows_capped: limit={MAX_SEEN_ROWS}")
        warnings.append("duplicate_rows_unavailable: seen_rows capped")
    mixed_columns = [column["name"] for column in columns if column["dtype"] == "mixed"]
    if mixed_columns:
        warnings.append("mixed_types: " + ", ".join(mixed_columns))
    if not scan_complete:
        warnings.append("scan_incomplete: statistics cover only the scanned range")
    if row_count == 0 and not columns:
        warnings.append("empty_table")

    return {
        "status": "readable",
        "readable": True,
        "rows": row_count,
        "columns": len(columns),
        "column_count": len(columns),
        "column_names": column_names,
        "header_row": header_row,
        "header_candidates": _header_candidates(raw_rows),
        "duplicate_columns": duplicate_columns,
        "columns_meta": columns,
        "sample_rows": samples,
        "warnings": warnings,
        "duplicate_rows": duplicate_rows,
        "read_method": read_method,
        "scan": {
            "mode": "full" if scan_complete else "bounded",
            "rows_scanned": row_count,
            "complete": scan_complete,
            "chunked": chunked,
        },
    }


def _empty_sheet_profile(
    *,
    raw_rows: list[list[Any]],
    read_method: str,
    warning: str = "empty_table",
) -> dict[str, Any]:
    """构造没有可读数据行的 sheet 画像。"""
    profile = _profile_frames(
        [],
        raw_headers=raw_rows[0] if raw_rows else [],
        raw_rows=raw_rows,
        header_row=_choose_header_row(raw_rows),
        scan_complete=True,
        chunked=False,
        read_method=read_method,
    )
    if warning not in profile["warnings"]:
        profile["warnings"].append(warning)
    return profile


def _read_csv_raw_rows(path: Path, encoding: str) -> list[list[str]]:
    """读取 CSV 前几行，仅用于表头候选和风险判断。"""
    rows: list[list[str]] = []
    with path.open("r", encoding=encoding, newline="") as stream:
        reader = csv.reader(stream)
        for row in reader:
            rows.append(row)
            if len(rows) >= DEFAULT_HEADER_CANDIDATE_ROWS:
                break
    return rows


def _csv_sheet_profile(
    path: Path,
    *,
    max_rows: int | None,
    chunk_size: int,
) -> dict[str, Any]:
    """探查 CSV，并把编码回退和有限扫描写入画像。"""
    chunk_size = _bounded_positive(chunk_size, MAX_CHUNK_SIZE)
    encodings = ("utf-8-sig", "utf-8", "gb18030", "gbk", "latin-1")
    last_decode_error: UnicodeDecodeError | None = None

    for index, encoding in enumerate(encodings):
        try:
            raw_rows = _read_csv_raw_rows(path, encoding)
            header_row = _choose_header_row(raw_rows)
            read_kwargs: dict[str, Any] = {
                "encoding": encoding,
                "dtype": object,
                "header": header_row,
                "skip_blank_lines": False,
            }
            if max_rows is not None:
                read_kwargs["nrows"] = max_rows
            columns_kwargs = {key: value for key, value in read_kwargs.items()}
            columns_kwargs.pop("nrows", None)
            columns_frame = pd.read_csv(path, nrows=0, **columns_kwargs)
            reader = pd.read_csv(path, chunksize=chunk_size, **read_kwargs)
            profile = _profile_frames(
                reader,
                raw_headers=raw_rows[header_row] if raw_rows else [],
                raw_rows=raw_rows,
                header_row=header_row,
                scan_complete=max_rows is None,
                chunked=True,
                read_method=f"pandas.read_csv encoding={encoding}",
                encoding_warning=(
                    f"encoding_fallback: {encoding}" if index > 0 else None
                ),
            )
            if not profile["columns"] and len(columns_frame.columns):
                profile["columns"] = len(columns_frame.columns)
                profile["columns_meta"] = [
                    {
                        "name": str(column),
                        "dtype": "empty",
                        "non_empty": 0,
                        "non_empty_count": 0,
                        "missing": 0,
                        "missing_count": 0,
                        "missing_ratio": 0.0,
                        "unique": 0,
                        "unique_count": 0,
                        "values": [],
                    }
                    for column in columns_frame.columns
                ]
                profile["column_count"] = len(columns_frame.columns)
                profile["column_names"] = [
                    str(column) for column in columns_frame.columns
                ]
                profile["duplicate_columns"], _ = _duplicate_headers(
                    raw_rows[header_row] if raw_rows else []
                )
                if not profile["duplicate_columns"] and raw_rows:
                    profile["warnings"].append("empty_table")
            return profile
        except pd.errors.EmptyDataError:
            return _empty_sheet_profile(
                raw_rows=[],
                read_method=f"pandas.read_csv encoding={encoding}",
            )
        except UnicodeDecodeError as exc:
            last_decode_error = exc
            continue

    if last_decode_error is not None:
        raise last_decode_error
    raise ValueError("CSV could not be read")


def _excel_visibility(path: Path) -> dict[str, bool | None]:
    """读取 xlsx sheet 可见性；不支持的格式返回未知。"""
    if path.suffix.lower() != ".xlsx":
        return {}
    try:
        import openpyxl

        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except (ImportError, OSError, ValueError):
        return {}
    try:
        return {
            worksheet.title: worksheet.sheet_state != "visible"
            for worksheet in workbook.worksheets
        }
    finally:
        workbook.close()


def _excel_sheet_profile(
    path: Path,
    sheet_name: str,
    *,
    engine: str | None,
    max_rows: int | None,
    visibility: bool | None,
) -> dict[str, Any]:
    """探查一个 Excel sheet。"""
    read_kwargs: dict[str, Any] = {
        "sheet_name": sheet_name,
        "header": None,
        "nrows": DEFAULT_HEADER_CANDIDATE_ROWS,
        "dtype": object,
    }
    if engine:
        read_kwargs["engine"] = engine
    raw_frame = pd.read_excel(path, **read_kwargs)
    raw_rows = [
        [_json_value(value) for value in row]
        for row in raw_frame.itertuples(index=False, name=None)
    ]
    header_row = _choose_header_row(raw_rows)

    data_kwargs: dict[str, Any] = {
        "sheet_name": sheet_name,
        "header": header_row,
        "dtype": object,
    }
    if engine:
        data_kwargs["engine"] = engine
    if max_rows is not None:
        data_kwargs["nrows"] = max_rows
    frame = pd.read_excel(path, **data_kwargs)
    profile = _profile_frames(
        [frame],
        raw_headers=raw_rows[header_row] if raw_rows else [],
        raw_rows=raw_rows,
        header_row=header_row,
        scan_complete=max_rows is None,
        chunked=False,
        read_method=f"pandas.read_excel engine={engine or 'default'}",
    )
    profile["hidden"] = visibility
    return profile


def _file_hash(path: Path) -> tuple[int, str]:
    """计算文件字节数和 SHA-256。"""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def inspect_source_file(
    path: str | Path,
    *,
    relative_path: str | None = None,
    role: str = "source_data",
    max_rows: int | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> dict[str, Any]:
    """探查单个源文件并返回 JSON 可序列化的结构。

    Args:
        path: 待读取的 csv/xls/xlsx 路径。
        relative_path: 报告中使用的相对路径，缺省使用文件名。
        role: 文件角色，可为 ``source_data`` 或 ``result_template``。
        max_rows: 可选的扫描上限；设置后报告必须标记为不完整。
        chunk_size: CSV 分块大小。

    Returns:
        文件级探查条目。读取失败也会返回带有错误和 warning 的条目。
    """
    file_path = Path(path)
    report_path = relative_path or file_path.name
    max_rows = _bounded_max_rows(max_rows)
    chunk_size = _bounded_positive(chunk_size, MAX_CHUNK_SIZE)
    entry: dict[str, Any] = {
        "path": report_path.replace("\\", "/"),
        "id": report_path.replace("\\", "/"),
        "role": role,
        "exists": file_path.is_file(),
        "format": file_path.suffix.lower().lstrip("."),
        "status": "skipped" if role == "result_template" else "unreadable",
        "bytes": 0,
        "sha256": None,
        "sheets": [],
        "warnings": [],
    }
    if not file_path.is_file():
        entry["warnings"] = ["missing_file"]
        return entry

    try:
        size, digest = _file_hash(file_path)
        entry["bytes"] = size
        entry["sha256"] = digest
    except OSError as exc:
        entry["warnings"] = [f"hash_error: {type(exc).__name__}"]
        return entry

    if role == "result_template":
        entry["skip_reason"] = "result template"
        return entry

    try:
        if file_path.suffix.lower() == ".csv":
            sheet = _csv_sheet_profile(
                file_path,
                max_rows=max_rows,
                chunk_size=chunk_size,
            )
            sheet["name"] = "Sheet1"
            entry["sheets"] = [sheet]
            entry["read_method"] = sheet["read_method"]
        else:
            excel_file = pd.ExcelFile(file_path)
            try:
                sheet_names = [str(name) for name in excel_file.sheet_names]
                engine = getattr(excel_file, "engine", None)
            finally:
                excel_file.close()
            visibility = _excel_visibility(file_path)
            sheets: list[dict[str, Any]] = []
            for sheet_name in sheet_names:
                try:
                    profile = _excel_sheet_profile(
                        file_path,
                        sheet_name,
                        engine=engine,
                        max_rows=max_rows,
                        visibility=visibility.get(sheet_name),
                    )
                except Exception as exc:
                    profile = {
                        "status": "error",
                        "readable": False,
                        "rows": 0,
                        "columns": 0,
                        "header_candidates": [],
                        "duplicate_columns": [],
                        "columns_meta": [],
                        "sample_rows": [],
                        "warnings": [f"parse_error: {type(exc).__name__}"],
                        "error": str(exc),
                        "read_method": (
                            f"pandas.read_excel engine={engine or 'default'}"
                        ),
                        "scan": {
                            "mode": "unavailable",
                            "rows_scanned": 0,
                            "complete": False,
                            "chunked": False,
                        },
                        "hidden": visibility.get(sheet_name),
                    }
                profile["name"] = sheet_name
                sheets.append(profile)
            entry["sheets"] = sheets
            entry["read_method"] = f"pandas.read_excel engine={engine or 'default'}"
    except Exception as exc:
        entry["warnings"] = [f"parse_error: {type(exc).__name__}"]
        entry["error"] = str(exc)
        return entry

    for sheet in entry["sheets"]:
        sheet["id"] = f"{entry['id']}::{sheet.get('name', '')}"

    warnings = [
        f"{sheet['name']}: {warning}"
        for sheet in entry["sheets"]
        for warning in sheet.get("warnings") or []
    ]
    entry["warnings"] = warnings
    readable_sheets = [
        sheet for sheet in entry["sheets"] if sheet.get("readable") is True
    ]
    if readable_sheets:
        entry["status"] = "readable"
    elif entry["sheets"]:
        entry["status"] = "unreadable"
    else:
        entry["status"] = "unreadable"
        entry["warnings"].append("no_sheet")
    return entry


def build_source_inspection(
    work_dir: str | Path,
    task_id: str = "",
    generated_at: str = "not_provided",
    *,
    max_rows: int | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> dict[str, Any]:
    """扫描任务目录根下的表格并生成 source inspection 结构。

    ``generated_at`` 由调用方传入而不是在本模块取当前时间，保证相同输入得到
    相同结果。任务流程落盘时应传入 ISO 8601 时间。
    """
    root = Path(work_dir)
    max_rows = _bounded_max_rows(max_rows)
    chunk_size = _bounded_positive(chunk_size, MAX_CHUNK_SIZE)
    paths = (
        sorted(
            (
                path
                for path in root.iterdir()
                if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
            ),
            key=lambda path: path.name,
        )
        if root.is_dir()
        else []
    )

    files: list[dict[str, Any]] = []
    for path in paths:
        role = "result_template" if is_result_template(path.name) else "source_data"
        files.append(
            inspect_source_file(
                path,
                relative_path=path.name,
                role=role,
                max_rows=max_rows,
                chunk_size=chunk_size,
            )
        )

    # 清洗路径必须与 data_contract 使用同一套归一化和碰撞规则；将结果
    # 提前写入 inspection，Coder 才能在真正落盘前看到碰撞安全的目标名。
    from app.utils.data_contract import inspection_cleaned_paths

    cleaned_paths = inspection_cleaned_paths({"files": files})
    for file_entry in files:
        for sheet in file_entry.get("sheets") or []:
            sheet_id = str(sheet.get("id") or "")
            if sheet_id in cleaned_paths:
                sheet["cleaned_path"] = cleaned_paths[sheet_id]

    source_files = [entry["path"] for entry in files if entry["role"] == "source_data"]
    skipped_files = [
        entry["path"] for entry in files if entry["role"] == "result_template"
    ]
    readable_sheet_count = sum(
        1
        for entry in files
        for sheet in entry.get("sheets") or []
        if sheet.get("readable") is True
    )
    if source_files:
        status = "ready" if readable_sheet_count else "unreadable_source_data"
        data_status = "source_data"
    else:
        status = "not_applicable"
        data_status = "no_source_data"

    return {
        "version": INSPECTION_VERSION,
        "task_id": task_id,
        "generated_at": generated_at,
        "status": status,
        "data_status": data_status,
        "source_files": source_files,
        "skipped_files": skipped_files,
        "files": files,
    }


def save_source_inspection(work_dir: str | Path, inspection: dict[str, Any]) -> str:
    """把源数据探查报告写入任务目录，返回相对路径对应的绝对路径。"""
    path = Path(work_dir) / INSPECTION_FILENAME
    with path.open("w", encoding="utf-8") as stream:
        json.dump(inspection, stream, ensure_ascii=False, indent=2)
    return str(path)


def load_source_inspection(work_dir: str | Path) -> dict[str, Any]:
    """读取任务目录中的源数据探查报告。"""
    path = Path(work_dir) / INSPECTION_FILENAME
    try:
        with path.open(encoding="utf-8") as stream:
            inspection = json.load(stream)
    except FileNotFoundError as exc:
        raise ValueError(f"缺少 {INSPECTION_FILENAME}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{INSPECTION_FILENAME} 不是合法 JSON") from exc
    if not isinstance(inspection, dict):
        raise ValueError(f"{INSPECTION_FILENAME} 根节点必须是对象")
    return inspection


def _clean_render_text(value: Any) -> str:
    """清理渲染字段中的换行，避免单条记录绕过预算。"""
    return " ".join(str(value).split())


def _shorten_render_text(value: Any, max_chars: int) -> str:
    """压缩单个渲染字段，保留首尾以便路径仍可定位。"""
    text = _clean_render_text(value)
    if len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return text[:max_chars]
    left = max(1, (max_chars - 1) // 2)
    right = max_chars - 1 - left
    return f"{text[:left]}…{text[-right:]}" if right else f"{text[:left]}…"


def _warning_summary(value: Any, max_chars: int) -> str:
    """保留 warning 的开头摘要，避免不同条目的定位前缀被截掉。"""
    text = _clean_render_text(value)
    if len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return text[:max_chars]
    return text[: max_chars - 1] + "…"


def _append_bounded(lines: list[str], line: str, budget: int) -> bool:
    """向结果追加一行，无法容纳时截断该行。"""
    current = "\n".join(lines)
    separator = 1 if lines else 0
    available = budget - len(current) - separator
    if available <= 0:
        return False
    if len(line) <= available:
        lines.append(line)
        return True
    if available == 1:
        lines.append("…")
    else:
        lines.append(line[: available - 1] + "…")
    return False


def render_source_inspection(
    inspection: dict[str, Any],
    budget: int = DEFAULT_RENDER_BUDGET,
) -> str:
    """把探查结构渲染成不超过 ``budget`` 字符的确定性文本。

    内容优先级为任务状态、文件/sheet 定位 token、规模和风险警告，列名、hash、
    低基数取值和样例后置。
    """
    budget = _bounded_nonnegative(budget, MAX_RENDER_BUDGET)
    if budget <= 0:
        return ""

    files = inspection.get("files") or []
    header = (
        "源数据探查"
        f" version={inspection.get('version', '?')}"
        f" task={_shorten_render_text(inspection.get('task_id', ''), 80)}"
        f" status={_shorten_render_text(inspection.get('status', ''), 32)}"
        f" data_status={_shorten_render_text(inspection.get('data_status', ''), 32)}"
    )
    priority_lines: list[tuple[str, str, str, str]] = []
    column_lines: list[str] = []
    value_lines: list[str] = []
    sample_lines: list[str] = []
    hash_lines: list[str] = []
    for entry in files:
        raw_path = _clean_render_text(entry.get("path", ""))
        path = _shorten_render_text(raw_path, 160)
        sheets = entry.get("sheets") or []
        if not sheets:
            warnings = _shorten_render_text(
                "; ".join(
                    _clean_render_text(warning)
                    for warning in entry.get("warnings") or []
                )
                or "none",
                180,
            )
            line = (
                f"文件 {path} status={entry.get('status', '')}"
                f" role={entry.get('role', '')} warning={warnings}"
            )
            priority_lines.append((line, line, line, line))
        for sheet in sheets:
            raw_name = _clean_render_text(sheet.get("name", ""))
            scan = sheet.get("scan") or {}
            raw_source_label = f"{raw_path}::{raw_name}"
            source_label = _shorten_render_text(raw_source_label, 220)
            declared_cleaned_path = _clean_render_text(
                sheet.get("cleaned_path") or ""
            )
            entry_id = _clean_render_text(sheet.get("id") or "")
            if entry_id:
                token = entry_id
            elif len(raw_source_label) <= 140:
                token = raw_source_label
            elif declared_cleaned_path and declared_cleaned_path != "undeclared":
                token = declared_cleaned_path
            else:
                token = raw_source_label
            cleaned_path = _shorten_render_text(
                sheet.get("cleaned_path") or "undeclared",
                180,
            )
            warning_text = _shorten_render_text(
                "; ".join(
                    _clean_render_text(warning)
                    for warning in sheet.get("warnings") or []
                ),
                220,
            )
            skip_reason = _shorten_render_text(sheet.get("skip_reason") or "", 160)
            if skip_reason:
                warning_text = (
                    f"{warning_text}; skip={skip_reason}"
                    if warning_text
                    else f"skip={skip_reason}"
                )
            if not warning_text:
                warning_text = "none"
            full_line = (
                f"sheet {source_label}"
                f" status={sheet.get('status', '')}"
                f" rows={sheet.get('rows', 0)}"
                f" cols={sheet.get('columns', 0)}"
                f" scan={scan.get('mode', '')}/{scan.get('complete', False)}"
                f" cleaned_path={cleaned_path}"
                f" warning={warning_text}"
            )
            compact_line = (
                f"sheet {token}"
                f" status={_shorten_render_text(sheet.get('status', ''), 24)}"
                f" rows={sheet.get('rows', 0)}"
                f" cols={sheet.get('columns', 0)}"
                f" warning={_warning_summary(warning_text, 120) or 'none'}"
            )
            minimal_line = (
                f"sheet {token}"
                f" status={_shorten_render_text(sheet.get('status', ''), 16)}"
                f" rows={sheet.get('rows', 0)}"
                f" cols={sheet.get('columns', 0)}"
                f" warning={_warning_summary(warning_text, 32) or 'none'}"
            )
            required_line = (
                f"sheet {token}"
                f" status={_shorten_render_text(sheet.get('status', ''), 16)}"
                f" rows={sheet.get('rows', 0)}"
                f" cols={sheet.get('columns', 0)}"
                f" warning={_warning_summary(warning_text, 12) or 'none'}"
            )
            priority_lines.append(
                (full_line, compact_line, minimal_line, required_line)
            )

            columns = sheet.get("columns_meta") or []
            if columns:
                column_lines.append(
                    "  columns: "
                    + ", ".join(
                        _clean_render_text(column.get("name", ""))
                        for column in columns
                    )
                )
            for column in columns:
                values = column.get("values") or []
                if values:
                    value_lines.append(
                        f"  values {column.get('name', '')}: "
                        + ", ".join(
                            _clean_render_text(value) for value in values
                        )
                    )
            for row in sheet.get("sample_rows") or []:
                sample_lines.append(
                    "  sample: "
                    + " | ".join(_clean_render_text(value) for value in row)
                )
        if entry.get("sha256"):
            hash_lines.append(f"  sha256={entry['sha256']}")

    lines: list[str] = []
    required_budget = sum(
        len(candidate[3]) + 1 for candidate in priority_lines
    )
    if len(header) + required_budget <= budget:
        lines.append(header)
    for index, (full_line, compact_line, minimal_line, required_line) in enumerate(
        priority_lines
    ):
        used = len("\n".join(lines))
        separator = 1 if lines else 0
        available = budget - used - separator
        remaining_minimum = sum(
            len(candidate[3]) + 1
            for candidate in priority_lines[index + 1 :]
        )
        capacity = available - remaining_minimum
        if capacity >= len(full_line):
            line = full_line
        elif capacity >= len(compact_line):
            line = compact_line
        elif capacity >= len(minimal_line):
            line = minimal_line
        elif capacity >= len(required_line):
            line = required_line
        else:
            # 最低级只压缩 warning；定位 token、状态和规模不参与截断。
            warning_budget = max(1, capacity - (len(required_line) - 12))
            line = required_line
            if warning_budget < 12:
                prefix, _, warning = required_line.partition(" warning=")
                line = (
                    f"{prefix} warning="
                    f"{_warning_summary(warning, warning_budget) or 'none'}"
                )
        if len(line) > available:
            # 预算不足以覆盖所有最低字段时，保留已经开始的 sheet 行；
            # 后置字段不会挤掉后续 sheet 的定位摘要。
            if available <= 0:
                continue
            line = line[:available]
        _append_bounded(lines, line, budget)
    for line in [*column_lines, *hash_lines, *value_lines, *sample_lines]:
        available = budget - len("\n".join(lines)) - (1 if lines else 0)
        if len(line) > available:
            break
        _append_bounded(lines, line, budget)
    return "\n".join(lines)


render_source_inspection_for_llm = render_source_inspection
