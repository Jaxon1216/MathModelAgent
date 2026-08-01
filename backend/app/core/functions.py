"""工具函数定义模块，为各 Agent 提供可用的工具 schema。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.skills.loader import SkillLoader

# ---- OpenAI 格式（Chat Completions + Responses 共用） ----

_EXECUTE_CODE_TOOL = {
    "type": "function",
    "function": {
        "name": "execute_code",
        "description": (
            "Execute Python code in the Jupyter kernel and return terminal output. "
            "If the code generates image output the function returns '[image]'. "
            "The kernel remains active after execution, retaining all variables in memory. "
            "Store plots/images to the working directory instead of showing them interactively."
        ),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "The Python code to execute"}
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    },
}

_EXECUTE_CODE_TOOL_ANTHROPIC = {
    "name": "execute_code",
    "description": (
        "Execute Python code in the Jupyter kernel and return terminal output. "
        "If the code generates image output the function returns '[image]'. "
        "The kernel remains active after execution, retaining all variables in memory. "
        "Store plots/images to the working directory instead of showing them interactively."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "The Python code to execute"}
        },
        "required": ["code"],
    },
}


def _build_load_skill_tool(skill_descriptions: str) -> dict:
    """构造 OpenAI 格式的 load_skill 工具 schema。

    Args:
        skill_descriptions: 由 SkillLoader.get_descriptions() 返回的技能列表文本。

    Returns:
        OpenAI function calling 格式的工具 schema 字典。
    """
    return {
        "type": "function",
        "function": {
            "name": "load_skill",
            "description": (
                "Load a skill to get detailed domain knowledge and instructions.\n\n"
                "Available skills:\n"
                f"{skill_descriptions}\n\n"
                "When to call: call BEFORE starting the corresponding task type. "
                "E.g. call load_skill('eda') before exploratory data analysis, "
                "load_skill('visualization') before creating any figure."
            ),
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_name": {
                        "type": "string",
                        "description": "Name of the skill to load (e.g. 'eda', 'visualization')",
                    }
                },
                "required": ["skill_name"],
                "additionalProperties": False,
            },
        },
    }


def _build_load_skill_tool_anthropic(skill_descriptions: str) -> dict:
    """构造 Anthropic 格式的 load_skill 工具 schema。

    Args:
        skill_descriptions: 由 SkillLoader.get_descriptions() 返回的技能列表文本。

    Returns:
        Anthropic tool 格式的 schema 字典。
    """
    return {
        "name": "load_skill",
        "description": (
            "Load a skill to get detailed domain knowledge and instructions.\n\n"
            "Available skills:\n"
            f"{skill_descriptions}\n\n"
            "When to call: call BEFORE starting the corresponding task type. "
            "E.g. call load_skill('eda') before exploratory data analysis, "
            "load_skill('visualization') before creating any figure."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": "Name of the skill to load (e.g. 'eda', 'visualization')",
                }
            },
            "required": ["skill_name"],
        },
    }


def get_coder_tools(skill_loader: SkillLoader) -> list:
    """构造 CoderAgent 的 OpenAI 格式工具列表（包含 load_skill）。

    Args:
        skill_loader: 已初始化的 SkillLoader 实例。

    Returns:
        OpenAI function calling 格式的工具 schema 列表。
    """
    return [
        _EXECUTE_CODE_TOOL,
        _build_load_skill_tool(skill_loader.get_descriptions()),
    ]


def get_coder_tools_anthropic(skill_loader: SkillLoader) -> list:
    """构造 CoderAgent 的 Anthropic 格式工具列表（包含 load_skill）。

    Args:
        skill_loader: 已初始化的 SkillLoader 实例。

    Returns:
        Anthropic tool 格式的工具 schema 列表。
    """
    return [
        _EXECUTE_CODE_TOOL_ANTHROPIC,
        _build_load_skill_tool_anthropic(skill_loader.get_descriptions()),
    ]


# 向后兼容：保留旧常量供非 CoderAgent 代码使用
coder_tools = [_EXECUTE_CODE_TOOL]
coder_tools_anthropic = [_EXECUTE_CODE_TOOL_ANTHROPIC]

writer_tools = [
    {
        "type": "function",
        "function": {
            "name": "search_papers",
            "description": "Search for papers using a query string.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The query string"}
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
]

writer_tools_anthropic = [
    {
        "name": "search_papers",
        "description": "Search for papers using a query string.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The query string"}
            },
            "required": ["query"],
        },
    },
]
