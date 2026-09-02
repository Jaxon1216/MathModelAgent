"""Agent 间通信数据模型定义。"""

from typing import Any

from pydantic import BaseModel, Field


class CoordinatorToModeler(BaseModel):
    """协调者传递给建模手的数据结构。"""

    questions: dict[str, Any]
    ques_count: int
    constraints: list[str] = Field(default_factory=list)


class ModelerToCoder(BaseModel):
    """建模手传递给代码手的数据结构。"""

    questions_solution: dict[str, str]
    constraints: list[str] = Field(default_factory=list)


class CoderToWriter(BaseModel):
    """代码手传递给写作手的数据结构。"""

    status: str = "success"
    code_response: str | None = None
    code_output: str | None = None
    created_images: list[str] | None = None
    phase_result: dict[str, Any] | None = None


class WriterResponse(BaseModel):
    """写作手的响应数据结构。"""

    status: str = "success"
    response_content: Any
    footnotes: list[tuple[str, str]] | None = None
