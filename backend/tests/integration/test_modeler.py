"""Modeler Agent 与领域契约的集成测试。"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.agents.modeler import ModelerAgent, ModelerResponseError
from app.domain.problem import DataCatalog, Problem, QuestionSet
from app.orchestration.workflow import ModelerStageError, ModelerWorkflow
from app.runtime.llm.client import LegacyLLMClient


class FakeLLMClient:
    """记录请求并按顺序返回预设文本的 LLM 边界替身。"""

    def __init__(self, responses: list[str]) -> None:
        self._responses = iter(responses)
        self.messages: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]]) -> str:
        """记录请求并返回下一条响应。"""
        self.messages.append(list(messages))
        return next(self._responses)


class LegacyLLMStub:
    """记录新适配器发出的旧 LLM 调用。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        """返回符合旧 LLM 响应形状的最小对象。"""
        self.calls.append(kwargs)
        return SimpleNamespace(content="{}")


def _problem() -> Problem:
    """构造单问题的 Modeler 输入。"""
    return Problem(
        task_id="modeler-integration",
        question_set=QuestionSet(
            question_count=1,
            questions={"ques1": "预测下一年度产量。"},
            background="农作物种植策略",
        ),
        data_catalog=DataCatalog(
            files=("crop.csv",),
            columns_by_file={"crop.csv": ("year", "yield")},
        ),
    )


def _payload() -> dict:
    """构造可通过领域校验的模型响应。"""
    section = {
        "data": [{"file": "crop.csv", "columns": ["year", "yield"]}],
        "objective": "预测下一年度产量",
        "model": "趋势回归模型",
        "method": "拟合趋势并外推",
        "constraints": ["产量非负"],
        "validation": ["留出年度误差"],
        "figures": ["预测趋势图"],
        "fallback": "使用最近年份均值",
    }
    return {
        "version": "m1",
        "question_plans": {"ques1": section},
        "sensitivity_analysis": {
            **section,
            "objective": "评估趋势参数扰动",
            "figures": ["参数敏感性曲线"],
        },
    }


def test_modeler_repairs_once_without_retaining_invalid_response():
    """Modeler 只给一次修复机会，修复请求不回灌无效模型正文。"""
    invalid_response = "这不是 JSON"
    client = FakeLLMClient(
        [invalid_response, json.dumps(_payload(), ensure_ascii=False)]
    )

    plan = asyncio.run(ModelerWorkflow(ModelerAgent(client)).create_plan(_problem()))

    assert set(plan.question_plans) == {"ques1"}
    assert len(client.messages) == 2
    assert len(client.messages[0]) == 2
    assert len(client.messages[1]) == 3
    assert "invalid_json" in client.messages[1][-1]["content"]
    assert invalid_response not in str(client.messages[1])


def test_modeler_stage_stops_after_one_failed_repair():
    """两次非法输出后，编排层必须以明确阶段失败停止。"""
    client = FakeLLMClient(["{}", "{}"])

    with pytest.raises(ModelerStageError, match="after 2 attempts") as exc_info:
        asyncio.run(ModelerWorkflow(ModelerAgent(client)).create_plan(_problem()))

    assert isinstance(exc_info.value.__cause__, ModelerResponseError)
    assert len(client.messages) == 2


def test_legacy_llm_adapter_disables_provider_retries():
    """唯一旧 LLM 适配器必须保留 Modeler 身份并禁用内部重试。"""
    legacy_llm = LegacyLLMStub()

    response = asyncio.run(
        LegacyLLMClient(legacy_llm).complete(
            [{"role": "user", "content": "返回 JSON"}]
        )
    )

    assert response == "{}"
    assert legacy_llm.calls == [
        {
            "history": [{"role": "user", "content": "返回 JSON"}],
            "agent_name": "ModelerAgent",
            "max_retries": 0,
        }
    ]
