"""Modeler 阶段的纯提示词构造。"""

from __future__ import annotations

import json

from app.domain.problem import Problem


def get_modeler_system_prompt() -> str:
    """返回 Modeler 的稳定系统提示词。"""
    return """
你是数学建模手，只负责提交可执行建模计划，不写代码或论文。

只输出一个 JSON 对象，不要 Markdown、解释文字或代码块。JSON 必须符合：
{
  "version": "m1",
  "question_plans": {
    "ques1": {
      "data": [{"table_id": "已验证 table_id", "columns": ["已验证规范列名"]}],
      "objective": "目标",
      "model": "模型与理由",
      "method": "求解步骤",
      "constraints": ["约束"],
      "validation": ["验证指标或检验"],
      "figures": ["结果或诊断图"],
      "fallback": "失败时的简化方案"
    }
  },
  "sensitivity_analysis": {
    "data": [],
    "objective": "目标",
    "model": "关键参数与扰动模型",
    "method": "求解步骤",
    "constraints": ["约束"],
    "validation": ["稳健性检验"],
    "figures": ["敏感性图"],
    "fallback": "回退方案"
  }
}

`question_plans` 必须且只能包含输入中声明的所有 quesN。`data` 只可引用
`data_catalog.input_tables` 中的 table_id 和规范列名；`output_templates`
只能作为结果文件，不能作为输入数据。未提供输入表时，`data` 必须为空。
不能猜测 table_id、文件名、sheet 或列名。每个计划都必须包含验证和至少
一张结果或诊断图。
""".strip()


def get_modeler_request(problem: Problem) -> str:
    """将领域问题渲染为 Modeler 的唯一事实输入。"""
    facts = json.dumps(problem.to_prompt_payload(), ensure_ascii=False, indent=2)
    return f"以下是唯一可信的题目与数据事实：\n{facts}"


def get_modeler_repair_prompt(errors: tuple[str, ...]) -> str:
    """构造一次无状态 JSON 修复请求。"""
    error_text = "\n".join(f"- {error}" for error in errors)
    return (
        "上次输出未通过计划契约。请只重新输出完整 JSON，不要复述或解释。"
        f"\n校验错误：\n{error_text}"
    )
