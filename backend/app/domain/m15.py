"""M1.5 数据可靠性阶段的版本化领域契约。"""

from __future__ import annotations

import hashlib
import math
import re
from pathlib import PurePosixPath
from typing import Annotated, Literal, cast

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

M15_SCHEMA_VERSION = "m1.5"
M15_ARTIFACT_DIRECTORY = "m15"

_ARTIFACT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:[._:-][a-z0-9][a-z0-9_-]*)*$")
_QUESTION_ID_PATTERN = re.compile(r"^ques[1-9][0-9]*$")


def _validate_source_name(value: str) -> str:
    """校验原始名称非空，同时保留有意义的首尾空白。"""
    if not value.strip():
        raise ValueError("原始名称不能为空")
    return value


def _validate_stable_id(value: str) -> str:
    """校验可跨阶段引用的机器标识。"""
    if not _ARTIFACT_ID_PATTERN.fullmatch(value):
        raise ValueError("稳定标识必须使用小写字母、数字及 . _ : - 分隔")
    return value


def _validate_question_id(value: str) -> str:
    """校验稳定问题标识。"""
    if not _QUESTION_ID_PATTERN.fullmatch(value):
        raise ValueError("问题标识必须为 quesN，且 N 从 1 开始")
    return value


def _validate_relative_path(value: str) -> str:
    """校验工作目录内使用的 POSIX 相对路径。"""
    if value != value.strip() or not value:
        raise ValueError("相对路径不能为空或包含首尾空白")
    if "\\" in value or "\x00" in value:
        raise ValueError("相对路径必须使用 POSIX 分隔符")

    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError("相对路径不能包含空段、. 或 ..")

    path = PurePosixPath(value)
    if path.is_absolute():
        raise ValueError("产物路径必须为相对路径")
    return path.as_posix()


def _validate_m15_artifact_path(value: str) -> str:
    """限定 JSON 领域产物位于任务的 m15 目录。"""
    path = _validate_relative_path(value)
    parts = PurePosixPath(path).parts
    if not parts or parts[0] != M15_ARTIFACT_DIRECTORY:
        raise ValueError(f"M1.5 产物路径必须位于 {M15_ARTIFACT_DIRECTORY}/")
    if not path.endswith(".json"):
        raise ValueError("M1.5 领域产物必须使用 .json 后缀")
    return path


StableId = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=160),
    AfterValidator(_validate_stable_id),
]
QuestionId = Annotated[
    str,
    StringConstraints(strict=True, min_length=5, max_length=32),
    AfterValidator(_validate_question_id),
]
RelativePath = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=1024),
    AfterValidator(_validate_relative_path),
]
M15ArtifactPath = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=1024),
    AfterValidator(_validate_m15_artifact_path),
]
Sha256 = Annotated[
    str,
    StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$"),
]
NonBlankText = Annotated[
    str,
    StringConstraints(strict=True, strip_whitespace=True, min_length=1),
]
SourceName = Annotated[
    str,
    StringConstraints(strict=True, min_length=1),
    AfterValidator(_validate_source_name),
]

ArtifactValidationStatus = Literal["validated", "failed"]
CanonicalDataType = Literal[
    "string",
    "integer",
    "number",
    "boolean",
    "date",
    "datetime",
    "category",
]


