"""Modeler 计划契约与纯校验逻辑。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from app.domain.m15 import (
    ContractDataReference,
    DataContract,
    Deliverable,
    QuestionId,
    QuestionPlan,
    TaskOutline,
    UpstreamResultReference,
)
from app.domain.problem import DataCatalog, Problem

MAX_CODER_HANDOFF_CHARS = 8_000
MAX_PLAN_LIST_ITEM_CHARS = 300


class DataReference(BaseModel):
    """计划中对已验证数据表和列的引用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    table_id: str = Field(min_length=1)
    columns: tuple[str, ...] = Field(min_length=1)

    @field_validator("table_id")
    @classmethod
    def normalize_table_id(cls, table_id: str) -> str:
        """规范化稳定表标识。"""
        return table_id.strip()

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
    constraints: tuple[str, ...] = Field(default=(), max_length=12)
    validation: tuple[str, ...] = Field(min_length=1, max_length=8)
    figures: tuple[str, ...] = Field(min_length=1, max_length=8)
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
        if any(len(value) > MAX_PLAN_LIST_ITEM_CHARS for value in normalized):
            raise ValueError(f"计划列表单项不能超过 {MAX_PLAN_LIST_ITEM_CHARS} 个字符")
        return normalized


class ModelPlan(BaseModel):
    """完整的版本化 Modeler 输出。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["m1"]
    question_plans: dict[str, PlanSection]
    sensitivity_analysis: PlanSection

    def to_coder_handoff(self, data_catalog: DataCatalog) -> dict[str, str]:
        """将结构化计划降级为旧 Coder 可消费的文本交接。

        Args:
            data_catalog: 用于将规范引用还原为真实文件、sheet 和原始表头。

        Returns:
            旧 `Flows` 所需的 `quesN` 和 `sensitivity_analysis` 键值对。
        """
        handoff = {
            **{
                question_key: _render_section(section, data_catalog)
                for question_key, section in self.question_plans.items()
            },
            "sensitivity_analysis": _render_section(
                self.sensitivity_analysis, data_catalog
            ),
        }
        oversized = {
            key: len(value)
            for key, value in handoff.items()
            if len(value) > MAX_CODER_HANDOFF_CHARS
        }
        if oversized:
            raise ModelPlanValidationError(
                (
                    "coder_handoff_too_long: "
                    + ", ".join(f"{key}={length}" for key, length in oversized.items()),
                )
            )
        return handoff


class ModelPlanValidationError(ValueError):
    """模型输出不满足领域契约。"""

    def __init__(self, errors: tuple[str, ...]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


class QuestionPlanValidationError(ModelPlanValidationError):
    """M1.5 QuestionPlan 集合不满足输入契约。"""


class QuestionPlanContent(BaseModel):
    """问题计划与兼容敏感性计划共享的有界内容。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    data: tuple[ContractDataReference, ...] = Field(default=(), max_length=24)
    objective: str = Field(min_length=1, max_length=1200)
    model: str = Field(min_length=1, max_length=1200)
    method: str = Field(min_length=1, max_length=1200)
    constraints: tuple[str, ...] = Field(default=(), max_length=12)
    validation: tuple[str, ...] = Field(min_length=1, max_length=8)
    figures: tuple[str, ...] = Field(min_length=1, max_length=8)
    fallback: str = Field(min_length=1, max_length=1200)

    @field_validator("objective", "model", "method", "fallback")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        """拒绝空白必填文本。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("必填计划文本不能为空")
        return normalized

    @field_validator("constraints", "validation", "figures")
    @classmethod
    def strip_text_items(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        """限制列表文本，避免向旧 Coder 传递无界内容。"""
        normalized = tuple(value.strip() for value in values)
        if any(not value for value in normalized):
            raise ValueError("计划列表不能包含空白项")
        if any(len(value) > MAX_PLAN_LIST_ITEM_CHARS for value in normalized):
            raise ValueError(f"计划列表单项不能超过 {MAX_PLAN_LIST_ITEM_CHARS} 个字符")
        return normalized

    @model_validator(mode="after")
    def validate_unique_data_tables(self) -> QuestionPlanContent:
        """同一计划对一张契约表只声明一次字段集合。"""
        table_ids = [reference.table_id for reference in self.data]
        if len(set(table_ids)) != len(table_ids):
            raise ValueError("计划的数据表引用不能重复")
        return self


class QuestionPlanCollection(BaseModel):
    """一次 Modeler 调用生成的完整 M1.5 QuestionPlan 集合。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["m1.5"]
    question_plans: tuple[QuestionPlan, ...] = Field(min_length=1)
    sensitivity_analysis: QuestionPlanContent

    @model_validator(mode="after")
    def validate_unique_questions(self) -> QuestionPlanCollection:
        """集合内问题标识及产物路径必须唯一。"""
        question_ids = [plan.question_id for plan in self.question_plans]
        if len(set(question_ids)) != len(question_ids):
            raise ValueError("QuestionPlan 集合的问题标识不能重复")
        artifact_ids = [plan.artifact_id for plan in self.question_plans]
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("QuestionPlan 集合的 artifact_id 不能重复")
        artifact_paths = [plan.artifact_path for plan in self.question_plans]
        if len(set(artifact_paths)) != len(artifact_paths):
            raise ValueError("QuestionPlan 集合的 artifact_path 不能重复")
        return self

    def get_plan(self, question_id: str) -> QuestionPlan | None:
        """按稳定问题标识返回计划。"""
        return next(
            (plan for plan in self.question_plans if plan.question_id == question_id),
            None,
        )

    def to_coder_handoff(
        self,
        work_dir: str | Path,
        data_contract: DataContract,
    ) -> dict[str, str]:
        """复验冻结契约并降级为旧 Coder 所需的有限文本。"""
        from app.data.contracts import validate_data_contract_integrity

        validate_data_contract_integrity(work_dir, data_contract)
        _validate_collection_contract_identity(self, data_contract)
        return _render_question_plan_handoff(self, data_contract)


