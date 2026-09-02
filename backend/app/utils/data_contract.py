"""清洗产物口径：cleaned/ 一表一 csv，以及 data_contract.json 的构建、校验与限长渲染。

后端只认 cleaned/*.csv，不扫整个 work_dir 的原始附件。
磁盘上的 JSON 是校验真源；进 LLM 的是限长渲染文本。
"""

from __future__ import annotations

import json
import hashlib
import os
import re
from pathlib import Path
from typing import Any

import pandas as pd

CLEANED_DIR_NAME = "cleaned"
CONTRACT_FILENAME = "data_contract.json"
CONTRACT_VERSION = 1
SOURCE_INSPECTION_FILENAME = "source_inspection.json"
DEFAULT_SHEET_NAME = "Sheet1"
MAX_SAMPLE_ROWS = 3
MAX_CATEGORY_VALUES = 20
MAX_CLEANING_NOTES_CHARS = 1500
DEFAULT_RENDER_BUDGET = 8000

_UNSAFE_CHARS = re.compile(r'[/\\:*?"<>|]')
_WHITESPACE = re.compile(r"\s+")
_COLLISION_MARKER = "__collision-"
_SOURCE_HASH_MARKER = "__source_sha256-"
_ENTRY_HASH_MARKER = "__entry-"
_COLLISION_HASH_LENGTH = 8


class DataContractError(Exception):
    """清洗产物不符合口径。"""


def sanitize_filename_token(name: str) -> str:
    """去掉路径非法字符，空白改为下划线，供清洗文件名使用。"""
    cleaned = _UNSAFE_CHARS.sub("_", (name or "").strip())
    cleaned = _WHITESPACE.sub("_", cleaned).strip("._")
    return cleaned or "unnamed"


def is_result_template(filename: str) -> bool:
    """判断是否为答题模板（result*.xlsx / csv），数据准备阶段应忽略。"""
    name = os.path.basename(filename).lower()
    return name.startswith("result") and name.endswith((".xlsx", ".xls", ".csv"))


def is_source_data_file(filename: str) -> bool:
    """判断是否为原始数据表（csv/xlsx），排除模板与 contract。"""
    name = os.path.basename(filename)
    lower = name.lower()
    if name == CONTRACT_FILENAME:
        return False
    if not lower.endswith((".csv", ".xlsx", ".xls")):
        return False
    return not is_result_template(name)


def list_source_data_files(work_dir: str) -> list[str]:
    """列出 work_dir 根目录下的原始数据文件，不含 cleaned/ 与 result 模板。"""
    if not os.path.isdir(work_dir):
        return []
    names: list[str] = []
    for name in sorted(os.listdir(work_dir)):
        path = os.path.join(work_dir, name)
        if os.path.isdir(path):
            continue
        if is_source_data_file(name):
            names.append(name)
    return names


def has_source_data(work_dir: str) -> bool:
    """工作目录根下是否存在需要清洗的表格。"""
    return bool(list_source_data_files(work_dir))


def cleaned_dir(work_dir: str) -> str:
    """返回 cleaned/ 绝对路径。"""
    return os.path.join(work_dir, CLEANED_DIR_NAME)


def cleaned_relpath_for(
    source_filename: str,
    sheet: str,
) -> str:
    """生成清洗 csv 的历史兼容基础路径。"""
    stem = sanitize_filename_token(Path(source_filename).stem)
    sheet_token = sanitize_filename_token(sheet or DEFAULT_SHEET_NAME)
    return f"{CLEANED_DIR_NAME}/{stem}__{sheet_token}.csv"


def _collision_cleaned_relpath_for(
    source_filename: str,
    sheet: str,
    inspection_entry_id: str,
) -> str:
    """为同一 inspection 内的基础路径碰撞生成稳定短后缀。"""
    base_path = cleaned_relpath_for(source_filename, sheet)
    stem = Path(base_path).stem
    entry_digest = hashlib.sha256(inspection_entry_id.encode("utf-8")).hexdigest()
    return (
        f"{CLEANED_DIR_NAME}/{stem}{_COLLISION_MARKER}"
        f"{entry_digest[:_COLLISION_HASH_LENGTH]}.csv"
    )


def list_cleaned_csvs(work_dir: str) -> list[str]:
    """列出 cleaned/ 下的 csv 文件名（不含目录）。"""
    directory = cleaned_dir(work_dir)
    if not os.path.isdir(directory):
        return []
    return sorted(
        name
        for name in os.listdir(directory)
        if name.lower().endswith(".csv") and not name.startswith(".")
    )


