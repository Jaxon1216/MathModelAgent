"""M1.5 受约束数据清洗规划提示词。"""

from __future__ import annotations

import json

from app.domain.m15 import CleaningPlan, DataIssue, DataProfile, TaskOutline


def get_cleaning_system_prompt() -> str:
    """返回只允许声明有限清洗操作的稳定系统提示词。"""
    return """
你是 Modeler 的数据清洗规划步骤。你只声明计划，不执行代码。

只输出一个 JSON 对象，不要 Markdown、代码、表达式或解释性前后缀：
{
  "plans": [
    {
      "table_id": "<输入 DataProfile.table_id>",
      "operations": [
        {
          "operation_id": "clean-op:<稳定小写标识>",
          "operation_type": "cast_type | fill_missing | drop_empty_rows | drop_duplicates | normalize_join_key",
          "target_columns": ["<本表规范列名>"],
          "target_type": null,
          "strategy": null,
          "fill_value": null,
          "keep": null,
          "normalizations": [],
          "reason": "<基于画像事实的理由>",
          "postconditions": [
            {
              "rule_id": "rule:<稳定小写标识>",
              "rule_type": "columns_exist | canonical_type | row_count | key_unique | key_non_null | missing_count | duplicate_count | join_compatible | source_sha256",
              "columns": [],
              "target_table_id": null,
              "target_columns": [],
              "operator": "eq | le | ge",
              "expected": true
            }
          ]
        }
      ],
      "no_op_reason": null
    }
  ]
}

每张 DataProfile 必须恰好有一个计划。无须清洗时 operations 为空并填写
no_op_reason；有操作时 no_op_reason 必须为 null。

白名单参数：
- cast_type：strategy="strict"，target_type 为 string/integer/number/boolean/date/datetime/category。
- fill_missing：strategy 只能是 forward_fill/backward_fill/constant/mean/median/mode；
  只有 constant 可提供非空 fill_value。
- drop_empty_rows：strategy="strict"，仅删除 target_columns 全部缺失的行。
- drop_duplicates：只按 target_columns 去重，keep 只能是 first/last。
- normalize_join_key：normalizations 只能按顺序选 strip/casefold/unicode_nfkc/collapse_whitespace。
  target_columns 必须与当前 DataProfile.join_evidence 中某一条 source_columns 完全一致。
  不得把多条关联证据中的字段合并为同一次操作，也不得规范化仅名称看似关联的字段。

每项操作必须有 reason 和至少一个可机器验证的 postcondition。只可引用当前表
字段；join_compatible 的目标必须来自 DataProfile.join_evidence。禁止声明任何
文件路径、Python/SQL/正则表达式、函数、命令、脚本、跨表写入或白名单外操作。
说明列高缺失本身不构成填充理由；只有画像和题目语义支持时才能修改。合并单元格
造成的键列缺失可定向 forward_fill，空行和重复行必须使用各自白名单操作。
""".strip()


def get_cleaning_request(
    outline: TaskOutline,
    profiles: tuple[DataProfile, ...],
) -> str:
    """只渲染 TaskOutline 与 DataProfile，不加入工作目录或其他上下文。"""
    payload = {
        "task_outline": outline.model_dump(mode="json"),
        "data_profiles": [profile.model_dump(mode="json") for profile in profiles],
    }
    return "以下是唯一可信的规划输入：\n" + json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
    )


def get_cleaning_repair_prompt(errors: tuple[str, ...]) -> str:
    """构造不包含无效模型原文的无状态 JSON 格式修复请求。"""
    details = "\n".join(f"- {error}" for error in errors)
    return (
        "上次输出未通过 CleaningPlan 契约。只重新输出完整 JSON，不要解释，"
        f"不要复述无效输出。\n校验错误：\n{details}"
    )


def get_table_repair_system_prompt() -> str:
    """返回严格限制在失败表和失败规则内的 repair 提示词。"""
    return (
        get_cleaning_system_prompt()
        + "\n\n当前请求是局部 Repair。只输出一个 plan 对象，不要输出 plans 包装。"
        "未失败操作及其规则必须逐项原样保留。"
        "若一个失败操作无法由白名单安全修复，可删除该操作及其失败规则；"
        "不要复用或重新定义已删除的失败 rule_id。"
        "保留失败 rule_id 时，其规则定义和目标列也必须原样保留。"
        "只能修改仅包含失败规则的操作。"
        "禁止引用或修改其他表。"
    )


def get_table_repair_request(
    profile: DataProfile,
    plan: CleaningPlan,
    issues: tuple[DataIssue, ...],
) -> str:
    """只向 Repair 暴露失败表画像、原计划和该表失败证据。"""
    payload = {
        "failed_table_profile": profile.model_dump(mode="json"),
        "current_plan": plan.model_dump(mode="json"),
        "failed_issues": [issue.model_dump(mode="json") for issue in issues],
    }
    return "以下是本次 Repair 的全部授权输入：\n" + json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
    )
