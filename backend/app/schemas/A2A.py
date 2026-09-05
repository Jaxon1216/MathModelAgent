"""Agent 间通信数据模型定义。"""

from typing import Literal

from pydantic import BaseModel, Field


class CoordinatorToModeler(BaseModel):
    """协调者传递给建模手的数据结构。"""

    questions: dict
    ques_count: int


class ModelerToCoder(BaseModel):
    """建模手传递给代码手的数据结构。"""

    questions_solution: dict[str, str]


class CoderToWriter(BaseModel):
    """代码手传递给写作手的数据结构。"""

    status: Literal["success", "partial"] = "success"

    code_response: str | None = None
    code_output: str | None = None
    created_images: list[str] = Field(default_factory=list)
    created_artifacts: list[str] = Field(default_factory=list)
    verified_metrics: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    last_error: str | None = None


class ReferenceEvidence(BaseModel):
    """Writer 可引用的已检索文献。"""

    key: str
    openalex_id: str
    doi: str | None = None
    title: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    canonical_citation: str


class WriterResponse(BaseModel):
    """写作手的响应数据结构。"""

    status: Literal["success", "degraded"] = "success"
    response_content: str = ""
    footnotes: list[tuple[str, str]] | None = None
    references: list[ReferenceEvidence] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