def truncate_cleaning_notes(
    notes: str, max_chars: int = MAX_CLEANING_NOTES_CHARS
) -> str:
    """截取 Coder 收工摘要，避免 notes 撑爆 contract。"""
    text = (notes or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


def format_data_prep_brief(source_files: list[str]) -> str:
    """数据准备阶段注入 Coder 的口径和探查报告状态指针。"""
    files_line = ", ".join(source_files) if source_files else "（无）"
    brief = (
        "原始数据文件（只读，禁止覆盖、禁止改名、禁止挪走）："
        f"{files_line}\n"
        "源数据探查报告已落盘，首轮阶段提示将提供有限探查材料；"
        "请按文件和 sheet 阅读后再决定清洗动作。\n"
        "答题模板 result*.xlsx / result*.csv 忽略，不要清洗、不要改名。\n\n"
        "清洗输出口径（必须遵守）：\n"
        f"- 目录：{CLEANED_DIR_NAME}/\n"
        "- 格式：UTF-8 csv\n"
        "- xlsx 多 sheet → 一 sheet 一文件；禁止把两张表拼成一个文件\n"
        f"- 无碰撞时文件名：{{附件主名}}__{{sheet名}}.csv ，例如 {CLEANED_DIR_NAME}/附件1__乡村现有耕地.csv\n"
        "- 若探查声明中存在 cleaned_path，必须严格使用该声明路径，不要自行猜测或改名\n"
        f"- 单 sheet 或原来就是 csv：sheet 名用 {DEFAULT_SHEET_NAME}\n"
        '- sheet 名去掉 / \\ : * ? " < > | ，空白改下划线\n'
        f"- 完成后 {CLEANED_DIR_NAME}/ 至少 1 个 csv\n"
        "- 本阶段不要画论文图，不要改 result 模板\n"
        "- 对每个源文件/sheet 明确记录：清洗动作、输出路径或跳过原因、警告和限制；"
        "不得静默跳过不可读或异常 sheet"
    )
    return brief


def format_no_data_brief() -> str:
    """无表格时给建模手 / 解题 Coder 的说明。"""
    return (
        "未发现 csv/xlsx 表格（答题模板 result* 不计）。"
        "按物理/机理题建模，禁止编造列名或数据集。"
    )


def _json_cell(val: Any) -> Any:
    """把 pandas/numpy 标量转成 JSON 可序列化值。"""
    if val is None:
        return None
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(val, pd.Timestamp):
        return val.isoformat()
    if hasattr(val, "item"):
        try:
            return val.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(val, (str, int, float, bool)):
        return val
    return str(val)


def _infer_column_meta(series: pd.Series) -> dict[str, Any]:
    """从一列推断 dtype / 缺失 / 低基数取值。"""
    n = len(series)
    missing = int(series.isna().sum())
    missing_pct = round(missing / n, 4) if n else 0.0
    nunique = int(series.nunique(dropna=True))
    if pd.api.types.is_datetime64_any_dtype(series):
        kind = "datetime"
    elif pd.api.types.is_bool_dtype(series):
        kind = "bool"
    elif pd.api.types.is_integer_dtype(series):
        kind = "int"
    elif pd.api.types.is_float_dtype(series):
        kind = "float"
    else:
        kind = "string"
    col: dict[str, Any] = {
        "name": str(series.name),
        "dtype": kind,
        "missing_pct": missing_pct,
        "nunique": nunique,
    }
    if kind == "string" and 0 < nunique <= MAX_CATEGORY_VALUES:
        raw_values = series.dropna().astype(str).unique().tolist()
        col["values"] = [str(v) for v in raw_values[:MAX_CATEGORY_VALUES]]
    return col


def _sample_rows(df: pd.DataFrame, n: int = MAX_SAMPLE_ROWS) -> list[list[Any]]:
    """取前 n 行作为样例，单元格转为 JSON 安全值。"""
    rows: list[list[Any]] = []
    for _, row in df.head(n).iterrows():
        rows.append([_json_cell(val) for val in row.tolist()])
    return rows


def _parse_cleaned_filename(filename: str) -> tuple[str, str]:
    """从 附件1__乡村现有耕地.csv 解析 (附件1, 乡村现有耕地)。"""
    stem = Path(filename).stem
    for marker in (_COLLISION_MARKER, _SOURCE_HASH_MARKER, _ENTRY_HASH_MARKER):
        stem = stem.split(marker, 1)[0]
    if "__" not in stem:
        return stem, DEFAULT_SHEET_NAME
    source, sheet = stem.split("__", 1)
    return source, sheet


def infer_source_filename(work_dir: str, stem: str) -> str:
    """用清洗文件主名反推 work_dir 根下的原始附件名。"""
    for ext in (".xlsx", ".xls", ".csv"):
        name = f"{stem}{ext}"
        if os.path.isfile(os.path.join(work_dir, name)):
            return name
    matches = [
        name
        for name in list_source_data_files(work_dir)
        if sanitize_filename_token(Path(name).stem) == stem
    ]
    if len(matches) == 1:
        return matches[0]
    return stem


def _inspection_entry_id(
    file_entry: dict[str, Any], sheet: dict[str, Any]
) -> str:
    """返回 inspection 中 sheet 的稳定 ID，兼容旧报告字段。"""
    name = str(sheet.get("name") or DEFAULT_SHEET_NAME)
    return str(
        sheet.get("id")
        or f"{file_entry.get('id') or file_entry.get('path')}::{name}"
    )


def inspection_cleaned_paths(
    source_inspection: dict[str, Any] | None,
) -> dict[str, str]:
    """为 inspection 中的每个 source sheet 生成唯一清洗目标路径。

    旧命名只在基础路径唯一时保留；一旦不同合法文件名或 sheet 名归一化到
    同一路径，使用 inspection entry 的短 SHA 生成不冲突的目标。源文件
    SHA-256 只保留在 inspection/contract provenance 中作为审计字段。返回值
    按 ``inspection_entry_id`` 索引，供 source inspection 和 contract 共用。
    """
    if not source_inspection:
        return {}

    candidates: list[tuple[str, str, str, str]] = []
    for file_entry in source_inspection.get("files") or []:
        if file_entry.get("role") != "source_data":
            continue
        source_file = str(file_entry.get("path") or "")
        if not source_file:
            continue
        for sheet in file_entry.get("sheets") or []:
            source_sheet = str(sheet.get("name") or DEFAULT_SHEET_NAME)
            entry_id = _inspection_entry_id(file_entry, sheet)
            base_path = cleaned_relpath_for(source_file, source_sheet)
            candidates.append(
                (
                    entry_id,
                    source_file,
                    source_sheet,
                    base_path,
                )
            )

    base_counts: dict[str, int] = {}
    for _, _, _, base_path in candidates:
        base_counts[base_path] = base_counts.get(base_path, 0) + 1

    paths: dict[str, str] = {}
    for entry_id, source_file, source_sheet, base_path in candidates:
        if base_counts[base_path] == 1:
            paths[entry_id] = base_path
            continue
        paths[entry_id] = _collision_cleaned_relpath_for(
            source_file, source_sheet, entry_id
        )
    return paths


def _cleaned_source_candidates(
    filename: str,
    source_inspection: dict[str, Any] | None,
) -> list[tuple[str, str]]:
    """返回一个 cleaned 文件在 inspection 声明中的所有候选来源。"""
    return [
        (
            str(file_entry.get("path") or ""),
            str(sheet.get("name") or DEFAULT_SHEET_NAME),
        )
        for file_entry, sheet in _cleaned_inspection_candidates(
            filename, source_inspection
        )
    ]


def _cleaned_inspection_candidates(
    filename: str,
    source_inspection: dict[str, Any] | None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """返回一个 cleaned 文件在 inspection 中的所有 sheet 候选。"""
    if not source_inspection:
        return []
    expected_path = f"{CLEANED_DIR_NAME}/{Path(filename).name}".replace("\\", "/")
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for file_entry in source_inspection.get("files") or []:
        if file_entry.get("role") != "source_data":
            continue
        if not file_entry.get("path"):
            continue
        for sheet in file_entry.get("sheets") or []:
            declared_path = str(sheet.get("cleaned_path") or "")
            declared_path = declared_path.replace("\\", "/")
            if declared_path == expected_path:
                matches.append((file_entry, sheet))
    return sorted(
        matches,
        key=lambda item: (
            str(item[0].get("path") or ""),
            str(item[1].get("name") or DEFAULT_SHEET_NAME),
        ),
    )


def _declared_cleaned_source(
    filename: str,
    source_inspection: dict[str, Any],
) -> tuple[str, str]:
    """按 inspection 的声明路径唯一确定清洗文件来源。"""
    file_entry, sheet = _declared_cleaned_entry(filename, source_inspection)
    return (
        str(file_entry.get("path") or ""),
        str(sheet.get("name") or DEFAULT_SHEET_NAME),
    )


def _declared_cleaned_entry(
    filename: str,
    source_inspection: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """按 inspection 的声明路径唯一确定清洗文件对应的 sheet。"""
    matches = _cleaned_inspection_candidates(filename, source_inspection)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        candidate_text = ", ".join(
            f"{file_entry.get('path', '')} / "
            f"{sheet.get('name') or DEFAULT_SHEET_NAME}"
            for file_entry, sheet in matches
        )
        raise DataContractError(
            f"{CLEANED_DIR_NAME}/{Path(filename).name} 来源映射不唯一: "
            f"{candidate_text}"
        )
    raise DataContractError(
        f"{CLEANED_DIR_NAME}/{Path(filename).name} "
        "未在 source inspection 中声明 cleaned_path"
    )


def _file_hash(path: str | Path) -> tuple[int, str] | None:
    """计算文件字节数和 SHA-256；文件不可读时返回 None。"""
    digest = hashlib.sha256()
    size = 0
    try:
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
    except OSError:
        return None
    return size, digest.hexdigest()


def _inspection_sheet(
    inspection: dict[str, Any] | None,
    source_file: str,
    source_sheet: str,
    *,
    inspection_entry_id: str | None = None,
    source_sha256: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """按 entry ID、源 hash、文件和 sheet 确定性查找 inspection 条目。"""
    if not inspection:
        return None

    def _sheet_matches(
        file_entries: list[dict[str, Any]],
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for file_entry in file_entries:
            for sheet in file_entry.get("sheets") or []:
                if str(sheet.get("name") or "") == source_sheet:
                    matches.append((file_entry, sheet))
        return matches

    normalized_source = source_file.replace("\\", "/")
    file_entries = [
        file_entry
        for file_entry in inspection.get("files") or []
        if file_entry.get("role") == "source_data"
    ]

    if inspection_entry_id:
        entry_matches = [
            (file_entry, sheet)
            for file_entry in file_entries
            for sheet in file_entry.get("sheets") or []
            if _inspection_entry_id(file_entry, sheet) == inspection_entry_id
        ]
        if len(entry_matches) == 1:
            return entry_matches[0]

    if source_sha256:
        hash_matches = [
            file_entry
            for file_entry in file_entries
            if str(file_entry.get("sha256") or "") == source_sha256
        ]
        exact_hash_sheets = _sheet_matches(hash_matches)
        if len(exact_hash_sheets) == 1:
            return exact_hash_sheets[0]

    exact_file_matches = [
        file_entry
        for file_entry in file_entries
        if (
            str(file_entry.get("path") or "").replace("\\", "/")
            == normalized_source
            or Path(str(file_entry.get("path") or "")).name
            == Path(normalized_source).name
        )
    ]
    exact_sheets = _sheet_matches(exact_file_matches)
    if len(exact_sheets) == 1:
        return exact_sheets[0]

    fuzzy_file_matches = [
        file_entry
        for file_entry in file_entries
        if sanitize_filename_token(
            Path(str(file_entry.get("path") or "")).stem
        )
        == sanitize_filename_token(Path(normalized_source).stem)
    ]
    fuzzy_sheets = _sheet_matches(fuzzy_file_matches)
    if len(fuzzy_sheets) == 1:
        return fuzzy_sheets[0]

    # Sheet 名也可能因空格/非法字符归一化而碰撞；只有唯一候选时才允许
    # 这种兼容性兜底，绝不按 inspection 列表顺序随机选第一项。
    fuzzy_sheet_matches = [
        (file_entry, sheet)
        for file_entry in file_entries
        for sheet in file_entry.get("sheets") or []
        if sanitize_filename_token(str(sheet.get("name") or ""))
        == sanitize_filename_token(source_sheet)
    ]
    if len(fuzzy_sheet_matches) == 1:
        return fuzzy_sheet_matches[0]
    return None


def _cleaning_actions(cleaning_notes: str) -> list[str]:
    """从 Coder 收工摘要提取有限的动作证据。"""
    lines = [line.strip(" -*\t") for line in (cleaning_notes or "").splitlines()]
    return [line for line in lines if line][:20]


def _column_names_from_inspection(sheet: dict[str, Any]) -> list[str]:
    """读取 inspection 中的列名，兼容不同版本的画像字段。"""
    names = sheet.get("column_names")
    if isinstance(names, list):
        return [str(name) for name in names]
    return [
        str(column.get("name", ""))
        for column in sheet.get("columns_meta") or []
        if isinstance(column, dict)
    ]


_ROW_CHANGE_TERMS = (
    "行",
    "记录",
    "row",
    "record",
    "duplicate",
    "重复",
    "空行",
    "去重",
    "dedup",
    "删除",
    "drop",
    "remove",
    "过滤",
    "filter",
    "保留",
    "retain",
)
_COLUMN_CHANGE_TERMS = (
    "列",
    "字段",
    "column",
    "field",
    "重命名",
    "rename",
    "新增",
    "添加",
    "删除",
    "drop",
    "remove",
)


def _change_is_explained(
    values: list[Any],
    terms: tuple[str, ...],
) -> bool:
    """判断动作/限制是否明确解释了对应的行列变化。

    ``scan is bounded`` 只能说明探查能力有限，不能解释清洗结果为什么改变。
    因此这里不把所有 limitation 当作变化说明，而要求文本至少触及对应维度。
    """
    text = " ".join(str(value).casefold() for value in values if value)
    return any(term.casefold() in text for term in terms)


def _build_table_provenance(
    work_dir: str,
    table: dict[str, Any],
    *,
    cleaning_notes: str,
    source_inspection: dict[str, Any] | None,
    inspection_file_sha256: str | None,
) -> dict[str, Any]:
    """为一张清洗表构建可校验的来源和前后变化记录。"""
    source_file = str(table.get("source") or "")
    source_sheet = str(table.get("sheet") or DEFAULT_SHEET_NAME)
    cleaned_path = os.path.join(work_dir, str(table["path"]))
    source_path = os.path.join(work_dir, source_file)
    source_hash_info = _file_hash(source_path)
    cleaned_hash_info = _file_hash(cleaned_path)
    source_sha256 = source_hash_info[1] if source_hash_info else None
    cleaned_sha256 = cleaned_hash_info[1] if cleaned_hash_info else None
    after_columns = [str(column["name"]) for column in table.get("columns") or []]
    after = {
        "rows": int(table.get("rows", 0)),
        "columns": len(after_columns),
        "column_names": after_columns,
        "sha256": cleaned_sha256,
    }

    matched = _inspection_sheet(
        source_inspection,
        source_file,
        source_sheet,
        inspection_entry_id=(
            str(table.get("id")) if table.get("id") else None
        ),
        source_sha256=source_sha256,
    )
    actions = _cleaning_actions(cleaning_notes)
    warnings: list[str] = []
    limitations: list[str] = []
    before: dict[str, Any] | None = None
    inspection_entry_id: str | None = None
    if matched is None:
        limitations.append("source inspection entry unavailable")
    else:
        file_entry, sheet = matched
        inspection_entry_id = _inspection_entry_id(file_entry, sheet)
        before_columns = _column_names_from_inspection(sheet)
        before = {
            "rows": int(sheet.get("rows", 0)),
            "columns": int(
                sheet.get("columns", sheet.get("column_count", len(before_columns)))
            ),
            "column_names": before_columns,
        }
        warnings.extend(str(warning) for warning in sheet.get("warnings") or [])
        scan = sheet.get("scan") or {}
        if scan.get("complete") is False:
            limitations.append("source inspection scan is bounded")
        if sheet.get("status") != "readable" or sheet.get("readable") is False:
            limitations.append("source sheet was not readable during inspection")

    if before is None:
        limitations.append("cleaning before/after change cannot be verified")
    else:
        removed = [
            name for name in before["column_names"] if name not in after_columns
        ]
        added = [name for name in after_columns if name not in before["column_names"]]
        if removed:
            warnings.append("columns_removed: " + ", ".join(removed))
        if added:
            warnings.append("columns_added: " + ", ".join(added))

    provenance = {
        "status": "pending_validation",
        "inspection_path": SOURCE_INSPECTION_FILENAME,
        "inspection_entry_id": inspection_entry_id,
        "inspection_file_sha256": inspection_file_sha256,
        "source_file": source_file,
        "source_sheet": source_sheet,
        "source_sha256": source_sha256,
        "cleaned_file": table["path"],
        "cleaned_sha256": cleaned_sha256,
        "before": before,
        "after": after,
        "actions": actions,
        "warnings": warnings,
        "limitations": limitations,
    }
    return {
        "source_file": source_file,
        "source_sheet": source_sheet,
        "inspection_entry_id": inspection_entry_id,
        "inspection_sha256": inspection_file_sha256,
        "source_sha256": source_sha256,
        "cleaned_sha256": cleaned_sha256,
        "cleaning_before": before,
        "cleaning_after": after,
        "actions": actions,
        "warnings": warnings,
        "limitations": limitations,
        "provenance": provenance,
    }


def _load_optional_source_inspection(
    work_dir: str, source_inspection: dict[str, Any] | None
) -> dict[str, Any] | None:
    """优先使用调用方报告，否则兼容从任务目录读取已有报告。"""
    if source_inspection is not None:
        return source_inspection
    path = os.path.join(work_dir, SOURCE_INSPECTION_FILENAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise DataContractError(
            f"{SOURCE_INSPECTION_FILENAME} 无法读取: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise DataContractError(f"{SOURCE_INSPECTION_FILENAME} 根节点必须是对象")
    return value


def _is_readable_source_sheet(
    file_entry: dict[str, Any], sheet: dict[str, Any]
) -> bool:
    """判断 inspection 中的 sheet 是否需要被清洗覆盖。"""
    return (
        file_entry.get("role") == "source_data"
        and sheet.get("readable") is True
        and sheet.get("status") == "readable"
    )


def _has_explicit_skip_reason(sheet: dict[str, Any]) -> bool:
    """判断 sheet 是否有可机器校验的非空跳过理由。"""
    reason = sheet.get("skip_reason")
    return isinstance(reason, str) and bool(reason.strip())


def _validate_readable_sheet_skip_reasons(
    inspection: dict[str, Any],
) -> None:
    """独立校验所有带 skip_reason 的可读 source sheet。"""
    invalid: list[str] = []
    for file_entry, sheet in _readable_source_sheets(inspection):
        if "skip_reason" not in sheet or _has_explicit_skip_reason(sheet):
            continue
        invalid.append(
            f"{file_entry.get('path', '')} / "
            f"{sheet.get('name') or DEFAULT_SHEET_NAME}"
        )
    if invalid:
        raise DataContractError(
            "source inspection readable sheet coverage 不完整；"
            "skip_reason 必须是非空字符串: "
            + ", ".join(invalid)
        )


def _readable_source_sheets(
    inspection: dict[str, Any],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """返回 inspection 中所有需要清洗或明确跳过的 source sheet。"""
    return [
        (file_entry, sheet)
        for file_entry in inspection.get("files") or []
        for sheet in file_entry.get("sheets") or []
        if _is_readable_source_sheet(file_entry, sheet)
    ]


def _explicitly_skipped_source_sheets(
    inspection: dict[str, Any],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """返回所有带有效 skip reason 的 readable source sheet。"""
    sheets = _readable_source_sheets(inspection)
    if not sheets or not all(
        _has_explicit_skip_reason(sheet) for _, sheet in sheets
    ):
        return []
    return sheets


def _skipped_sheet_records(
    sheets: list[tuple[dict[str, Any], dict[str, Any]]],
) -> list[dict[str, str]]:
    """把跳过的 source sheet 转成 contract 中稳定、可审计的记录。"""
    return [
        {
            "source_file": str(file_entry.get("path") or ""),
            "source_sheet": str(sheet.get("name") or DEFAULT_SHEET_NAME),
            "inspection_entry_id": _inspection_entry_id(file_entry, sheet),
            "skip_reason": str(sheet["skip_reason"]).strip(),
        }
        for file_entry, sheet in sheets
    ]


def _validate_readable_sheet_coverage(
    inspection: dict[str, Any],
    tables: list[dict[str, Any]],
    *,
    covered_entry_ids: set[str] | None = None,
) -> None:
    """确保每个可读源 sheet 有清洗表或明确的结构化跳过理由。"""
    _validate_readable_sheet_skip_reasons(inspection)
    covered = set(covered_entry_ids or ())
    for table in tables:
        provenance = table.get("provenance") or {}
        entry_id = (
            provenance.get("inspection_entry_id")
            or table.get("inspection_entry_id")
        )
        if entry_id:
            covered.add(str(entry_id))
            continue
        path = str(table.get("path") or "")
        candidates = _cleaned_inspection_candidates(path, inspection)
        if len(candidates) == 1:
            covered.add(_inspection_entry_id(*candidates[0]))

    missing: list[str] = []
    for file_entry in inspection.get("files") or []:
        for sheet in file_entry.get("sheets") or []:
            if not _is_readable_source_sheet(file_entry, sheet):
                continue
            entry_id = _inspection_entry_id(file_entry, sheet)
            source_label = (
                f"{file_entry.get('path', '')} / "
                f"{sheet.get('name') or DEFAULT_SHEET_NAME}"
            )
            if entry_id in covered:
                continue
            if _has_explicit_skip_reason(sheet):
                continue
            missing.append(source_label)

    if missing:
        details: list[str] = []
        details.append("缺少 cleaned CSV 或明确 skip_reason: " + ", ".join(missing))
        raise DataContractError(
            "source inspection readable sheet coverage 不完整；" + "；".join(details)
        )


def profile_cleaned_csv(
    work_dir: str,
    filename: str,
    source_inspection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """读取一张 cleaned csv，生成 contract 中的 table 条目。"""
    abs_path = os.path.join(cleaned_dir(work_dir), filename)
    df = pd.read_csv(abs_path)
    parsed_source_stem, parsed_sheet = _parse_cleaned_filename(filename)
    if source_inspection is not None:
        declared_entry = _declared_cleaned_entry(filename, source_inspection)
        source = str(declared_entry[0].get("path") or "")
        sheet = str(declared_entry[1].get("name") or DEFAULT_SHEET_NAME)
        table_id = _inspection_entry_id(*declared_entry)
    else:
        source = infer_source_filename(work_dir, parsed_source_stem)
        sheet = parsed_sheet
        table_id = f"{parsed_source_stem}__{parsed_sheet}"
    relpath = f"{CLEANED_DIR_NAME}/{filename}".replace("\\", "/")
    return {
        "id": table_id,
        "path": relpath,
        "source": source,
        "sheet": sheet,
        "rows": int(len(df)),
        "columns": [_infer_column_meta(df.iloc[:, i]) for i in range(df.shape[1])],
        "sample_rows": _sample_rows(df),
    }


def build_data_contract(
    work_dir: str,
    cleaning_notes: str = "",
    source_inspection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """根据 cleaned/*.csv 构建 data_contract。

    Args:
        work_dir: 任务工作目录。
        cleaning_notes: Coder 收工摘要，写入顶层 notes。
        source_inspection: EDA 前生成的源数据探查报告；缺省时尝试读取落盘报告。

    Returns:
        contract 字典。

    Raises:
        DataContractError: cleaned/ 不存在、没有 csv，或 inspection 覆盖不完整。
    """
    inspection = _load_optional_source_inspection(work_dir, source_inspection)
    if inspection is not None:
        _validate_readable_sheet_skip_reasons(inspection)
    csv_names = list_cleaned_csvs(work_dir)
    if not csv_names:
        skipped_sheets = (
            _explicitly_skipped_source_sheets(inspection)
            if inspection is not None
            else []
        )
        if skipped_sheets and inspection is not None:
            inspection_path = os.path.join(work_dir, SOURCE_INSPECTION_FILENAME)
            inspection_hash_info = _file_hash(inspection_path)
            inspection_file_sha256 = (
                inspection_hash_info[1] if inspection_hash_info else None
            )
            return {
                "version": CONTRACT_VERSION,
                "status": "skipped",
                "data_status": "no_data_skipped",
                "tables": [],
                "skipped_sheets": _skipped_sheet_records(skipped_sheets),
                "cleaning_notes": truncate_cleaning_notes(cleaning_notes),
                "source_inspection": {
                    "path": SOURCE_INSPECTION_FILENAME,
                    "version": inspection.get("version"),
                    "task_id": inspection.get("task_id"),
                    "sha256": inspection_file_sha256,
                },
            }
        if inspection is not None:
            _validate_readable_sheet_coverage(inspection, [])
        raise DataContractError(f"{CLEANED_DIR_NAME}/ 中没有 csv，数据准备未产出清洗表")
    inspection_path = os.path.join(work_dir, SOURCE_INSPECTION_FILENAME)
    inspection_hash_info = _file_hash(inspection_path) if inspection else None
    inspection_file_sha256 = (
        inspection_hash_info[1] if inspection_hash_info else None
    )
    tables = [
        profile_cleaned_csv(work_dir, name, source_inspection=inspection)
        for name in csv_names
    ]
    for table in tables:
        table.update(
            _build_table_provenance(
                work_dir,
                table,
                cleaning_notes=cleaning_notes,
                source_inspection=inspection,
                inspection_file_sha256=inspection_file_sha256,
            )
        )
    if inspection is not None:
        _validate_readable_sheet_coverage(inspection, tables)

    contract: dict[str, Any] = {
        "version": CONTRACT_VERSION,
        "tables": tables,
        "cleaning_notes": truncate_cleaning_notes(cleaning_notes),
    }
    if inspection is not None:
        contract["source_inspection"] = {
            "path": SOURCE_INSPECTION_FILENAME,
            "version": inspection.get("version"),
            "task_id": inspection.get("task_id"),
            "sha256": (
                inspection_hash_info[1] if inspection_hash_info else None
            ),
        }
    return contract


def save_data_contract(work_dir: str, contract: dict[str, Any]) -> str:
    """把 contract 写到 work_dir/data_contract.json，返回绝对路径。"""
    path = os.path.join(work_dir, CONTRACT_FILENAME)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(contract, f, ensure_ascii=False, indent=2)
    return path


def load_data_contract(work_dir: str) -> dict[str, Any]:
    """读取已落盘的 data_contract.json。

    Raises:
        DataContractError: 文件不存在或 JSON 非法。
    """
    path = os.path.join(work_dir, CONTRACT_FILENAME)
    if not os.path.isfile(path):
        raise DataContractError(f"缺少 {CONTRACT_FILENAME}")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise DataContractError(f"{CONTRACT_FILENAME} 不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise DataContractError(f"{CONTRACT_FILENAME} 根节点必须是对象")
    return data


def _load_contract_source_inspection(
    work_dir: str, contract: dict[str, Any]
) -> dict[str, Any] | None:
    """读取 contract 声明的 inspection，并校验报告文件指纹。"""
    metadata = contract.get("source_inspection")
    if not metadata:
        return None
    if not isinstance(metadata, dict):
        raise DataContractError("data_contract.source_inspection 必须是对象")
    relpath = metadata.get("path") or SOURCE_INSPECTION_FILENAME
    path = os.path.join(work_dir, relpath)
    if not os.path.isfile(path):
        raise DataContractError(f"缺少源数据探查报告: {relpath}")
    try:
        with open(path, encoding="utf-8") as stream:
            inspection = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise DataContractError(f"源数据探查报告无法读取: {relpath}") from exc
    if not isinstance(inspection, dict):
        raise DataContractError("源数据探查报告根节点必须是对象")
    expected_hash = metadata.get("sha256")
    actual_hash_info = _file_hash(path)
    if expected_hash and (
        actual_hash_info is None or actual_hash_info[1] != expected_hash
    ):
        raise DataContractError(f"源数据探查报告 hash 不一致: {relpath}")
    return inspection


def validate_data_contract(
    work_dir: str, contract: dict[str, Any] | None = None
) -> None:
    """核对落盘 csv、来源条目、hash 和 contract 一致。

    Raises:
        DataContractError: 缺文件、行列、来源或 hash 对不上。
    """
    if contract is None:
        contract = load_data_contract(work_dir)
    inspection = _load_contract_source_inspection(work_dir, contract)
    if inspection is not None:
        _validate_readable_sheet_skip_reasons(inspection)
    tables = contract.get("tables")
    if not isinstance(tables, list):
        raise DataContractError("data_contract 中没有 tables")
    if not tables:
        if (
            contract.get("data_status") != "no_data_skipped"
            or inspection is None
            or list_cleaned_csvs(work_dir)
            or not _explicitly_skipped_source_sheets(inspection)
        ):
            raise DataContractError("data_contract 中没有 tables")
        skipped_records = contract.get("skipped_sheets")
        expected_records = _skipped_sheet_records(
            _explicitly_skipped_source_sheets(inspection)
        )
        if skipped_records != expected_records:
            raise DataContractError("no-data contract 的 skipped_sheets 与 inspection 不一致")
        _validate_readable_sheet_coverage(inspection, [])
        return
    covered_entry_ids: set[str] = set()
    for table in tables:
        relpath = table.get("path") or ""
        abs_path = os.path.join(work_dir, relpath)
        if not os.path.isfile(abs_path):
            raise DataContractError(f"缺少清洗表: {relpath}")
        df = pd.read_csv(abs_path)
        expected_rows = int(table.get("rows", -1))
        if len(df) != expected_rows:
            raise DataContractError(
                f"{relpath} 行数不一致：文件 {len(df)} 行，contract {expected_rows} 行"
            )
        expected_cols = [c["name"] for c in table.get("columns") or []]
        actual_cols = [str(c) for c in df.columns.tolist()]
        if actual_cols != expected_cols:
            raise DataContractError(
                f"{relpath} 列名不一致：文件 {actual_cols}，contract {expected_cols}"
            )

        declared_source: tuple[str, str] | None = None
        if inspection is not None:
            declared_entry = _declared_cleaned_entry(relpath, inspection)
            declared_source = (
                str(declared_entry[0].get("path") or ""),
                str(declared_entry[1].get("name") or DEFAULT_SHEET_NAME),
            )
            covered_entry_ids.add(_inspection_entry_id(*declared_entry))

        provenance = table.get("provenance")
        if not provenance:
            # version=1 的旧 contract 没有 provenance 时仍可按旧规则消费。
            continue
        if not isinstance(provenance, dict):
            raise DataContractError(f"{relpath} provenance 必须是对象")

        after = provenance.get("after") or table.get("cleaning_after")
        if after:
            if int(after.get("rows", -1)) != len(df):
                raise DataContractError(f"{relpath} provenance 清洗后行数不一致")
            after_cols = [
                str(name) for name in after.get("column_names") or []
            ]
            if after_cols and after_cols != actual_cols:
                raise DataContractError(
                    f"{relpath} provenance 清洗后列名不一致"
                )

        expected_cleaned_hash = (
            provenance.get("cleaned_sha256") or table.get("cleaned_sha256")
        )
        actual_cleaned_hash_info = _file_hash(abs_path)
        if expected_cleaned_hash and (
            actual_cleaned_hash_info is None
            or actual_cleaned_hash_info[1] != expected_cleaned_hash
        ):
            raise DataContractError(f"{relpath} 清洗文件 hash 不一致")

        source_file = (
            provenance.get("source_file")
            or table.get("source_file")
            or table.get("source")
        )
        source_sheet = (
            provenance.get("source_sheet")
            or table.get("source_sheet")
            or table.get("sheet")
            or DEFAULT_SHEET_NAME
        )
        expected_source_hash = (
            provenance.get("source_sha256") or table.get("source_sha256")
        )
        expected_entry_id = provenance.get("inspection_entry_id") or table.get(
            "inspection_entry_id"
        )

        if declared_source is not None and (
            str(source_file or "") != declared_source[0]
            or str(source_sheet) != declared_source[1]
        ):
            raise DataContractError(f"{relpath} provenance 与声明路径来源不一致")

        if inspection is not None:
            matched = _inspection_sheet(
                inspection,
                str(source_file or ""),
                str(source_sheet),
                inspection_entry_id=(
                    str(expected_entry_id) if expected_entry_id else None
                ),
                source_sha256=(
                    str(expected_source_hash) if expected_source_hash else None
                ),
            )
            if matched is None:
                raise DataContractError(
                    f"{relpath} 来源不存在于 source inspection: "
                    f"{source_file} / {source_sheet}"
                )
            file_entry, sheet = matched
            canonical_source_file = str(file_entry.get("path") or "")
            canonical_source_sheet = str(
                sheet.get("name") or DEFAULT_SHEET_NAME
            )
            if source_file and (
                str(source_file).replace("\\", "/") != canonical_source_file
                and Path(str(source_file)).name != Path(canonical_source_file).name
            ):
                raise DataContractError(f"{relpath} source 文件关联不一致")
            if source_sheet and source_sheet != canonical_source_sheet:
                raise DataContractError(f"{relpath} source sheet 关联不一致")
            source_file = canonical_source_file
            source_sheet = canonical_source_sheet
            if file_entry.get("role") == "result_template":
                raise DataContractError(f"{relpath} 来源不能是 result 模板")
            if sheet.get("readable") is False or sheet.get("status") != "readable":
                raise DataContractError(
                    f"{relpath} 来源 sheet 不可读: {source_file} / {source_sheet}"
                )
            actual_entry_id = _inspection_entry_id(file_entry, sheet)
            if expected_entry_id and str(expected_entry_id) != actual_entry_id:
                raise DataContractError(f"{relpath} inspection entry 不一致")
            source_hash = file_entry.get("sha256")
            if source_hash and expected_source_hash != source_hash:
                raise DataContractError(f"{relpath} source inspection hash 不一致")
            before = provenance.get("before") or table.get("cleaning_before")
            if before:
                actual_before_columns = _column_names_from_inspection(sheet)
                if int(before.get("rows", -1)) != int(sheet.get("rows", 0)):
                    raise DataContractError(f"{relpath} 清洗前行数 provenance 不一致")
                if [
                    str(name) for name in before.get("column_names") or []
                ] != actual_before_columns:
                    raise DataContractError(f"{relpath} 清洗前列名 provenance 不一致")

        if source_file and (inspection is not None or expected_source_hash):
            source_path = os.path.join(work_dir, str(source_file))
            if not os.path.isfile(source_path):
                raise DataContractError(f"{relpath} 缺少源文件: {source_file}")
            actual_source_hash_info = _file_hash(source_path)
            if expected_source_hash and (
                actual_source_hash_info is None
                or actual_source_hash_info[1] != expected_source_hash
            ):
                raise DataContractError(f"{relpath} 源文件 hash 不一致")

        before = provenance.get("before") or table.get("cleaning_before")
        if before:
            row_changed = int(before.get("rows", len(df))) != len(df)
            column_changed = (
                int(before.get("columns", len(actual_cols))) != len(actual_cols)
                or [
                    str(name) for name in before.get("column_names") or []
                ]
                != actual_cols
            )
            actions = provenance.get("actions") or table.get("actions") or []
            limitations = (
                provenance.get("limitations")
                or table.get("limitations")
                or []
            )
            explanations = [*actions, *limitations]
            if row_changed and not _change_is_explained(
                explanations, _ROW_CHANGE_TERMS
            ):
                raise DataContractError(
                    f"{relpath} 清洗前后发生变化但缺少动作或限制说明（行数变化）"
                )
            if column_changed and not _change_is_explained(
                explanations, _COLUMN_CHANGE_TERMS
            ):
                raise DataContractError(
                    f"{relpath} 清洗前后发生变化但缺少动作或限制说明（列数变化）"
                )
    if inspection is not None:
        _validate_readable_sheet_coverage(
            inspection,
            [table for table in tables if isinstance(table, dict)],
            covered_entry_ids=covered_entry_ids,
        )


def _render_table(
    table: dict[str, Any],
    *,
    include_samples: bool,
    max_values: int,
) -> str:
    """渲染单张表的限长说明。"""
    cols = table.get("columns") or []
    col_bits: list[str] = []
    for col in cols:
        bit = f"{col['name']}({col.get('dtype', '?')}, 缺失{col.get('missing_pct', 0):.0%}"
        values = col.get("values") or []
        if max_values > 0 and values:
            shown = values[:max_values]
            extra = "…" if len(values) > max_values else ""
            bit += f", 取值[{', '.join(str(v) for v in shown)}{extra}]"
        elif col.get("nunique"):
            bit += f", {col['nunique']}种"
        bit += ")"
        col_bits.append(bit)
    lines = [
        f"表 {table.get('id', '')}  path={table.get('path', '')}",
        f"  来源: {table.get('source', '')} / sheet={table.get('sheet', '')}",
        f"  {table.get('rows', 0)} 行 × {len(cols)} 列",
        f"  列: {'; '.join(col_bits) if col_bits else '（无列）'}",
    ]
    provenance = table.get("provenance") or {}
    if provenance:
        before = provenance.get("before") or table.get("cleaning_before")
        after = provenance.get("after") or table.get("cleaning_after")
        if before or after:
            before_size = (
                f"{before.get('rows', '?')}x{before.get('columns', '?')}"
                if before
                else "unavailable"
            )
            after_size = (
                f"{after.get('rows', '?')}x{after.get('columns', '?')}"
                if after
                else "unavailable"
            )
            lines.append(f"  清洗变化: before={before_size} after={after_size}")
        lines.append(
            "  provenance: "
            f"inspection={provenance.get('inspection_entry_id', 'unavailable')}"
            f" source_sha256={provenance.get('source_sha256', 'unavailable')}"
            f" cleaned_sha256={provenance.get('cleaned_sha256', 'unavailable')}"
        )
        for field, label in (
            ("actions", "动作"),
            ("warnings", "警告"),
            ("limitations", "限制"),
        ):
            values = provenance.get(field) or table.get(field) or []
            if values:
                lines.append(f"  {label}: {'; '.join(str(value) for value in values)}")
    if include_samples:
        samples = table.get("sample_rows") or []
        if samples:
            preview = [
                " | ".join("" if c is None else str(c) for c in row) for row in samples
            ]
            lines.append("  sample:")
            lines.extend(f"    {row}" for row in preview)
    return "\n".join(lines)


def render_contract_for_llm(
    contract: dict[str, Any], budget: int = DEFAULT_RENDER_BUDGET
) -> str:
    """把 contract 渲成注入建模手 / 解题 Coder 的限长文本。

    超预算时先丢 sample，再截分类取值，最后硬截断。
    """
    notes = (contract.get("cleaning_notes") or "").strip()

    def _assemble(include_samples: bool, max_values: int) -> str:
        tables = contract.get("tables") or []
        if contract.get("data_status") == "no_data_skipped":
            chunks = [
                (
                    f"- {record.get('source_file', '')} / "
                    f"{record.get('source_sheet', '')}: "
                    f"skip={record.get('skip_reason', '')}"
                )
                for record in contract.get("skipped_sheets") or []
            ]
            body = "无可用清洗数据表；已明确跳过以下 source sheet：\n"
            body += "\n".join(chunks)
            if notes:
                body += f"\n\n清洗摘要:\n{notes}"
            return body
        chunks = [
            _render_table(t, include_samples=include_samples, max_values=max_values)
            for t in tables
        ]
        header = "已清洗数据表（只许用下列 path，禁止编造列名）:\n"
        body = header + "\n\n".join(chunks)
        if notes:
            body += f"\n\n清洗摘要:\n{notes}"
        return body

    text = _assemble(include_samples=True, max_values=MAX_CATEGORY_VALUES)
    if len(text) <= budget:
        return text
    text = _assemble(include_samples=False, max_values=MAX_CATEGORY_VALUES)
    if len(text) <= budget:
        return text
    for max_values in (8, 3, 0):
        text = _assemble(include_samples=False, max_values=max_values)
        if len(text) <= budget:
            return text
    if budget <= 8:
        return text[:budget]
    return text[: budget - 8] + "\n…（已截断）"


def render_contract_summary_for_writer(contract: dict[str, Any]) -> str:
    """Writer 预处理章用的短清单：path / 行数 / 列名 + 清洗摘要，不含 sample。"""
    if contract.get("data_status") == "no_data_skipped":
        lines = ["数据表清单: 无可用清洗数据表"]
        for record in contract.get("skipped_sheets") or []:
            lines.append(
                f"- 跳过 {record.get('source_file', '')} / "
                f"{record.get('source_sheet', '')}: "
                f"{record.get('skip_reason', '')}"
            )
        notes = (contract.get("cleaning_notes") or "").strip()
        if notes:
            lines.extend(["", "清洗摘要:", notes])
        return "\n".join(lines)
    lines: list[str] = ["数据表清单:"]
    for table in contract.get("tables") or []:
        cols = ", ".join(c["name"] for c in (table.get("columns") or []))
        n_cols = len(table.get("columns") or [])
        lines.append(
            f"- {table.get('path', '')}: {table.get('rows', 0)} 行 × {n_cols} 列；列: {cols}"
        )
    notes = (contract.get("cleaning_notes") or "").strip()
    if notes:
        lines.append("")
        lines.append("清洗摘要:")
        lines.append(notes)
    return "\n".join(lines)
