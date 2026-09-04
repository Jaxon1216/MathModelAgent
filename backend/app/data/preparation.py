"""只读扫描任务附件，构造 Modeler 可验证的数据目录。"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from app.domain.problem import DataCatalog, DataColumn, DataTable

_SUPPORTED_DATA_SUFFIXES = {".csv", ".xlsx"}
_OUTPUT_TEMPLATE_PATTERN = re.compile(r"^result.*\.(?:csv|xlsx)$", re.IGNORECASE)
_CSV_ENCODINGS = ("utf-8-sig", "gb18030")


class DataCatalogInspectionError(RuntimeError):
    """附件表头无法被可靠读取。"""

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"无法读取数据目录 {path.name}: {reason}")


def inspect_data_catalog(work_dir: str | Path) -> DataCatalog:
    """只读扫描工作目录下的 CSV/XLSX 表头。

    `result*.csv/xlsx` 只登记为输出模板，绝不作为 Modeler 输入数据。
    字体、图片、生成物和子目录均不参与扫描。

    Args:
        work_dir: 当前任务工作目录。

    Returns:
        按文件名、sheet 名稳定排序的数据目录。

    Raises:
        DataCatalogInspectionError: 支持的数据文件存在但表头不可读取。
    """
    root = Path(work_dir)
    if not root.is_dir():
        raise DataCatalogInspectionError(root, "工作目录不存在")

    input_tables: list[DataTable] = []
    output_templates: list[str] = []
    candidates = sorted(root.iterdir(), key=lambda path: path.name)

    for path in candidates:
        if path.is_symlink() or not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in _SUPPORTED_DATA_SUFFIXES:
            continue
        if _OUTPUT_TEMPLATE_PATTERN.fullmatch(path.name):
            output_templates.append(path.name)
            continue

        try:
            if suffix == ".csv":
                input_tables.append(_inspect_csv(path))
            else:
                input_tables.extend(_inspect_xlsx(path))
        except DataCatalogInspectionError:
            raise
        except Exception as exc:
            raise DataCatalogInspectionError(path, str(exc)) from exc

    try:
        return DataCatalog(
            input_tables=tuple(input_tables),
            output_templates=tuple(output_templates),
        )
    except Exception as exc:
        raise DataCatalogInspectionError(root, str(exc)) from exc


def _inspect_csv(path: Path) -> DataTable:
    """读取 CSV 表头，不读取数据行。"""
    last_error: UnicodeDecodeError | None = None
    for encoding in _CSV_ENCODINGS:
        try:
            frame = pd.read_csv(path, nrows=0, encoding=encoding)
            return _build_table(path.name, None, frame.columns)
        except UnicodeDecodeError as exc:
            last_error = exc

    reason = str(last_error) if last_error is not None else "未知编码错误"
    raise DataCatalogInspectionError(path, reason)


def _inspect_xlsx(path: Path) -> list[DataTable]:
    """读取 Excel 中每个 sheet 的表头，不读取数据行。"""
    tables: list[DataTable] = []
    with pd.ExcelFile(path) as workbook:
        for sheet_name in workbook.sheet_names:
            frame = pd.read_excel(workbook, sheet_name=sheet_name, nrows=0)
            tables.append(_build_table(path.name, sheet_name, frame.columns))
    return tables


def _build_table(
    filename: str,
    sheet_name: str | None,
    columns: pd.Index,
) -> DataTable:
    """从 pandas 表头建立保留原始名称的领域表。"""
    data_columns = tuple(
        DataColumn(name=str(column).strip(), source_name=str(column))
        for column in columns
    )
    if not data_columns:
        location = f"{filename}::{sheet_name}" if sheet_name is not None else filename
        raise ValueError(f"{location} 没有可用列")

    table_id = (
        f"{filename}::{sheet_name.strip()}" if sheet_name is not None else filename
    )
    return DataTable(
        table_id=table_id,
        file=filename,
        sheet=sheet_name,
        columns=data_columns,
    )
