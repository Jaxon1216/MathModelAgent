"""Modeler 阶段的纯提示词构造。"""

from __future__ import annotations

import json

from app.domain.m15 import (
    DataContract,
    TaskOutline,
    UpstreamResultReference,
)
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


def get_question_plan_system_prompt() -> str:
    """返回 M1.5 冻结契约后的按题规划提示词。"""
    return """
你是数学建模手，只负责根据已冻结事实提交按题执行计划，不写代码或论文。

只输出一个 JSON 对象，不要 Markdown、解释文字或代码块：
{
  "schema_version": "m1.5",
  "question_plans": [
    {
      "question_id": "ques2",
      "data": [{"table_id": "契约 table_id", "columns": ["契约规范列名"]}],
      "upstream_results": [
        {"question_id": "ques1", "outputs": ["output:declared"]}
      ],
      "objective": "单题目标",
      "model": "模型与选择理由",
      "method": "可执行求解步骤",
      "constraints": ["约束"],
      "validation": ["验证指标或检验"],
      "figures": ["结果或诊断图"],
      "deliverables": [
        {
          "deliverable_id": "outline 中的原标识",
          "description": "outline 中的原描述",
          "path": null
        }
      ],
      "fallback": "失败时的简化方案"
    }
  ],
  "sensitivity_analysis": {
    "data": [],
    "objective": "敏感性分析目标",
    "model": "参数扰动模型",
    "method": "求解步骤",
    "constraints": ["约束"],
    "validation": ["稳健性检验"],
    "figures": ["敏感性图"],
    "fallback": "回退方案"
  }
}

question_plans 必须恰好覆盖 TaskOutline.questions，每题一次。data 只能使用
DataContract.tables 中的 table_id 和 columns.name；不得添加 path/file/sheet，
不得引用 source_path、原始附件或输出模板。执行时只能使用契约 cleaned_path，
该路径由系统桥接，不需要也不允许你在输出中声明。no_data 契约下所有 data
必须为空。

upstream_results 只能使用该题 allowed_upstream_results 中列出的传递依赖及
output 标识；无声明时必须为空。deliverables 必须逐项原样复制该题 outline
交付物。每个计划和 sensitivity_analysis 都必须包含验证和至少一张图。
""".strip()


def get_question_plan_request(
    outline: TaskOutline,
    data_contract: DataContract,
    upstream_result_declarations: tuple[UpstreamResultReference, ...],
) -> str:
    """只投影按题规划允许读取的 outline、契约和上游声明。"""
    declarations = {
        declaration.question_id: declaration.model_dump(mode="json")
        for declaration in upstream_result_declarations
    }
    question_by_id = {question.question_id: question for question in outline.questions}

    def transitive_dependencies(question_id: str) -> set[str]:
        dependencies: set[str] = set()

        def collect(current_id: str) -> None:
            for dependency_id in question_by_id[current_id].depends_on:
                if dependency_id in dependencies:
                    continue
                dependencies.add(dependency_id)
                collect(dependency_id)

        collect(question_id)
        return dependencies

    outline_payload = {
        "artifact_id": outline.artifact_id,
        "questions": [
            {
                "question_id": question.question_id,
                "text": question.text,
                "depends_on": list(question.depends_on),
                "order_key": question.order_key,
                "deliverables": [
                    deliverable.model_dump(mode="json")
                    for deliverable in question.deliverables
                ],
                "allowed_upstream_results": [
                    declarations[dependency_id]
                    for dependency_id in transitive_dependencies(question.question_id)
                    if dependency_id in declarations
                ],
            }
            for question in outline.questions
        ],
    }
    contract_payload = {
        "artifact_id": data_contract.artifact_id,
        "status": data_contract.status,
        "tables": [
            {
                "table_id": table.table_id,
                "cleaned_path": table.cleaned_path,
                "row_count": table.row_count,
                "columns": [
                    {
                        "name": column.name,
                        "canonical_type": column.canonical_type,
                        "nullable": column.nullable,
                        "statistic_definition": column.statistic_definition,
                    }
                    for column in table.columns
                ],
                "keys": [
                    {
                        "key_id": key.key_id,
                        "columns": list(key.columns),
                        "kind": key.kind,
                    }
                    for key in table.keys
                ],
                "relations": [
                    {
                        "relation_id": relation.relation_id,
                        "source_columns": list(relation.source_columns),
                        "target_table_id": relation.target_table_id,
                        "target_columns": list(relation.target_columns),
                    }
                    for relation in table.relations
                ],
            }
            for table in data_contract.tables
        ],
    }
    payload = {
        "task_outline": outline_payload,
        "data_contract": contract_payload,
    }
    return "以下是唯一可信的按题规划输入：\n" + json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
    )


def get_question_plan_repair_prompt(errors: tuple[str, ...]) -> str:
    """构造不回灌无效正文的 M1.5 无状态 JSON 修复请求。"""
    error_text = "\n".join(f"- {error}" for error in errors)
    return (
        "上次输出未通过 QuestionPlan 集合契约。只重新输出完整 JSON，"
        f"不要复述或解释无效输出。\n校验错误：\n{error_text}"
    )