class StrictFrozenModel(BaseModel):
    """所有 M1.5 契约共享的严格、只读 Pydantic 配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class VersionedArtifact(StrictFrozenModel):
    """可持久化阶段产物的共享元数据。"""

    schema_version: Literal["m1.5"]
    artifact_id: StableId
    source_artifact_ids: tuple[StableId, ...]
    validation_status: ArtifactValidationStatus
    artifact_path: M15ArtifactPath

    @field_validator("source_artifact_ids")
    @classmethod
    def validate_source_artifact_ids(
        cls,
        source_artifact_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        """来源标识必须唯一，顺序由产物生成方稳定提供。"""
        if len(set(source_artifact_ids)) != len(source_artifact_ids):
            raise ValueError("来源产物标识不能重复")
        return source_artifact_ids

    @model_validator(mode="after")
    def validate_not_self_sourced(self) -> VersionedArtifact:
        """产物不能把自身声明为上游来源。"""
        if self.artifact_id in self.source_artifact_ids:
            raise ValueError("产物不能引用自身作为来源")
        return self


class FileFact(StrictFrozenModel):
    """TaskFacts 中一个只读附件事实。"""

    path: RelativePath
    sha256: Sha256


class TaskFacts(VersionedArtifact):
    """由外部请求与附件发现形成的只读任务事实。"""

    artifact_type: Literal["task_facts"] = "task_facts"
    task_id: NonBlankText
    problem_text: NonBlankText
    expected_question_ids: tuple[QuestionId, ...] | None = None
    expected_question_count: int | None = Field(default=None, ge=1)
    attachments: tuple[FileFact, ...] = ()
    output_templates: tuple[FileFact, ...] = ()

    @model_validator(mode="after")
    def validate_task_facts(self) -> TaskFacts:
        """任务事实是来源根节点，且附件身份不能重叠。"""
        if self.source_artifact_ids:
            raise ValueError("TaskFacts 不能声明上游产物")
        attachment_paths = [item.path for item in self.attachments]
        template_paths = [item.path for item in self.output_templates]
        if len(set(attachment_paths)) != len(attachment_paths):
            raise ValueError("附件路径不能重复")
        if len(set(template_paths)) != len(template_paths):
            raise ValueError("输出模板路径不能重复")
        overlap = set(attachment_paths) & set(template_paths)
        if overlap:
            raise ValueError(f"附件和输出模板身份不能重叠: {sorted(overlap)}")

        if (self.expected_question_ids is None) != (
            self.expected_question_count is None
        ):
            raise ValueError("预期问题标识与数量必须同时存在或同时不可用")
        if self.expected_question_ids is not None:
            expected_ids = tuple(
                f"ques{index}"
                for index in range(1, len(self.expected_question_ids) + 1)
            )
            if self.expected_question_ids != expected_ids:
                raise ValueError(
                    "TaskFacts 预期问题标识必须按顺序严格覆盖 ques1..quesN"
                )
            if self.expected_question_count != len(self.expected_question_ids):
                raise ValueError("TaskFacts 预期问题数量必须由问题标识数量确定")
        return self


class Deliverable(StrictFrozenModel):
    """题目声明的一项交付物。"""

    deliverable_id: StableId
    description: NonBlankText
    path: RelativePath | None = None


class OutlineQuestion(StrictFrozenModel):
    """TaskOutline 中的一个稳定问题节点。"""

    question_id: QuestionId
    text: NonBlankText
    depends_on: tuple[QuestionId, ...] = ()
    order_key: int = Field(ge=1)
    deliverables: tuple[Deliverable, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_direct_dependencies(self) -> OutlineQuestion:
        """单节点层面拒绝重复和自依赖。"""
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("问题的直接依赖不能重复")
        if self.question_id in self.depends_on:
            raise ValueError("问题不能依赖自身")
        return self


class TaskOutline(VersionedArtifact):
    """Coordinator 基于 TaskFacts 生成的全局任务骨架。"""

    artifact_type: Literal["task_outline"] = "task_outline"
    task_facts_id: StableId
    questions: tuple[OutlineQuestion, ...] = Field(min_length=1)
    deliverables: tuple[Deliverable, ...] = ()

    @model_validator(mode="after")
    def validate_task_outline_sources(self) -> TaskOutline:
        """骨架必须覆盖连续问题集合，并形成合法有向无环图。"""
        if self.source_artifact_ids != (self.task_facts_id,):
            raise ValueError("TaskOutline 必须且只能直接来源于 task_facts_id")

        question_ids = [question.question_id for question in self.questions]
        if len(set(question_ids)) != len(question_ids):
            raise ValueError("TaskOutline 的问题标识不能重复")

        expected_question_ids = {
            f"ques{index}" for index in range(1, len(question_ids) + 1)
        }
        if set(question_ids) != expected_question_ids:
            raise ValueError(
                "TaskOutline 的问题标识必须严格覆盖 ques1..quesN: "
                f"expected={sorted(expected_question_ids)}, "
                f"actual={sorted(question_ids)}"
            )

        order_keys = [question.order_key for question in self.questions]
        if len(set(order_keys)) != len(order_keys):
            raise ValueError("TaskOutline 的排序键不能重复")
        expected_order_keys = set(range(1, len(self.questions) + 1))
        if set(order_keys) != expected_order_keys:
            raise ValueError("TaskOutline 的排序键必须严格覆盖 1..N")

        question_id_set = set(question_ids)
        for question in self.questions:
            unknown = set(question.depends_on) - question_id_set
            if unknown:
                raise ValueError(
                    f"{question.question_id} 包含未知依赖: {sorted(unknown)}"
                )

        dependencies = {
            question.question_id: question.depends_on for question in self.questions
        }
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(question_id: str) -> None:
            if question_id in visiting:
                raise ValueError("TaskOutline 的问题依赖不能形成环")
            if question_id in visited:
                return
            visiting.add(question_id)
            for dependency_id in dependencies[question_id]:
                visit(dependency_id)
            visiting.remove(question_id)
            visited.add(question_id)

        for question_id in question_ids:
            visit(question_id)

        deliverable_ids = [
            deliverable.deliverable_id for deliverable in self.deliverables
        ]
        if len(set(deliverable_ids)) != len(deliverable_ids):
            raise ValueError("TaskOutline 的总体交付物标识不能重复")
        for question in self.questions:
            question_deliverable_ids = [
                deliverable.deliverable_id for deliverable in question.deliverables
            ]
            if len(set(question_deliverable_ids)) != len(question_deliverable_ids):
                raise ValueError(f"{question.question_id} 的交付物标识不能重复")
        return self

    @property
    def ques_count(self) -> int:
        """返回由问题节点数量派生的旧接口兼容值。"""
        return len(self.questions)


class ProfileColumn(StrictFrozenModel):
    """数据画像中的单列统计。"""

    name: NonBlankText
    source_name: SourceName
    raw_dtype: NonBlankText
    canonical_type: CanonicalDataType
    missing_count: int = Field(ge=0)
    missing_ratio: float = Field(ge=0.0, le=1.0)
    unique_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_normalized_name(self) -> ProfileColumn:
        """规范列名必须与原始表头的 strip 结果一致。"""
        if self.name != self.source_name.strip():
            raise ValueError("画像规范列名必须等于原始表头的 strip 结果")
        return self


class CandidateKey(StrictFrozenModel):
    """基于画像证据得到的候选键。"""

    columns: tuple[NonBlankText, ...] = Field(min_length=1)
    uniqueness_ratio: float = Field(ge=0.0, le=1.0)
    non_null_ratio: float = Field(ge=0.0, le=1.0)

    @field_validator("columns")
    @classmethod
    def validate_columns(cls, columns: tuple[str, ...]) -> tuple[str, ...]:
        """候选键不能重复引用同一列。"""
        if len(set(columns)) != len(columns):
            raise ValueError("候选键列不能重复")
        return columns


class JoinEvidence(StrictFrozenModel):
    """两个画像表之间候选关联字段的匹配证据。"""

    target_table_id: NonBlankText
    source_columns: tuple[NonBlankText, ...] = Field(min_length=1)
    target_columns: tuple[NonBlankText, ...] = Field(min_length=1)
    type_compatible: bool
    non_null_ratio: float = Field(ge=0.0, le=1.0)
    overlap_ratio: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_column_arity(self) -> JoinEvidence:
        """关联两侧必须使用相同数量的字段。"""
        if len(self.source_columns) != len(self.target_columns):
            raise ValueError("关联字段两侧数量必须一致")
        if len(set(self.source_columns)) != len(self.source_columns):
            raise ValueError("关联源字段不能重复")
        if len(set(self.target_columns)) != len(self.target_columns):
            raise ValueError("关联目标字段不能重复")
        return self


class DataProfile(VersionedArtifact):
    """逐文件、逐 Sheet 的只读完整数据画像。"""

    artifact_type: Literal["data_profile"] = "data_profile"
    task_outline_id: StableId
    table_id: NonBlankText
    source_path: RelativePath
    source_sheet: str | None = None
    source_sha256: Sha256
    row_count: int = Field(ge=0)
    column_count: int = Field(ge=1)
    columns: tuple[ProfileColumn, ...] = Field(min_length=1)
    duplicate_row_count: int = Field(ge=0)
    candidate_keys: tuple[CandidateKey, ...] = ()
    join_evidence: tuple[JoinEvidence, ...] = ()

    @field_validator("source_sheet")
    @classmethod
    def validate_source_sheet(cls, source_sheet: str | None) -> str | None:
        """CSV 使用 None，Excel Sheet 名不能是空白。"""
        if source_sheet is not None and not source_sheet.strip():
            raise ValueError("Sheet 名不能为空")
        return source_sheet

    @model_validator(mode="after")
    def validate_profile(self) -> DataProfile:
        """画像来源和列统计必须内部一致。"""
        _require_sources(self.source_artifact_ids, self.task_outline_id)
        source_suffix = PurePosixPath(self.source_path).suffix.lower()
        if source_suffix == ".csv" and self.source_sheet is not None:
            raise ValueError("CSV DataProfile 不能声明 source_sheet")
        if source_suffix == ".xlsx" and self.source_sheet is None:
            raise ValueError("XLSX DataProfile 必须声明 source_sheet")
        if source_suffix not in {".csv", ".xlsx"}:
            raise ValueError("DataProfile 只支持 CSV 或 XLSX 来源")
        expected_table_id = (
            self.source_path
            if self.source_sheet is None
            else f"{self.source_path}::{self.source_sheet.strip()}"
        )
        if self.table_id != expected_table_id:
            raise ValueError(
                f"DataProfile.table_id 必须等于源文件与 Sheet 组合: {expected_table_id}"
            )
        if self.column_count != len(self.columns):
            raise ValueError("column_count 必须等于 columns 数量")
        names = [column.name for column in self.columns]
        if len(set(names)) != len(names):
            raise ValueError("画像中的规范列名不能重复")
        if self.duplicate_row_count > self.row_count:
            raise ValueError("重复行数不能超过总行数")

        name_set = set(names)
        for column in self.columns:
            if column.missing_count > self.row_count:
                raise ValueError(f"{column.name} 的缺失数不能超过总行数")
            if column.unique_count > self.row_count - column.missing_count:
                raise ValueError(f"{column.name} 的唯一值数不能超过非空行数")
            expected_ratio = (
                column.missing_count / self.row_count if self.row_count else 0.0
            )
            if abs(column.missing_ratio - expected_ratio) > 1e-12:
                raise ValueError(f"{column.name} 的缺失比例与行数不一致")

        candidate_columns = [key.columns for key in self.candidate_keys]
        if len(set(candidate_columns)) != len(candidate_columns):
            raise ValueError("候选键不能重复")
        for key in self.candidate_keys:
            unknown = set(key.columns) - name_set
            if unknown:
                raise ValueError(f"候选键包含未知列: {sorted(unknown)}")

        join_keys: set[tuple[str, tuple[str, ...], tuple[str, ...]]] = set()
        for evidence in self.join_evidence:
            unknown = set(evidence.source_columns) - name_set
            if unknown:
                raise ValueError(f"关联证据包含未知源列: {sorted(unknown)}")
            join_key = (
                evidence.target_table_id,
                evidence.source_columns,
                evidence.target_columns,
            )
            if join_key in join_keys:
                raise ValueError("关联证据不能重复")
            join_keys.add(join_key)
        return self


class ValidationExpectation(StrictFrozenModel):
    """可由确定性 verifier 执行的后置条件。"""

    rule_id: StableId
    rule_type: Literal[
        "columns_exist",
        "canonical_type",
        "row_count",
        "key_unique",
        "key_non_null",
        "missing_count",
        "duplicate_count",
        "join_compatible",
        "source_sha256",
    ]
    columns: tuple[NonBlankText, ...] = ()
    target_table_id: NonBlankText | None = None
    target_columns: tuple[NonBlankText, ...] = ()
    operator: Literal["eq", "le", "ge"]
    expected: JsonValue

    @model_validator(mode="after")
    def validate_expectation_shape(self) -> ValidationExpectation:
        """规则参数必须与确定性验证语义一致。"""
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("验证规则的字段不能重复")
        if len(set(self.target_columns)) != len(self.target_columns):
            raise ValueError("验证规则的关联目标字段不能重复")
        column_rules = {
            "columns_exist",
            "canonical_type",
            "key_unique",
            "key_non_null",
            "missing_count",
        }
        if self.rule_type in column_rules and not self.columns:
            raise ValueError(f"{self.rule_type} 必须声明 columns")
        if self.rule_type == "join_compatible":
            if (
                not self.columns
                or self.target_table_id is None
                or not self.target_columns
            ):
                raise ValueError("join_compatible 必须声明关联两侧表和列")
            if len(self.columns) != len(self.target_columns):
                raise ValueError("join_compatible 两侧字段数量必须一致")
        elif self.target_table_id is not None or self.target_columns:
            raise ValueError("只有 join_compatible 可声明关联目标")

        exact_rules = {
            "columns_exist",
            "canonical_type",
            "key_unique",
            "key_non_null",
            "join_compatible",
            "source_sha256",
        }
        if self.rule_type in exact_rules and self.operator != "eq":
            raise ValueError(f"{self.rule_type} 只支持 eq")
        if self.rule_type in {"key_unique", "key_non_null", "join_compatible"}:
            if self.expected is not True:
                raise ValueError(f"{self.rule_type} 的 expected 必须为 true")
        if self.rule_type == "columns_exist" and self.expected is not True:
            raise ValueError("columns_exist 的 expected 必须为 true")
        if self.rule_type == "canonical_type" and self.expected not in {
            "string",
            "integer",
            "number",
            "boolean",
            "date",
            "datetime",
            "category",
        }:
            raise ValueError("canonical_type 的 expected 必须是规范类型")
        if self.rule_type == "canonical_type" and len(self.columns) != 1:
            raise ValueError("canonical_type 每条规则必须只验证一列")
        if self.rule_type in {
            "row_count",
            "missing_count",
            "duplicate_count",
        } and (not isinstance(self.expected, int) or isinstance(self.expected, bool)):
            raise ValueError(f"{self.rule_type} 的 expected 必须为整数")
        if (
            self.rule_type
            in {
                "row_count",
                "missing_count",
                "duplicate_count",
            }
            and cast(int, self.expected) < 0
        ):
            raise ValueError(f"{self.rule_type} 的 expected 不能为负数")
        if self.rule_type in {"row_count", "source_sha256"} and self.columns:
            raise ValueError(f"{self.rule_type} 不能声明 columns")
        if self.rule_type == "source_sha256" and (
            not isinstance(self.expected, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.expected) is None
        ):
            raise ValueError("source_sha256 的 expected 必须为 SHA-256")
        return self


class CleaningOperation(StrictFrozenModel):
    """清洗计划中的一项白名单声明。"""

    operation_id: StableId
    operation_type: Literal[
        "cast_type",
        "fill_missing",
        "drop_empty_rows",
        "drop_duplicates",
        "normalize_join_key",
    ]
    target_columns: tuple[NonBlankText, ...] = Field(min_length=1)
    target_type: CanonicalDataType | None = None
    strategy: (
        Literal[
            "strict",
            "forward_fill",
            "backward_fill",
            "constant",
            "mean",
            "median",
            "mode",
        ]
        | None
    ) = None
    fill_value: JsonValue = None
    keep: Literal["first", "last"] | None = None
    normalizations: tuple[
        Literal["strip", "casefold", "unicode_nfkc", "collapse_whitespace"], ...
    ] = ()
    reason: NonBlankText
    postconditions: tuple[ValidationExpectation, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_operation_parameters(self) -> CleaningOperation:
        """每种操作只接受自身有限参数，拒绝含糊或组合式指令。"""
        if len(set(self.target_columns)) != len(self.target_columns):
            raise ValueError("清洗操作的目标列不能重复")
        rule_ids = [condition.rule_id for condition in self.postconditions]
        if len(set(rule_ids)) != len(rule_ids):
            raise ValueError("同一清洗操作的后置条件 rule_id 不能重复")

        if self.operation_type == "cast_type":
            if self.target_type is None or self.strategy != "strict":
                raise ValueError("cast_type 必须声明 target_type 和 strict strategy")
            self._reject_parameters(
                fill_value=self.fill_value,
                keep=self.keep,
                normalizations=self.normalizations,
            )
            allowed_rules = {"canonical_type"}
        elif self.operation_type == "fill_missing":
            if self.strategy not in {
                "forward_fill",
                "backward_fill",
                "constant",
                "mean",
                "median",
                "mode",
            }:
                raise ValueError("fill_missing 必须声明受支持的 strategy")
            if self.strategy == "constant" and self.fill_value is None:
                raise ValueError("constant 缺失处理必须声明非空 fill_value")
            if self.strategy == "constant" and isinstance(
                self.fill_value,
                (dict, list),
            ):
                raise ValueError("constant fill_value 必须是 JSON 标量")
            if (
                self.strategy == "constant"
                and isinstance(self.fill_value, float)
                and not math.isfinite(self.fill_value)
            ):
                raise ValueError("constant fill_value 必须是有限 JSON 数值")
            if self.strategy != "constant" and self.fill_value is not None:
                raise ValueError("只有 constant 缺失处理可声明 fill_value")
            self._reject_parameters(
                target_type=self.target_type,
                keep=self.keep,
                normalizations=self.normalizations,
            )
            allowed_rules = {"missing_count", "key_non_null"}
        elif self.operation_type == "drop_empty_rows":
            if self.strategy != "strict":
                raise ValueError("drop_empty_rows 必须声明 strict strategy")
            self._reject_parameters(
                target_type=self.target_type,
                fill_value=self.fill_value,
                keep=self.keep,
                normalizations=self.normalizations,
            )
            allowed_rules = {"row_count", "missing_count"}
        elif self.operation_type == "drop_duplicates":
            if self.keep is None:
                raise ValueError("drop_duplicates 必须声明 keep")
            self._reject_parameters(
                target_type=self.target_type,
                strategy=self.strategy,
                fill_value=self.fill_value,
                normalizations=self.normalizations,
            )
            allowed_rules = {"duplicate_count", "key_unique"}
        else:
            if not self.normalizations:
                raise ValueError("normalize_join_key 必须声明 normalizations")
            if len(set(self.normalizations)) != len(self.normalizations):
                raise ValueError("关联键规范化步骤不能重复")
            self._reject_parameters(
                target_type=self.target_type,
                strategy=self.strategy,
                fill_value=self.fill_value,
                keep=self.keep,
            )
            allowed_rules = {
                "canonical_type",
                "key_unique",
                "join_compatible",
            }

        invalid_rules = {
            condition.rule_type
            for condition in self.postconditions
            if condition.rule_type not in allowed_rules
        }
        if invalid_rules:
            raise ValueError(
                f"{self.operation_type} 包含不支持的后置条件: {sorted(invalid_rules)}"
            )
        for condition in self.postconditions:
            if condition.columns and not set(condition.columns).issubset(
                self.target_columns
            ):
                raise ValueError("后置条件只能引用当前操作的目标列")
            if (
                self.operation_type == "cast_type"
                and condition.expected != self.target_type
            ):
                raise ValueError("cast_type 后置类型必须等于 target_type")
            if (
                self.operation_type == "normalize_join_key"
                and condition.rule_type == "canonical_type"
                and condition.expected != "string"
            ):
                raise ValueError("normalize_join_key 的规范类型后置条件必须为 string")
        return self

    @staticmethod
    def _reject_parameters(**parameters: object) -> None:
        """拒绝当前操作类型无权声明的参数。"""
        populated = [
            name for name, value in parameters.items() if value not in (None, (), [])
        ]
        if populated:
            raise ValueError(f"操作包含不适用参数: {sorted(populated)}")


class CleaningPlan(VersionedArtifact):
    """针对单张画像表的有限清洗计划。"""

    artifact_type: Literal["cleaning_plan"] = "cleaning_plan"
    task_outline_id: StableId
    data_profile_id: StableId
    table_id: NonBlankText
    operations: tuple[CleaningOperation, ...] = ()
    no_op_reason: NonBlankText | None = None
    repair_attempt: int = Field(default=0, ge=0, le=2)
    repaired_issue_ids: tuple[StableId, ...] = ()

    @model_validator(mode="after")
    def validate_cleaning_plan(self) -> CleaningPlan:
        """计划必须追溯到 outline/profile，并显式说明空计划。"""
        _require_sources(
            self.source_artifact_ids,
            self.task_outline_id,
            self.data_profile_id,
        )
        if self.operations and self.no_op_reason is not None:
            raise ValueError("有清洗操作时不能设置 no_op_reason")
        if not self.operations and self.no_op_reason is None:
            raise ValueError("空清洗计划必须说明 no_op_reason")
        operation_ids = [operation.operation_id for operation in self.operations]
        if len(set(operation_ids)) != len(operation_ids):
            raise ValueError("清洗操作标识不能重复")
        rule_ids = [
            condition.rule_id
            for operation in self.operations
            for condition in operation.postconditions
        ]
        if len(set(rule_ids)) != len(rule_ids):
            raise ValueError("CleaningPlan 的后置条件 rule_id 不能重复")
        if self.repair_attempt == 0 and self.repaired_issue_ids:
            raise ValueError("初始 CleaningPlan 不能声明 repaired_issue_ids")
        if self.repair_attempt > 0 and not self.repaired_issue_ids:
            raise ValueError("Repair CleaningPlan 必须声明 repaired_issue_ids")
        missing_issue_sources = set(self.repaired_issue_ids) - set(
            self.source_artifact_ids
        )
        if missing_issue_sources:
            raise ValueError(
                f"Repair CleaningPlan 缺少 issue 来源: {sorted(missing_issue_sources)}"
            )
        if len(set(self.repaired_issue_ids)) != len(self.repaired_issue_ids):
            raise ValueError("repaired_issue_ids 不能重复")
        return self


class DataIssue(VersionedArtifact):
    """画像、清洗或验证阶段产生的结构化问题证据。"""

    artifact_type: Literal["data_issue"] = "data_issue"
    issue_id: StableId
    table_id: NonBlankText
    rule_id: StableId
    expected: JsonValue
    actual: JsonValue
    evidence_summary: NonBlankText
    repair_attempt: int = Field(ge=0, le=2)
    status: Literal["unresolved", "resolved", "exhausted"]

    @model_validator(mode="after")
    def validate_issue(self) -> DataIssue:
        """Issue 必须有来源，且稳定 issue_id 与 artifact_id 一致。"""
        if not self.source_artifact_ids:
            raise ValueError("DataIssue 必须声明来源产物")
        if self.issue_id != self.artifact_id:
            raise ValueError("DataIssue.issue_id 必须等于 artifact_id")
        if self.status == "exhausted" and self.repair_attempt != 2:
            raise ValueError("exhausted issue 的 repair_attempt 必须为 2")
        return self


class ContractColumn(StrictFrozenModel):
    """冻结数据契约中可供下游引用的字段。"""

    name: NonBlankText
    source_name: SourceName
    canonical_type: CanonicalDataType
    nullable: bool
    statistic_definition: NonBlankText


class ContractKey(StrictFrozenModel):
    """经 cleaned verifier 确认的候选键或计划声明键。"""

    key_id: StableId
    columns: tuple[NonBlankText, ...] = Field(min_length=1)
    kind: Literal["candidate", "declared"]
    uniqueness_verified: bool
    non_null_verified: bool
    validation_rule_ids: tuple[StableId, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_key(self) -> ContractKey:
        """键字段与验证规则必须唯一，且至少验证一项键约束。"""
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("契约键字段不能重复")
        if len(set(self.validation_rule_ids)) != len(self.validation_rule_ids):
            raise ValueError("契约键验证规则不能重复")
        if not self.uniqueness_verified and not self.non_null_verified:
            raise ValueError("契约键必须至少验证唯一性或非空性")
        if self.kind == "candidate" and (
            not self.uniqueness_verified or not self.non_null_verified
        ):
            raise ValueError("候选键必须同时通过唯一性与非空性验证")
        return self


class ContractRelation(StrictFrozenModel):
    """经过验证的数据表关联。"""

    relation_id: StableId
    source_columns: tuple[NonBlankText, ...] = Field(min_length=1)
    target_table_id: NonBlankText
    target_columns: tuple[NonBlankText, ...] = Field(min_length=1)
    validation_rule_id: StableId

    @model_validator(mode="after")
    def validate_column_arity(self) -> ContractRelation:
        """冻结关联两侧必须使用相同数量的字段。"""
        if len(self.source_columns) != len(self.target_columns):
            raise ValueError("关联字段两侧数量必须一致")
        if len(set(self.source_columns)) != len(self.source_columns):
            raise ValueError("关联源字段不能重复")
        if len(set(self.target_columns)) != len(self.target_columns):
            raise ValueError("关联目标字段不能重复")
        return self


class ContractTable(StrictFrozenModel):
    """DataContract 中一张已验证 cleaned 表。"""

    table_id: NonBlankText
    data_profile_id: StableId
    data_profile_path: M15ArtifactPath
    cleaning_plan_id: StableId
    cleaning_plan_path: M15ArtifactPath
    source_path: RelativePath
    source_sheet: str | None = None
    source_sha256: Sha256
    cleaned_path: RelativePath
    cleaned_sha256: Sha256
    row_count: int = Field(ge=0)
    columns: tuple[ContractColumn, ...] = Field(min_length=1)
    keys: tuple[ContractKey, ...] = ()
    relations: tuple[ContractRelation, ...] = ()

    @model_validator(mode="after")
    def validate_table(self) -> ContractTable:
        """表内 schema、键、关联与稳定 cleaned 路径必须一致。"""
        expected_cleaned_path = (
            "cleaned/"
            f"{hashlib.sha256(self.table_id.encode('utf-8')).hexdigest()[:16]}"
            ".csv"
        )
        if self.cleaned_path != expected_cleaned_path:
            raise ValueError(
                "ContractTable.cleaned_path 必须是 table_id 对应的稳定 cleaned 路径"
            )

        column_names = [column.name for column in self.columns]
        if len(set(column_names)) != len(column_names):
            raise ValueError("ContractTable 的字段不能重复")
        column_name_set = set(column_names)

        key_ids = [key.key_id for key in self.keys]
        if len(set(key_ids)) != len(key_ids):
            raise ValueError("ContractTable 的 key_id 不能重复")
        for key in self.keys:
            unknown = set(key.columns) - column_name_set
            if unknown:
                raise ValueError(f"契约键包含未知字段: {sorted(unknown)}")

        relation_ids = [relation.relation_id for relation in self.relations]
        if len(set(relation_ids)) != len(relation_ids):
            raise ValueError("ContractTable 的 relation_id 不能重复")
        for relation in self.relations:
            unknown = set(relation.source_columns) - column_name_set
            if unknown:
                raise ValueError(f"契约关联包含未知源字段: {sorted(unknown)}")
        return self


class DataContract(VersionedArtifact):
    """全部输入表验证通过后发布的冻结数据契约。"""

    artifact_type: Literal["data_contract"] = "data_contract"
    status: Literal["frozen", "no_data", "invalid"]
    tables: tuple[ContractTable, ...] = ()
    scanned_attachments: tuple[RelativePath, ...] = ()
    output_templates: tuple[RelativePath, ...] = ()
    resolved_issue_ids: tuple[StableId, ...] = ()

    @model_validator(mode="after")
    def validate_contract(self) -> DataContract:
        """冻结与 no-data 状态必须和表集合一致。"""
        if self.artifact_path != "m15/data_contract.json":
            raise ValueError("DataContract 必须使用 m15/data_contract.json")
        if not self.source_artifact_ids:
            raise ValueError("DataContract 必须声明来源产物")
        if self.status == "invalid" and self.validation_status != "failed":
            raise ValueError("invalid DataContract 必须标记 validation_status=failed")
        if self.status != "invalid" and self.validation_status != "validated":
            raise ValueError(
                "frozen/no_data DataContract 必须标记 validation_status=validated"
            )
        if self.status == "frozen" and not self.tables:
            raise ValueError("frozen DataContract 必须至少包含一张表")
        if self.status == "no_data" and self.tables:
            raise ValueError("no_data DataContract 不能包含表")
        table_ids = [table.table_id for table in self.tables]
        if len(set(table_ids)) != len(table_ids):
            raise ValueError("DataContract 的 table_id 不能重复")
        cleaned_paths = [table.cleaned_path for table in self.tables]
        if len(set(cleaned_paths)) != len(cleaned_paths):
            raise ValueError("DataContract 的 cleaned_path 不能重复")
        if len(set(self.scanned_attachments)) != len(self.scanned_attachments):
            raise ValueError("scanned_attachments 不能重复")
        if len(set(self.output_templates)) != len(self.output_templates):
            raise ValueError("output_templates 不能重复")
        overlap = set(self.scanned_attachments) & set(self.output_templates)
        if overlap:
            raise ValueError(f"输入附件和输出模板不能重叠: {sorted(overlap)}")
        unknown_sources = {table.source_path for table in self.tables} - set(
            self.scanned_attachments
        )
        if unknown_sources:
            raise ValueError(
                f"契约表来源不在已扫描输入附件中: {sorted(unknown_sources)}"
            )
        template_tables = {table.source_path for table in self.tables} & set(
            self.output_templates
        )
        if template_tables:
            raise ValueError(
                f"输出模板不能成为 DataContract 表: {sorted(template_tables)}"
            )

        table_by_id = {table.table_id: table for table in self.tables}
        relation_ids: set[str] = set()
        for table in self.tables:
            if table.data_profile_id not in self.source_artifact_ids:
                raise ValueError("DataContract 缺少表的 DataProfile 来源")
            if table.cleaning_plan_id not in self.source_artifact_ids:
                raise ValueError("DataContract 缺少表的 CleaningPlan 来源")
            for relation in table.relations:
                if relation.relation_id in relation_ids:
                    raise ValueError("DataContract 的 relation_id 不能重复")
                relation_ids.add(relation.relation_id)
                target = table_by_id.get(relation.target_table_id)
                if target is None:
                    raise ValueError("契约关联目标表不存在")
                target_columns = {column.name for column in target.columns}
                unknown = set(relation.target_columns) - target_columns
                if unknown:
                    raise ValueError(f"契约关联包含未知目标字段: {sorted(unknown)}")
        if len(set(self.resolved_issue_ids)) != len(self.resolved_issue_ids):
            raise ValueError("resolved_issue_ids 不能重复")
        missing_issue_sources = set(self.resolved_issue_ids) - set(
            self.source_artifact_ids
        )
        if missing_issue_sources:
            raise ValueError(
                f"DataContract 缺少 resolved issue 来源: {sorted(missing_issue_sources)}"
            )
        return self


class ContractDataReference(StrictFrozenModel):
    """QuestionPlan 对冻结数据表及字段的引用。"""

    table_id: NonBlankText
    columns: tuple[NonBlankText, ...] = Field(min_length=1)

    @field_validator("columns")
    @classmethod
    def validate_columns(cls, columns: tuple[str, ...]) -> tuple[str, ...]:
        """同一数据引用不能重复列。"""
        if len(set(columns)) != len(columns):
            raise ValueError("同一数据引用的列不能重复")
        return columns


class UpstreamResultReference(StrictFrozenModel):
    """QuestionPlan 对已声明上游问题结果的引用。"""

    question_id: QuestionId
    outputs: tuple[StableId, ...] = Field(min_length=1)

    @field_validator("outputs")
    @classmethod
    def validate_outputs(cls, outputs: tuple[str, ...]) -> tuple[str, ...]:
        """同一上游问题的结果声明不能重复。"""
        if len(set(outputs)) != len(outputs):
            raise ValueError("同一上游问题的结果标识不能重复")
        return outputs


class QuestionPlan(VersionedArtifact):
    """冻结 DataContract 之后生成的单题执行计划。"""

    artifact_type: Literal["question_plan"] = "question_plan"
    question_id: QuestionId
    task_outline_id: StableId
    data_contract_id: StableId
    objective: NonBlankText
    data: tuple[ContractDataReference, ...] = ()
    upstream_results: tuple[UpstreamResultReference, ...] = ()
    model: NonBlankText
    method: NonBlankText
    constraints: tuple[NonBlankText, ...] = ()
    validation: tuple[NonBlankText, ...] = Field(min_length=1)
    figures: tuple[NonBlankText, ...] = Field(min_length=1)
    deliverables: tuple[Deliverable, ...] = Field(min_length=1)
    fallback: NonBlankText

    @model_validator(mode="after")
    def validate_question_plan_sources(self) -> QuestionPlan:
        """按题计划必须直接追溯到 outline 和冻结契约。"""
        _require_sources(
            self.source_artifact_ids,
            self.task_outline_id,
            self.data_contract_id,
        )
        table_ids = [reference.table_id for reference in self.data]
        if len(set(table_ids)) != len(table_ids):
            raise ValueError("QuestionPlan 的数据表引用不能重复")
        upstream_ids = [reference.question_id for reference in self.upstream_results]
        if len(set(upstream_ids)) != len(upstream_ids):
            raise ValueError("上游问题引用不能重复")
        if self.question_id in upstream_ids:
            raise ValueError("QuestionPlan 不能引用自身结果")
        return self


M15Artifact = (
    TaskFacts
    | TaskOutline
    | DataProfile
    | CleaningPlan
    | DataIssue
    | DataContract
    | QuestionPlan
)
M15_ARTIFACT_TYPES: tuple[type[VersionedArtifact], ...] = (
    TaskFacts,
    TaskOutline,
    DataProfile,
    CleaningPlan,
    DataIssue,
    DataContract,
    QuestionPlan,
)


def _require_sources(
    source_artifact_ids: tuple[str, ...],
    *required_ids: str,
) -> None:
    """要求显式来源字段同时出现在共享来源元数据中。"""
    missing = set(required_ids) - set(source_artifact_ids)
    if missing:
        raise ValueError(f"缺少来源产物标识: {sorted(missing)}")
