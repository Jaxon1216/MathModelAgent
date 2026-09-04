"""Modeler 计划契约与纯校验逻辑。"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.domain.problem import DataCatalog, Problem


class DataReference(BaseModel):
    """计划中对已验证数据表和列的引用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    file: str = Field(min_length=1)
    columns: tuple[str, ...] = ()

    @field_validator("file")
    @classmethod
    def normalize_file(cls, filename: str) -> str:
        """规范化文件名。"""
        return filename.strip()

    @field_validator("columns")
    @classmethod
    def normalize_columns(cls, columns: tuple[str, ...]) -> tuple[str, ...]:
        """规范化列名。"""
        normalized = tuple(column.strip() for column in columns)
        if any(not column for column in normalized):
            raise ValueError("列名不能为空")
        if len(set(normalized)) != len(normalized):
            raise ValueError("同一文件的列名不能重复")
        return normalized


class PlanSection(BaseModel):
    """一个问题或敏感性分析的可执行计划。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    data: tuple[DataReference, ...] = ()
    objective: str = Field(min_length=1, max_length=1200)
    model: str = Field(min_length=1, max_length=1200)
    method: str = Field(min_length=1, max_length=1200)
    constraints: tuple[str, ...] = ()
    validation: tuple[str, ...] = Field(min_length=1)
    figures: tuple[str, ...] = Field(min_length=1)
    fallback: str = Field(min_length=1, max_length=1200)

    @field_validator("objective", "model", "method", "fallback")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        """拒绝只含空白的必填文本。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("必填计划文本不能为空")
        return normalized

    @field_validator("constraints", "validation", "figures")
    @classmethod
    def strip_text_items(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """拒绝空白约束、验证项和图表项。"""
        normalized = tuple(value.strip() for value in values)
        if any(not value for value in normalized):
            raise ValueError("计划列表不能包含空白项")
        return normalized


class ModelPlan(BaseModel):
    """完整的版本化 Modeler 输出。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["m1"]
    question_plans: dict[str, PlanSection]
    sensitivity_analysis: PlanSection

    def to_coder_handoff(self) -> dict[str, str]:
        """将结构化计划降级为旧 Coder 可消费的文本交接。

        Returns:
            旧 `Flows` 所需的 `quesN` 和 `sensitivity_analysis` 键值对。
        """
        return {
            **{
                question_key: _render_section(section)
                for question_key, section in self.question_plans.items()
            },
            "sensitivity_analysis": _render_section(self.sensitivity_analysis),
        }


class ModelPlanValidationError(ValueError):
    """模型输出不满足领域契约。"""

    def __init__(self, errors: tuple[str, ...]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


class PlanValidation(BaseModel):
    """将原始 JSON 校验为针对特定问题和数据目录的计划。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    question_ids: tuple[str, ...]
    data_catalog: DataCatalog

    @classmethod
    def for_problem(cls, problem: Problem) -> PlanValidation:
        """从领域问题构造计划校验器。"""
        return cls(
            question_ids=tuple(problem.question_set.questions),
            data_catalog=problem.data_catalog,
        )

    def parse_json(self, raw_json: str) -> ModelPlan:
        """解析并校验模型返回的 JSON 文本。

        Args:
            raw_json: 模型返回的纯 JSON 文本。

        Returns:
            覆盖全部问题且只引用已验证数据的计划。

        Raises:
            ModelPlanValidationError: JSON、Pydantic 或领域引用校验失败。
        """
        try:
            payload: Any = json.loads(_strip_code_fence(raw_json))
        except json.JSONDecodeError as exc:
            raise ModelPlanValidationError((f"invalid_json: {exc.msg}",)) from exc

        try:
            plan = ModelPlan.model_validate(payload)
        except ValidationError as exc:
            errors = tuple(
                f"schema.{'.'.join(str(part) for part in error['loc'])}: "
                f"{error['msg']}"
                for error in exc.errors()
            )
            raise ModelPlanValidationError(errors) from exc

        expected_ids = set(self.question_ids)
        actual_ids = set(plan.question_plans)
        if actual_ids != expected_ids:
            raise ModelPlanValidationError(
                (
                    "question_keys_mismatch: "
                    f"expected={sorted(expected_ids)}, actual={sorted(actual_ids)}",
                )
            )

        self._validate_data_references(plan)
        return plan

    def _validate_data_references(self, plan: ModelPlan) -> None:
        """校验计划引用的数据文件和列都来自领域数据目录。"""
        sections = [*plan.question_plans.values(), plan.sensitivity_analysis]
        for section in sections:
            for reference in section.data:
                self._validate_reference(reference)

    def _validate_reference(self, reference: DataReference) -> None:
        """校验单条数据引用。"""
        allowed_files = set(self.data_catalog.files)
        if reference.file not in allowed_files:
            raise ModelPlanValidationError(
                (f"unknown_data_file: {reference.file}",)
            )

        allowed_columns = set(self.data_catalog.columns_by_file.get(reference.file, ()))
        unknown_columns = set(reference.columns) - allowed_columns
        if unknown_columns:
            raise ModelPlanValidationError(
                (
                    f"unknown_data_columns: file={reference.file}, "
                    f"columns={sorted(unknown_columns)}",
                )
            )


def _strip_code_fence(raw_json: str) -> str:
    """仅兼容一层 Markdown JSON fence，不做猜测式 JSON 修复。"""
    stripped = raw_json.strip()
    if stripped.startswith("```json") and stripped.endswith("```"):
        return stripped[7:-3].strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        return stripped[3:-3].strip()
    return stripped


def _render_section(section: PlanSection) -> str:
    """将一个领域计划段渲染为旧 Coder 的紧凑文本。"""
    if section.data:
        data_text = "；".join(
            f"{reference.file}({', '.join(reference.columns) or '全部已验证列'})"
            for reference in section.data
        )
    else:
        data_text = "未引用已验证数据表；先按题目事实和实际数据确认输入。"

    constraints = "；".join(section.constraints) or "按题目与数据事实确定。"
    return "\n".join(
        [
            f"目标：{section.objective}",
            f"数据：{data_text}",
            f"模型：{section.model}",
            f"方法：{section.method}",
            f"约束：{constraints}",
            f"验证：{'；'.join(section.validation)}",
            f"图表：{'；'.join(section.figures)}",
            f"回退：{section.fallback}",
        ]
    )