class _QuestionPlanProposal(QuestionPlanContent):
    """模型可声明的单题内容，不允许伪造产物元数据或路径。"""

    question_id: QuestionId
    upstream_results: tuple[UpstreamResultReference, ...] = ()
    deliverables: tuple[Deliverable, ...] = Field(min_length=1)


class _QuestionPlanProposalSet(BaseModel):
    """Modeler 的 M1.5 JSON 响应结构。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["m1.5"]
    question_plans: tuple[_QuestionPlanProposal, ...] = Field(min_length=1)
    sensitivity_analysis: QuestionPlanContent


class QuestionPlanValidation(BaseModel):
    """结合 TaskOutline、冻结契约和上游声明校验 Modeler JSON。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    outline: TaskOutline
    data_contract: DataContract
    upstream_result_declarations: tuple[UpstreamResultReference, ...] = ()

    @model_validator(mode="after")
    def validate_inputs(self) -> QuestionPlanValidation:
        """拒绝未冻结、跨任务或无消费方的上游声明。"""
        if self.outline.validation_status != "validated":
            raise ValueError("TaskOutline 未通过校验")
        if (
            self.data_contract.validation_status != "validated"
            or self.data_contract.status not in {"frozen", "no_data"}
        ):
            raise ValueError("DataContract 必须为 validated frozen/no_data")
        if (
            self.outline.artifact_id not in self.data_contract.source_artifact_ids
            and self.outline.task_facts_id not in self.data_contract.source_artifact_ids
        ):
            raise ValueError("DataContract 不属于当前 TaskOutline")

        declaration_ids = [
            declaration.question_id for declaration in self.upstream_result_declarations
        ]
        if len(set(declaration_ids)) != len(declaration_ids):
            raise ValueError("上游结果声明的问题标识不能重复")

        question_ids = {question.question_id for question in self.outline.questions}
        unknown = set(declaration_ids) - question_ids
        if unknown:
            raise ValueError(f"上游结果声明包含未知问题: {sorted(unknown)}")
        dependency_ids = {
            dependency_id
            for question in self.outline.questions
            for dependency_id in _transitive_dependencies(
                self.outline,
                question.question_id,
            )
        }
        unused = set(declaration_ids) - dependency_ids
        if unused:
            raise ValueError(f"上游结果声明不属于任何问题的传递依赖: {sorted(unused)}")
        return self

    @classmethod
    def for_inputs(
        cls,
        outline: TaskOutline,
        data_contract: DataContract,
        upstream_result_declarations: tuple[UpstreamResultReference, ...] = (),
    ) -> QuestionPlanValidation:
        """从 M1.5 唯一允许的三类输入构造校验器。"""
        return cls(
            outline=outline,
            data_contract=data_contract,
            upstream_result_declarations=upstream_result_declarations,
        )

    def parse_json(self, raw_json: str) -> QuestionPlanCollection:
        """解析模型响应并执行集合、数据、依赖和交付物校验。"""
        try:
            proposal_set = _QuestionPlanProposalSet.model_validate_json(
                _strip_code_fence(raw_json)
            )
        except ValidationError as exc:
            errors = tuple(
                f"schema.{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            )
            raise QuestionPlanValidationError(errors) from exc

        proposal_by_id: dict[str, _QuestionPlanProposal] = {}
        for proposal in proposal_set.question_plans:
            if proposal.question_id in proposal_by_id:
                raise QuestionPlanValidationError(
                    (f"duplicate_question_plan: {proposal.question_id}",)
                )
            proposal_by_id[proposal.question_id] = proposal

        expected_ids = {question.question_id for question in self.outline.questions}
        actual_ids = set(proposal_by_id)
        if actual_ids != expected_ids:
            raise QuestionPlanValidationError(
                (
                    "question_keys_mismatch: "
                    f"expected={sorted(expected_ids)}, actual={sorted(actual_ids)}",
                )
            )

        plans: list[QuestionPlan] = []
        try:
            for outline_question in self.outline.questions:
                proposal = proposal_by_id[outline_question.question_id]
                plan = QuestionPlan(
                    schema_version="m1.5",
                    artifact_id=f"question-plan:{outline_question.question_id}",
                    source_artifact_ids=(
                        self.outline.artifact_id,
                        self.data_contract.artifact_id,
                    ),
                    validation_status="validated",
                    artifact_path=(
                        f"m15/question_plans/{outline_question.question_id}.json"
                    ),
                    question_id=outline_question.question_id,
                    task_outline_id=self.outline.artifact_id,
                    data_contract_id=self.data_contract.artifact_id,
                    objective=proposal.objective,
                    data=proposal.data,
                    upstream_results=proposal.upstream_results,
                    model=proposal.model,
                    method=proposal.method,
                    constraints=proposal.constraints,
                    validation=proposal.validation,
                    figures=proposal.figures,
                    deliverables=proposal.deliverables,
                    fallback=proposal.fallback,
                )
                self._validate_plan(plan, outline_question.deliverables)
                plans.append(plan)

            self._validate_data_references(proposal_set.sensitivity_analysis.data)
            collection = QuestionPlanCollection(
                schema_version="m1.5",
                question_plans=tuple(plans),
                sensitivity_analysis=proposal_set.sensitivity_analysis,
            )
            _render_question_plan_handoff(collection, self.data_contract)
        except (ValidationError, ValueError) as exc:
            raise QuestionPlanValidationError((str(exc),)) from exc
        return collection

    def _validate_plan(
        self,
        plan: QuestionPlan,
        expected_deliverables: tuple[Deliverable, ...],
    ) -> None:
        """校验一题的数据、传递依赖、来源和交付物。"""
        if plan.source_artifact_ids != (
            self.outline.artifact_id,
            self.data_contract.artifact_id,
        ):
            raise ValueError("QuestionPlan 只能来源于当前 outline 和 contract")
        if plan.deliverables != expected_deliverables:
            raise ValueError(f"deliverables_mismatch: question={plan.question_id}")
        self._validate_data_references(plan.data)

        allowed_dependencies = set(
            _transitive_dependencies(self.outline, plan.question_id)
        )
        declarations = {
            declaration.question_id: set(declaration.outputs)
            for declaration in self.upstream_result_declarations
        }
        for reference in plan.upstream_results:
            if reference.question_id not in allowed_dependencies:
                raise ValueError(
                    "invalid_upstream_question: "
                    f"question={plan.question_id}, "
                    f"upstream={reference.question_id}"
                )
            allowed_outputs = declarations.get(reference.question_id)
            if allowed_outputs is None:
                raise ValueError(
                    "undeclared_upstream_result: "
                    f"question={plan.question_id}, "
                    f"upstream={reference.question_id}"
                )
            unknown_outputs = set(reference.outputs) - allowed_outputs
            if unknown_outputs:
                raise ValueError(
                    "unknown_upstream_outputs: "
                    f"question={plan.question_id}, "
                    f"outputs={sorted(unknown_outputs)}"
                )

    def _validate_data_references(
        self,
        references: tuple[ContractDataReference, ...],
    ) -> None:
        """只允许引用冻结契约中的表和规范字段。"""
        table_by_id = {table.table_id: table for table in self.data_contract.tables}
        for reference in references:
            table = table_by_id.get(reference.table_id)
            if table is None:
                raise ValueError(f"unknown_data_table: {reference.table_id}")
            allowed_columns = {column.name for column in table.columns}
            unknown_columns = set(reference.columns) - allowed_columns
            if unknown_columns:
                raise ValueError(
                    f"unknown_data_columns: table={reference.table_id}, "
                    f"columns={sorted(unknown_columns)}"
                )


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
                f"schema.{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
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
        # 兼容交接也是 M1 输出契约的一部分，必须在 phase 成功前验证。
        plan.to_coder_handoff(self.data_catalog)
        return plan

    def _validate_data_references(self, plan: ModelPlan) -> None:
        """校验计划引用的数据文件和列都来自领域数据目录。"""
        sections = [*plan.question_plans.values(), plan.sensitivity_analysis]
        for section in sections:
            for reference in section.data:
                self._validate_reference(reference)

    def _validate_reference(self, reference: DataReference) -> None:
        """校验单条数据引用。"""
        table = self.data_catalog.get_table(reference.table_id)
        if table is None:
            raise ModelPlanValidationError(
                (f"unknown_data_table: {reference.table_id}",)
            )

        allowed_columns = {column.name for column in table.columns}
        unknown_columns = set(reference.columns) - allowed_columns
        if unknown_columns:
            raise ModelPlanValidationError(
                (
                    f"unknown_data_columns: table={reference.table_id}, "
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


def _render_section(section: PlanSection, data_catalog: DataCatalog) -> str:
    """将一个领域计划段渲染为旧 Coder 的紧凑文本。"""
    if section.data:
        data_text = "；".join(
            _render_reference(reference, data_catalog) for reference in section.data
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


def _render_reference(
    reference: DataReference,
    data_catalog: DataCatalog,
) -> str:
    """把规范引用渲染为旧 Coder 可准确读取的文件与原始表头。"""
    table = data_catalog.get_table(reference.table_id)
    if table is None:
        raise ValueError(f"未知 table_id: {reference.table_id}")

    column_by_name = {column.name: column for column in table.columns}
    rendered_columns = []
    for name in reference.columns:
        column = column_by_name[name]
        if column.name == column.source_name:
            rendered_columns.append(column.name)
        else:
            rendered_columns.append(f"{column.name}（原始表头={column.source_name!r}）")

    sheet_text = f"，sheet={table.sheet!r}" if table.sheet is not None else ""
    return (
        f"{table.table_id}（文件={table.file!r}{sheet_text}，"
        f"列={', '.join(rendered_columns)}）"
    )


def _transitive_dependencies(
    outline: TaskOutline,
    question_id: str,
) -> tuple[str, ...]:
    """按 outline 顺序返回指定问题的全部传递依赖。"""
    question_by_id = {question.question_id: question for question in outline.questions}
    dependencies: set[str] = set()

    def collect(current_id: str) -> None:
        for dependency_id in question_by_id[current_id].depends_on:
            if dependency_id in dependencies:
                continue
            dependencies.add(dependency_id)
            collect(dependency_id)

    collect(question_id)
    return tuple(
        question.question_id
        for question in outline.questions
        if question.question_id in dependencies
    )


def _validate_collection_contract_identity(
    collection: QuestionPlanCollection,
    data_contract: DataContract,
) -> None:
    """防止用另一个冻结契约渲染已经校验过的计划。"""
    mismatched = [
        plan.question_id
        for plan in collection.question_plans
        if plan.data_contract_id != data_contract.artifact_id
    ]
    if mismatched:
        raise QuestionPlanValidationError(
            (
                "data_contract_mismatch: "
                f"questions={sorted(mismatched)}, "
                f"actual={data_contract.artifact_id}",
            )
        )


def _render_question_plan_handoff(
    collection: QuestionPlanCollection,
    data_contract: DataContract,
) -> dict[str, str]:
    """构造唯一 M1.5 到旧 Coder 的文本兼容边界。"""
    handoff = {
        plan.question_id: _render_m15_plan(plan, data_contract)
        for plan in collection.question_plans
    }
    handoff["sensitivity_analysis"] = _render_m15_content(
        collection.sensitivity_analysis,
        data_contract,
        upstream_results=(),
        deliverables=(),
    )
    oversized = {
        key: len(value)
        for key, value in handoff.items()
        if len(value) > MAX_CODER_HANDOFF_CHARS
    }
    if oversized:
        raise QuestionPlanValidationError(
            (
                "coder_handoff_too_long: "
                + ", ".join(f"{key}={length}" for key, length in oversized.items()),
            )
        )
    return handoff


def _render_m15_plan(
    plan: QuestionPlan,
    data_contract: DataContract,
) -> str:
    """渲染单题计划，不暴露原始附件路径或原始表头。"""
    content = QuestionPlanContent(
        data=plan.data,
        objective=plan.objective,
        model=plan.model,
        method=plan.method,
        constraints=plan.constraints,
        validation=plan.validation,
        figures=plan.figures,
        fallback=plan.fallback,
    )
    return _render_m15_content(
        content,
        data_contract,
        upstream_results=plan.upstream_results,
        deliverables=plan.deliverables,
    )


def _render_m15_content(
    content: QuestionPlanContent,
    data_contract: DataContract,
    *,
    upstream_results: tuple[UpstreamResultReference, ...],
    deliverables: tuple[Deliverable, ...],
) -> str:
    """使用 cleaned 路径和契约字段渲染一段紧凑计划。"""
    table_by_id = {table.table_id: table for table in data_contract.tables}
    if content.data:
        data_text = "；".join(
            _render_contract_reference(reference, table_by_id)
            for reference in content.data
        )
    else:
        data_text = "无数据输入；仅依据题目事实与已声明上游结果。"

    if upstream_results:
        upstream_text = "；".join(
            f"{reference.question_id}[{', '.join(reference.outputs)}]"
            for reference in upstream_results
        )
    else:
        upstream_text = "无"

    if deliverables:
        deliverable_text = "；".join(
            (
                f"{deliverable.deliverable_id}={deliverable.description}"
                if deliverable.path is None
                else (
                    f"{deliverable.deliverable_id}={deliverable.description}"
                    f"（输出={deliverable.path}）"
                )
            )
            for deliverable in deliverables
        )
    else:
        deliverable_text = "沿用各问题已声明交付物。"

    constraints = "；".join(content.constraints) or "按题目与契约事实确定。"
    return "\n".join(
        [
            f"目标：{content.objective}",
            f"数据：{data_text}",
            f"上游结果：{upstream_text}",
            f"模型：{content.model}",
            f"方法：{content.method}",
            f"约束：{constraints}",
            f"验证：{'；'.join(content.validation)}",
            f"图表：{'；'.join(content.figures)}",
            f"交付物：{deliverable_text}",
            f"回退：{content.fallback}",
        ]
    )


def _render_contract_reference(
    reference: ContractDataReference,
    table_by_id: dict[str, Any],
) -> str:
    """只渲染可执行 cleaned 路径和已经验证的规范字段。"""
    table = table_by_id.get(reference.table_id)
    if table is None:
        raise ValueError(f"unknown_data_table: {reference.table_id}")
    column_by_name = {column.name: column for column in table.columns}
    try:
        rendered_columns = ", ".join(
            f"{name}:{column_by_name[name].canonical_type}"
            for name in reference.columns
        )
    except KeyError as exc:
        raise ValueError(
            f"unknown_data_columns: table={reference.table_id}, column={exc.args[0]}"
        ) from exc
    return f"cleaned_path={table.cleaned_path!r}（verified_columns={rendered_columns}）"
