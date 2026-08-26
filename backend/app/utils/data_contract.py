"""清洗产物口径：cleaned/ 一表一 csv，以及 data_contract.json 的构建、校验与限长渲染。

后端只认 cleaned/*.csv，不扫整个 work_dir 的原始附件。
磁盘上的 JSON 是校验真源；进 LLM 的是限长渲染文本。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import pandas as pd

CLEANED_DIR_NAME = "cleaned"
CONTRACT_FILENAME = "data_contract.json"
CONTRACT_VERSION = 1
DEFAULT_SHEET_NAME = "Sheet1"
MAX_SAMPLE_ROWS = 3
MAX_CATEGORY_VALUES = 20
MAX_CLEANING_NOTES_CHARS = 1500
DEFAULT_RENDER_BUDGET = 8000

_UNSAFE_CHARS = re.compile(r'[/\\:*?"<>|]')
_WHITESPACE = re.compile(r"\s+")


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


def cleaned_relpath_for(source_filename: str, sheet: str) -> str:
    """按口径生成清洗 csv 的相对路径：cleaned/{主名}__{sheet}.csv。"""
    stem = sanitize_filename_token(Path(source_filename).stem)
    sheet_token = sanitize_filename_token(sheet or DEFAULT_SHEET_NAME)
    return f"{CLEANED_DIR_NAME}/{stem}__{sheet_token}.csv"


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
    """数据准备阶段注入 Coder 的口径说明（此时还没有 contract）。"""
    files_line = ", ".join(source_files) if source_files else "（无）"
    return (
        "原始数据文件（只读，禁止覆盖、禁止改名、禁止挪走）："
        f"{files_line}\n"
        "答题模板 result*.xlsx / result*.csv 忽略，不要清洗、不要改名。\n\n"
        "清洗输出口径（必须遵守）：\n"
        f"- 目录：{CLEANED_DIR_NAME}/\n"
        "- 格式：UTF-8 csv\n"
        "- xlsx 多 sheet → 一 sheet 一文件；禁止把两张表拼成一个文件\n"
        f"- 文件名：{{附件主名}}__{{sheet名}}.csv ，例如 {CLEANED_DIR_NAME}/附件1__乡村现有耕地.csv\n"
        f"- 单 sheet 或原来就是 csv：sheet 名用 {DEFAULT_SHEET_NAME}\n"
        '- sheet 名去掉 / \\ : * ? " < > | ，空白改下划线\n'
        f"- 完成后 {CLEANED_DIR_NAME}/ 至少 1 个 csv\n"
        "- 本阶段不要画论文图，不要改 result 模板"
    )


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
    return stem


def profile_cleaned_csv(work_dir: str, filename: str) -> dict[str, Any]:
    """读取一张 cleaned csv，生成 contract 中的 table 条目。"""
    abs_path = os.path.join(cleaned_dir(work_dir), filename)
    df = pd.read_csv(abs_path)
    source_stem, sheet = _parse_cleaned_filename(filename)
    relpath = f"{CLEANED_DIR_NAME}/{filename}".replace("\\", "/")
    return {
        "id": f"{source_stem}__{sheet}",
        "path": relpath,
        "source": infer_source_filename(work_dir, source_stem),
        "sheet": sheet,
        "rows": int(len(df)),
        "columns": [_infer_column_meta(df.iloc[:, i]) for i in range(df.shape[1])],
        "sample_rows": _sample_rows(df),
    }


def build_data_contract(work_dir: str, cleaning_notes: str = "") -> dict[str, Any]:
    """根据 cleaned/*.csv 构建 data_contract。目录为空则抛错。

    Args:
        work_dir: 任务工作目录。
        cleaning_notes: Coder 收工摘要，写入顶层 notes。

    Returns:
        contract 字典。

    Raises:
        DataContractError: cleaned/ 不存在或没有任何 csv。
    """
    csv_names = list_cleaned_csvs(work_dir)
    if not csv_names:
        raise DataContractError(f"{CLEANED_DIR_NAME}/ 中没有 csv，数据准备未产出清洗表")
    tables = [profile_cleaned_csv(work_dir, name) for name in csv_names]
    return {
        "version": CONTRACT_VERSION,
        "tables": tables,
        "cleaning_notes": truncate_cleaning_notes(cleaning_notes),
    }


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


def validate_data_contract(
    work_dir: str, contract: dict[str, Any] | None = None
) -> None:
    """核对落盘 csv 的行数、列名与 contract 一致。

    Raises:
        DataContractError: 缺文件、行列对不上。
    """
    if contract is None:
        contract = load_data_contract(work_dir)
    tables = contract.get("tables")
    if not tables:
        raise DataContractError("data_contract 中没有 tables")
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
    return text[: budget - 8] + "\n…（已截断）"


def render_contract_summary_for_writer(contract: dict[str, Any]) -> str:
    """Writer 预处理章用的短清单：path / 行数 / 列名 + 清洗摘要，不含 sample。"""
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
