"""只读扫描任务附件，构造 Modeler 可验证的数据目录。"""

from __future__ import annotations

import csv
import hashlib
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import pandas as pd

from app.domain.m15 import FileFact, TaskFacts
from app.domain.problem import DataCatalog, DataColumn, DataTable

_SUPPORTED_DATA_SUFFIXES = {".csv", ".xlsx"}
_OUTPUT_TEMPLATE_PATTERN = re.compile(r"^result.*\.(?:csv|xlsx)$", re.IGNORECASE)
_CSV_ENCODINGS = ("utf-8-sig", "gb18030")
_EMPTY_PANDAS_HEADER_PATTERN = re.compile(r"^Unnamed: \d+(?:_level_\d+)?$")
_QUESTION_HEADING_PATTERN = re.compile(
    r"(?m)(?:^[ \t]*(?:#{1,6}[ \t]+)?|(?<=[。！？!?；;])[ \t]*)"
    r"(?:问题[ \t]*([1-9][0-9]*)|第[ \t]*([1-9][0-9]*)[ \t]*问)"
    r"(?=[ \t]*[:：、.．]|[ \t]+)"
)
TASK_FACTS_ARTIFACT_ID = "task-facts:root"

CatalogFailureKind = Literal[
    "read_failure",
    "empty_header",
    "column_conflict",
]


class DataCatalogInspectionError(RuntimeError):
    """附件表头无法被可靠读取。"""

    def __init__(
        self,
        path: Path,
        reason: str,
        *,
        failure_kind: CatalogFailureKind = "read_failure",
        table_id: str | None = None,
    ) -> None:
        self.path = path
        self.reason = reason
        self.failure_kind = failure_kind
        self.table_id = table_id or path.name
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
        suffix = path.suffix.lower()
        if suffix not in _SUPPORTED_DATA_SUFFIXES:
            continue
        if path.is_symlink():
            raise DataCatalogInspectionError(path, "受支持附件不能是符号链接")
        if not path.is_file():
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


def build_task_facts(
    *,
    task_id: str,
    problem_text: str,
    work_dir: str | Path,
    data_catalog: DataCatalog,
) -> TaskFacts:
    """从 API 题目和只读附件发现结果构造任务事实。

    Args:
        task_id: API 请求中的任务标识。
        problem_text: API 请求中的完整题目原文。
        work_dir: 已完成附件复制的任务工作目录。
        data_catalog: `inspect_data_catalog` 的只读发现结果。

    Returns:
        带源文件 SHA-256 的只读 TaskFacts。

    Raises:
        DataCatalogInspectionError: 已发现文件缺失、变为符号链接或无法读取。
    """
    root = Path(work_dir)
    if not root.is_dir():
        raise DataCatalogInspectionError(root, "工作目录不存在")

    input_paths = sorted({table.file for table in data_catalog.input_tables})
    template_paths = sorted(data_catalog.output_templates)
    attachments = tuple(
        _build_file_fact(root, relative_path) for relative_path in input_paths
    )
    output_templates = tuple(
        _build_file_fact(root, relative_path) for relative_path in template_paths
    )
    expected_question_ids = extract_expected_question_ids(problem_text)
    return TaskFacts(
        schema_version="m1.5",
        artifact_id=TASK_FACTS_ARTIFACT_ID,
        source_artifact_ids=(),
        validation_status="validated",
        artifact_path="m15/task_facts.json",
        task_id=task_id,
        problem_text=problem_text,
        expected_question_ids=expected_question_ids,
        expected_question_count=(
            len(expected_question_ids) if expected_question_ids is not None else None
        ),
        attachments=attachments,
        output_templates=output_templates,
    )


def extract_expected_question_ids(problem_text: str) -> tuple[str, ...] | None:
    """从明确编号题头提取连续问题边界。

    仅识别行首题头，或位于句界后的带编号题头。提取结果必须无重复并严格按
    1..N 出现；否则返回 ``None``，由 Coordinator 在调用模型前停止。

    Args:
        problem_text: API 请求中的完整题目原文。

    Returns:
        连续的 ``ques1..quesN``；无法可靠确定时返回 ``None``。
    """
    numbers = tuple(
        int(match.group(1) or match.group(2))
        for match in _QUESTION_HEADING_PATTERN.finditer(problem_text)
    )
    if not numbers or numbers != tuple(range(1, len(numbers) + 1)):
        return None
    return tuple(f"ques{number}" for number in numbers)


