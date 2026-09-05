"""M2 Coder 终态 ResultPackage 领域契约。"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ResultPackageStatus = Literal["success", "partial"]
ArtifactKind = Literal[
    "code",
    "stdout",
    "image",
    "csv",
    "spreadsheet",
    "array",
    "file",
]
FailureKind = Literal[
    "retry_exhausted",
    "turn_limit",
    "execution_error",
    "result_package_persist",
]


def _validate_relative_path(value: str) -> str:
    """验证产物路径保持在任务工作目录内。"""
    candidate = PurePosixPath(value)
    if (
        not value
        or value != value.strip()
        or "\\" in value
        or candidate.is_absolute()
        or ".." in candidate.parts
        or "." in candidate.parts
    ):
        raise ValueError("ResultPackage 路径必须是安全的 POSIX 相对路径")
    return value


class StrictResultPackageModel(BaseModel):
    """ResultPackage 的严格、只读模型基类。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ResultArtifact(StrictResultPackageModel):
    """一个已落盘且带内容指纹的阶段文件。"""

    path: str
    kind: ArtifactKind
    bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    _validate_path = field_validator("path")(_validate_relative_path)


class ResultMetric(StrictResultPackageModel):
    """从阶段 stdout 快照提取的有限指标。"""

    statement: str = Field(min_length=1, max_length=300)
    unit: str | None = Field(default=None, max_length=80)
    source_path: str
    source_line: int = Field(ge=1)

    _validate_source_path = field_validator("source_path")(_validate_relative_path)


class FailureEvidence(StrictResultPackageModel):
    """partial package 中可定位的终态失败证据。"""

    kind: FailureKind
    message: str = Field(min_length=1, max_length=2000)


class ResultPackage(StrictResultPackageModel):
    """单个 Coder phase 的版本化终态结果。"""

    schema_version: Literal["m2"] = "m2"
    task_id: str = Field(min_length=1, max_length=256)
    phase: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    status: ResultPackageStatus
    created_at: str = Field(min_length=1)
    package_path: str
    code: ResultArtifact
    stdout: ResultArtifact
    artifacts: tuple[ResultArtifact, ...] = ()
    figures: tuple[ResultArtifact, ...] = ()
    metrics: tuple[ResultMetric, ...] = ()
    limitations: tuple[str, ...] = ()
    failure: FailureEvidence | None = None
    summary: str = Field(default="", max_length=2000)

    _validate_package_path = field_validator("package_path")(_validate_relative_path)

    @model_validator(mode="after")
    def validate_package_consistency(self) -> ResultPackage:
        """确保成功/partial 状态、文件清单和图表索引一致。"""
        if self.status == "success" and self.failure is not None:
            raise ValueError("success ResultPackage 不能包含失败证据")
        if self.status == "partial" and self.failure is None:
            raise ValueError("partial ResultPackage 必须包含失败证据")

        artifact_paths = [artifact.path for artifact in self.artifacts]
        if len(set(artifact_paths)) != len(artifact_paths):
            raise ValueError("ResultPackage artifacts 不能重复")
        figure_paths = [figure.path for figure in self.figures]
        if len(set(figure_paths)) != len(figure_paths):
            raise ValueError("ResultPackage figures 不能重复")
        if not set(figure_paths).issubset(set(artifact_paths)):
            raise ValueError("ResultPackage figures 必须属于 artifacts")
        if any(figure.kind != "image" for figure in self.figures):
            raise ValueError("ResultPackage figures 必须是 image")
        if any(metric.source_path != self.stdout.path for metric in self.metrics):
            raise ValueError("ResultPackage metrics 必须来源于 stdout 快照")
        return self
