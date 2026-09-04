"""Modeler 阶段使用的纯题目与数据事实契约。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DataCatalog(BaseModel):
    """可由 Modeler 引用的已验证数据事实。

    该对象由编排层或后续数据准备阶段提供；领域层不读取文件系统。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    files: tuple[str, ...] = ()
    columns_by_file: dict[str, tuple[str, ...]] = Field(default_factory=dict)

    @field_validator("files")
    @classmethod
    def validate_files(cls, files: tuple[str, ...]) -> tuple[str, ...]:
        """规范化并校验文件名集合。"""
        normalized = tuple(name.strip() for name in files)
        if any(not name for name in normalized):
            raise ValueError("数据文件名不能为空")
        if len(set(normalized)) != len(normalized):
            raise ValueError("数据文件名不能重复")
        return normalized

    @field_validator("columns_by_file")
    @classmethod
    def validate_columns(
        cls, columns_by_file: dict[str, tuple[str, ...]]
    ) -> dict[str, tuple[str, ...]]:
        """规范化并校验列名集合。"""
        normalized: dict[str, tuple[str, ...]] = {}
        for filename, columns in columns_by_file.items():
            clean_filename = filename.strip()
            clean_columns = tuple(column.strip() for column in columns)
            if not clean_filename:
                raise ValueError("数据文件名不能为空")
            if any(not column for column in clean_columns):
                raise ValueError(f"{clean_filename} 包含空列名")
            if len(set(clean_columns)) != len(clean_columns):
                raise ValueError(f"{clean_filename} 的列名不能重复")
            normalized[clean_filename] = clean_columns
        return normalized

    @model_validator(mode="after")
    def validate_column_files(self) -> DataCatalog:
        """确保列信息只属于已声明文件。"""
        unknown_files = set(self.columns_by_file) - set(self.files)
        if unknown_files:
            raise ValueError(
                f"列信息引用了未声明文件: {', '.join(sorted(unknown_files))}"
            )
        return self


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