def _build_file_fact(root: Path, relative_path: str) -> FileFact:
    """读取一个已发现文件的稳定指纹，不修改源文件。"""
    path = root / relative_path
    try:
        sha256 = compute_file_sha256(path)
    except OSError as exc:
        raise DataCatalogInspectionError(path, str(exc)) from exc
    return FileFact(path=relative_path, sha256=sha256)


def compute_file_sha256(path: str | Path) -> str:
    """只读计算普通文件的 SHA-256，并拒绝符号链接。"""
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise OSError(f"源文件不存在或不是普通文件: {source.name}")

    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inspect_csv(path: Path) -> DataTable:
    """读取 CSV 表头，不读取数据行。"""
    last_error: UnicodeDecodeError | None = None
    for encoding in _CSV_ENCODINGS:
        try:
            # pandas 会自动改写重复表头；原始首行用于保留并校验真实名称。
            pd.read_csv(path, nrows=0, encoding=encoding)
            with path.open(encoding=encoding, newline="") as stream:
                raw_columns = next(row for row in csv.reader(stream) if row)
            return _build_table(path, None, raw_columns)
        except pd.errors.EmptyDataError as exc:
            raise DataCatalogInspectionError(
                path,
                "文件没有表头",
                failure_kind="empty_header",
            ) from exc
        except UnicodeDecodeError as exc:
            last_error = exc
        except StopIteration as exc:
            raise DataCatalogInspectionError(
                path,
                "文件没有表头",
                failure_kind="empty_header",
            ) from exc

    reason = str(last_error) if last_error is not None else "未知编码错误"
    raise DataCatalogInspectionError(path, reason)


def _inspect_xlsx(path: Path) -> list[DataTable]:
    """读取 Excel 中每个 sheet 的表头，不读取数据行。"""
    tables: list[DataTable] = []
    with pd.ExcelFile(path) as workbook:
        for sheet_name in workbook.sheet_names:
            frame = pd.read_excel(
                workbook,
                sheet_name=sheet_name,
                header=None,
                nrows=1,
            )
            raw_columns = frame.iloc[0].tolist() if not frame.empty else []
            tables.append(_build_table(path, sheet_name, raw_columns))
    return tables


def _build_table(
    path: Path,
    sheet_name: str | None,
    columns: Sequence[object],
) -> DataTable:
    """从 pandas 表头建立保留原始名称的领域表。"""
    table_id = (
        f"{path.name}::{sheet_name.strip()}" if sheet_name is not None else path.name
    )
    if not columns:
        raise DataCatalogInspectionError(
            path,
            f"{table_id} 没有可用列",
            failure_kind="empty_header",
            table_id=table_id,
        )

    source_names = tuple(str(column) for column in columns)
    if any(
        pd.isna(column)
        or not source_name.strip()
        or _EMPTY_PANDAS_HEADER_PATTERN.fullmatch(source_name)
        for column, source_name in zip(columns, source_names, strict=True)
    ):
        raise DataCatalogInspectionError(
            path,
            f"{table_id} 包含空表头",
            failure_kind="empty_header",
            table_id=table_id,
        )

    normalized_names = tuple(source_name.strip() for source_name in source_names)
    if len(set(normalized_names)) != len(normalized_names):
        raise DataCatalogInspectionError(
            path,
            f"{table_id} 的规范列名不能重复",
            failure_kind="column_conflict",
            table_id=table_id,
        )

    data_columns = tuple(
        DataColumn(name=name, source_name=source_name)
        for name, source_name in zip(
            normalized_names,
            source_names,
            strict=True,
        )
    )
    return DataTable(
        table_id=table_id,
        file=path.name,
        sheet=sheet_name,
        columns=data_columns,
    )
