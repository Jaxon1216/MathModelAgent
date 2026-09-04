"""Modeler 阶段使用的纯题目与数据事实契约。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DataColumn(BaseModel):
    """数据表中的规范列名及其原始表头。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    source_name: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, name: str) -> str:
        """规范列名不允许首尾空白。"""
        normalized = name.strip()
        if not normalized:
            raise ValueError("规范列名不能为空")
        return normalized

    @field_validator("source_name")
    @classmethod
    def validate_source_name(cls, source_name: str) -> str:
        """保留原始表头，包括有意义的首尾空白。"""
        if not source_name.strip():
            raise ValueError("原始列名不能为空")
        return source_name

    @model_validator(mode="after")
    def validate_normalized_name(self) -> DataColumn:
        """规范名必须是原始表头去除首尾空白后的结果。"""
        if self.name != self.source_name.strip():
            raise ValueError("规范列名必须等于原始表头的 strip 结果")
        return self


class DataTable(BaseModel):
    """一个可被 Modeler 引用的 CSV 或 Excel sheet。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    table_id: str = Field(min_length=1)
    file: str = Field(min_length=1)
    sheet: str | None = None
    columns: tuple[DataColumn, ...] = Field(min_length=1)

    @field_validator("table_id", "file")
    @classmethod
    def normalize_identifier(cls, value: str) -> str:
        """表标识和文件名不允许首尾空白。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("表标识和文件名不能为空")
        return normalized

    @field_validator("sheet")
    @classmethod
    def validate_sheet(cls, sheet: str | None) -> str | None:
        """保留 Excel 原始 sheet 名；CSV 使用 None。"""
        if sheet is not None and not sheet.strip():
            raise ValueError("sheet 名不能为空")
        return sheet

    @model_validator(mode="after")
    def validate_columns(self) -> DataTable:
        """表标识可重建，且同一表的规范列名必须唯一。"""
        expected_table_id = (
            self.file if self.sheet is None else f"{self.file}::{self.sheet.strip()}"
        )
        if self.table_id != expected_table_id:
            raise ValueError(f"table_id 必须等于文件与 sheet 组合: {expected_table_id}")
        names = [column.name for column in self.columns]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.table_id} 的规范列名不能重复")
        return self


class DataCatalog(BaseModel):
    """可由 Modeler 引用的输入表与只可写入的结果模板。

    该对象由数据准备层提供；领域层不读取文件系统。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tables: tuple[DataTable, ...] = ()
    output_templates: tuple[str, ...] = ()

    @field_validator("output_templates")
    @classmethod
    def validate_output_templates(cls, templates: tuple[str, ...]) -> tuple[str, ...]:
        """规范化并校验结果模板文件名。"""
        normalized = tuple(template.strip() for template in templates)
        if any(not template for template in normalized):
            raise ValueError("结果模板文件名不能为空")
        if len(set(normalized)) != len(normalized):
            raise ValueError("结果模板文件名不能重复")
        return normalized

    @model_validator(mode="after")
    def validate_tables(self) -> DataCatalog:
        """表标识唯一，且输入文件不能同时被标为结果模板。"""
        table_ids = [table.table_id for table in self.input_tables]
        if len(set(table_ids)) != len(table_ids):
            raise ValueError("table_id 不能重复")
        input_files = {table.file for table in self.input_tables}
        overlap = input_files & set(self.output_templates)
        if overlap:
            raise ValueError(
                f"文件不能同时作为输入和结果模板: {', '.join(sorted(overlap))}"
            )
        return self

    def get_table(self, table_id: str) -> DataTable | None:
        """按稳定标识查找输入表。"""
        return next(
            (table for table in self.input_tables if table.table_id == table_id),
            None,
        )


class QuestionSet(BaseModel):
    """协调者拆解后的公开问题集合。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    question_count: int = Field(ge=1)
    questions: dict[str, str]
    background: str = ""

    @field_validator("questions")
    @classmethod
    def validate_questions(cls, questions: dict[str, str]) -> dict[str, str]:
        """去除题目文本首尾空白并拒绝空问题。"""
        normalized = {key.strip(): value.strip() for key, value in questions.items()}
        if any(not key or not value for key, value in normalized.items()):
            raise ValueError("问题编号和题目文本不能为空")
        return normalized

    @model_validator(mode="after")
    def validate_question_keys(self) -> QuestionSet:
        """要求问题键严格覆盖 ques1 到 quesN。"""
        expected = {f"ques{index}" for index in range(1, self.question_count + 1)}
        actual = set(self.questions)
        if actual != expected:
            raise ValueError(
                "问题编号必须严格匹配: "
                f"expected={sorted(expected)}, actual={sorted(actual)}"
            )
        return self

    @classmethod
    def from_coordinator(
        cls,
        questions: Mapping[str, Any],
        question_count: int,
    ) -> QuestionSet:
        """从旧 Coordinator 的输出建立领域问题集合。

        Args:
            questions: Coordinator 的扁平题目字典。
            question_count: Coordinator 声明的问题数量。

        Returns:
            仅包含公开问题与背景的领域契约。
        """
        question_map = {
            f"ques{index}": questions.get(f"ques{index}", "")
            for index in range(1, question_count + 1)
        }
        if any(not isinstance(value, str) for value in question_map.values()):
            raise ValueError("Coordinator 必须为每个 quesN 提供字符串题目")
        background = questions.get("background", "")
        if not isinstance(background, str):
            raise ValueError("Coordinator 的 background 必须为字符串")
        return cls(
            question_count=question_count,
            questions=question_map,
            background=background.strip(),
        )

    def to_prompt_payload(self) -> dict[str, Any]:
        """渲染给 Modeler 的公开题目事实。"""
        return {
            "background": self.background,
            "question_count": self.question_count,
            "questions": self.questions,
        }


class Problem(BaseModel):
    """Modeler 阶段的完整领域输入。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1)
    question_set: QuestionSet
    data_catalog: DataCatalog = Field(default_factory=DataCatalog)

    @field_validator("task_id")
    @classmethod
    def validate_task_id(cls, task_id: str) -> str:
        """拒绝空白任务标识。"""
        normalized = task_id.strip()
        if not normalized:
            raise ValueError("task_id 不能为空")
        return normalized

    def to_prompt_payload(self) -> dict[str, Any]:
        """渲染给 Modeler 的全部可验证事实。"""
        return {
            "task_id": self.task_id,
            "question_set": self.question_set.to_prompt_payload(),
            "data_catalog": self.data_catalog.model_dump(mode="json"),
        }
