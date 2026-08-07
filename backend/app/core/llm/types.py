"""LLM 响应标准化类型定义。"""

from dataclasses import dataclass, field


@dataclass
class ToolCall:
    """标准化工具调用。"""

    id: str
    name: str
    arguments: str  # JSON string


@dataclass
class Usage:
    """Token 用量。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0  # Anthropic cache_read / OpenAI cached_tokens
    reasoning_tokens: int = 0  # DeepSeek reasoning / o1 reasoning_tokens


@dataclass
class StandardResponse:
    """LLM 响应的标准化格式。

    Agent 侧统一使用此格式访问 LLM 结果，不感知底层 API 差异。
    """

    content: str | None = None
    reasoning_content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
