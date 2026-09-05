"""唯一 2024 高教杯 C 题附件的 M1.5 离线验收测试。"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from app.agents.cleaning_planner import CleaningPlannerAgent
from app.agents.modeler import ModelerAgent
from app.core.agents.coordinator_agent import CoordinatorAgent
from app.core.flows import Flows
from app.core.llm.types import StandardResponse
from app.data import M15ArtifactStore, compute_file_sha256
from app.domain.m15 import DataContract, TaskFacts
from app.orchestration.m15_workflow import M15Workflow
from app.orchestration.task_outline import TaskOutlineWorkflow
from app.schemas.A2A import ModelerToCoder

BACKEND_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = BACKEND_ROOT / "app" / "example" / "example" / "2024高教杯C题"
INPUT_FILES = ("附件1.xlsx", "附件2.xlsx")
OUTPUT_TEMPLATES = ("result1_1.xlsx", "result1_2.xlsx", "result2.xlsx")
EXPECTED_TABLES = (
    "附件1.xlsx::乡村的现有耕地",
    "附件1.xlsx::乡村种植的农作物",
    "附件2.xlsx::2023年的农作物种植情况",
    "附件2.xlsx::2023年统计的相关数据",
)


class FixedCoordinatorLLM:
    """只返回固定 TaskOutline，不访问网络。"""

    def __init__(self, response: str) -> None:
        self._response = response
        self.calls = 0

    async def chat(self, **kwargs) -> StandardResponse:
        """返回唯一固定响应。"""
        assert kwargs["history"]
        self.calls += 1
        return StandardResponse(content=self._response)


class FixedModelerLLM:
    """依次返回固定 CleaningPlan 和 QuestionPlan，不访问网络。"""

    def __init__(self, responses: tuple[str, ...]) -> None:
        self._responses = iter(responses)
        self.calls = 0

    async def complete(self, messages: list[dict[str, str]]) -> str:
        """返回下一条固定响应。"""
        assert messages
        self.calls += 1
        return next(self._responses)


def _file_state(path: Path) -> tuple[str, int]:
    """返回文件内容指纹与纳秒级修改时间。"""
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def _deliverable(
    deliverable_id: str,
    description: str,
    path: str | None = None,
) -> dict[str, object]:
    """构造固定 TaskOutline/QuestionPlan 共用的交付物。"""
    return {
        "deliverable_id": deliverable_id,
        "description": description,
        "path": path,
    }


def _outline_payload() -> str:
    """构造覆盖 ques1-3 且含显式依赖的固定 TaskOutline。"""
    result1_1 = _deliverable(
        "deliverable:result1-1",
        "问题一情形一结果模板",
        "result1_1.xlsx",
    )
    result1_2 = _deliverable(
        "deliverable:result1-2",
        "问题一情形二结果模板",
        "result1_2.xlsx",
    )
    result2 = _deliverable(
        "deliverable:result2",
        "问题二结果模板",
        "result2.xlsx",
    )
    comparison = _deliverable(
        "deliverable:comparison",
        "问题三与问题二的比较分析",
    )
    return json.dumps(
        {
            "schema_version": "m1.5",
            "artifact_id": "task-outline:root",
            "source_artifact_ids": ["task-facts:root"],
            "validation_status": "validated",
            "artifact_path": "m15/task_outline.json",
            "task_facts_id": "task-facts:root",
            "questions": [
                {
                    "question_id": "ques3",
                    "text": "问题 3 在现实生活中，各种农作物之间可能存在一定的可替代性和互补性",
                    "depends_on": ["ques2"],
                    "order_key": 1,
                    "deliverables": [comparison],
                },
                {
                    "question_id": "ques1",
                    "text": "问题 1 假定各种农作物未来的预期销售量",
                    "depends_on": [],
                    "order_key": 3,
                    "deliverables": [result1_1, result1_2],
                },
                {
                    "question_id": "ques2",
                    "text": "问题 2 根据经验，小麦和玉米未来的预期销售量有增长的趋势",
                    "depends_on": ["ques1"],
                    "order_key": 2,
                    "deliverables": [result2],
                },
            ],
            "deliverables": [result1_1, result1_2, result2, comparison],
        },
        ensure_ascii=False,
    )


def _cleaning_payload() -> str:
    """为四张真实输入 Sheet 固定生成显式 no-op 计划。"""
    return json.dumps(
        {
            "plans": [
                {
                    "table_id": table_id,
                    "operations": [],
                    "no_op_reason": "离线验收保留画像事实并生成独立 cleaned 副本。",
                }
                for table_id in EXPECTED_TABLES
            ]
        },
        ensure_ascii=False,
    )


def _question_plan_payload() -> str:
    """构造只引用冻结契约字段和声明上游结果的固定计划。"""
    land = {
        "table_id": EXPECTED_TABLES[0],
        "columns": ["地块名称", "地块类型", "地块面积/亩"],
    }
    crops = {
        "table_id": EXPECTED_TABLES[1],
        "columns": ["作物编号", "作物名称", "作物类型", "种植耕地"],
    }
    planting = {
        "table_id": EXPECTED_TABLES[2],
        "columns": ["种植地块", "作物编号", "种植面积/亩", "种植季次"],
    }
    statistics = {
        "table_id": EXPECTED_TABLES[3],
        "columns": [
            "作物编号",
            "地块类型",
            "种植季次",
            "亩产量/斤",
            "种植成本/(元/亩)",
            "销售单价/(元/斤)",
        ],
    }

    def content(
        objective: str,
        data: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "data": data,
            "objective": objective,
            "model": "混合整数规划与情景分析",
            "method": "仅使用冻结 cleaned 表构造约束、目标并验证结果",
            "constraints": ["满足地块、季次、轮作和豆类种植约束"],
            "validation": ["复核面积守恒、作物适配和依赖结果"],
            "figures": ["年度收益与种植结构图"],
            "fallback": "保留可行基线并报告限制",
        }

    return json.dumps(
        {
            "schema_version": "m1.5",
            "question_plans": [
                {
                    "question_id": "ques1",
                    **content(
                        "求解稳定参数下两种销售情形",
                        [land, crops, planting, statistics],
                    ),
                    "upstream_results": [],
                    "deliverables": [
                        _deliverable(
                            "deliverable:result1-1",
                            "问题一情形一结果模板",
                            "result1_1.xlsx",
                        ),
                        _deliverable(
                            "deliverable:result1-2",
                            "问题一情形二结果模板",
                            "result1_2.xlsx",
                        ),
                    ],
                },
                {
                    "question_id": "ques2",
                    **content(
                        "求解不确定参数下的稳健种植方案",
                        [land, crops, planting, statistics],
                    ),
                    "upstream_results": [
                        {
                            "question_id": "ques1",
                            "outputs": [
                                "deliverable:result1-1",
                                "deliverable:result1-2",
                            ],
                        }
                    ],
                    "deliverables": [
                        _deliverable(
                            "deliverable:result2",
                            "问题二结果模板",
                            "result2.xlsx",
                        )
                    ],
                },
                {
                    "question_id": "ques3",
                    **content("分析相关性并与问题二比较", [planting, statistics]),
                    "upstream_results": [
                        {
                            "question_id": "ques2",
                            "outputs": ["deliverable:result2"],
                        }
                    ],
                    "deliverables": [
                        _deliverable(
                            "deliverable:comparison",
                            "问题三与问题二的比较分析",
                        )
                    ],
                },
            ],
            "sensitivity_analysis": content(
                "评估关键参数扰动对方案的影响",
                [statistics],
            ),
        },
        ensure_ascii=False,
    )


def _transitive_dependencies(
    question_id: str,
    direct_dependencies: dict[str, set[str]],
) -> set[str]:
    """计算测试断言使用的传递依赖集合。"""
    result: set[str] = set()
    pending = list(direct_dependencies[question_id])
    while pending:
        dependency = pending.pop()
        if dependency in result:
            continue
        result.add(dependency)
        pending.extend(direct_dependencies[dependency])
    return result


@pytest.mark.asyncio
async def test_real_c_problem_fixture_completes_offline_m15_contract(
    tmp_path: Path,
):
    """真实附件离线形成四表冻结契约和合法三问计划。"""
    fixture_paths = tuple(
        FIXTURE_ROOT / filename for filename in (*INPUT_FILES, *OUTPUT_TEMPLATES)
    )
    fixture_before = {path.name: _file_state(path) for path in fixture_paths}
    for source in FIXTURE_ROOT.iterdir():
        if source.is_file():
            shutil.copy2(source, tmp_path / source.name)
    copied_before = {
        path.name: _file_state(tmp_path / path.name) for path in fixture_paths
    }

    problem_text = (FIXTURE_ROOT / "questions.txt").read_text(encoding="utf-8")
    coordinator_llm = FixedCoordinatorLLM(_outline_payload())
    outline_result = await TaskOutlineWorkflow(
        CoordinatorAgent(
            "m15-real-fixture",
            coordinator_llm,  # type: ignore[arg-type]
            max_repair_attempts=0,
        )
    ).create_outline(
        task_id="m15-real-fixture",
        problem_text=problem_text,
        work_dir=tmp_path,
    )
    modeler_llm = FixedModelerLLM((_cleaning_payload(), _question_plan_payload()))
    result = await M15Workflow(
        CleaningPlannerAgent(modeler_llm, max_json_repair_attempts=0),
        ModelerAgent(modeler_llm, max_repair_attempts=0),
    ).run(
        task_id="m15-real-fixture",
        work_dir=tmp_path,
        task_facts=outline_result.task_facts,
        outline=outline_result.outline,
    )

    assert coordinator_llm.calls == 1
    assert modeler_llm.calls == 2
    assert tuple(profile.table_id for profile in result.profiles) == EXPECTED_TABLES
    assert tuple(plan.table_id for plan in result.cleaning.plans) == EXPECTED_TABLES
    assert (
        tuple(table.table_id for table in result.data_contract.tables)
        == EXPECTED_TABLES
    )
    assert result.data_contract.status == "frozen"
    assert result.data_contract.output_templates == OUTPUT_TEMPLATES
    assert not set(OUTPUT_TEMPLATES) & {
        profile.source_path for profile in result.profiles
    }
    assert not set(OUTPUT_TEMPLATES) & {
        table.source_path for table in result.data_contract.tables
    }

    cleaned_by_table = {
        cleaned.table_id: cleaned for cleaned in result.cleaning.cleaned_tables
    }
    for table in result.data_contract.tables:
        cleaned = cleaned_by_table[table.table_id]
        assert (tmp_path / table.cleaned_path).is_file()
        assert compute_file_sha256(tmp_path / table.cleaned_path) == cleaned.sha256
        assert table.cleaned_sha256 == cleaned.sha256
        assert table.source_sha256 == copied_before[table.source_path][0]

    persisted_contract = M15ArtifactStore(tmp_path).read_json(
        "m15/data_contract.json",
        DataContract,
    )
    assert persisted_contract == result.data_contract
    assert {plan.question_id for plan in result.question_plans.question_plans} == {
        "ques1",
        "ques2",
        "ques3",
    }

    contract_columns = {
        table.table_id: {column.name for column in table.columns}
        for table in result.data_contract.tables
    }
    direct_dependencies = {
        question.question_id: set(question.depends_on)
        for question in outline_result.outline.questions
    }
    output_ids = {
        question.question_id: {
            deliverable.deliverable_id for deliverable in question.deliverables
        }
        for question in outline_result.outline.questions
    }
    for plan in result.question_plans.question_plans:
        for reference in plan.data:
            assert reference.table_id in contract_columns
            assert set(reference.columns) <= contract_columns[reference.table_id]
        allowed_upstream = _transitive_dependencies(
            plan.question_id,
            direct_dependencies,
        )
        for upstream in plan.upstream_results:
            assert upstream.question_id in allowed_upstream
            assert set(upstream.outputs) <= output_ids[upstream.question_id]

    questions = outline_result.to_legacy_questions()
    questions["ques_count"] = 99
    flows = Flows(
        questions,
        schedule=outline_result.schedule,
    ).get_solution_flows(
        questions,
        ModelerToCoder(questions_solution=result.coder_handoff),
        result.data_contract,
    )
    flow_order = [phase for phase in flows if phase.startswith("ques")]
    positions = {question_id: index for index, question_id in enumerate(flow_order)}
    assert flow_order == ["ques1", "ques2", "ques3"]
    for question_id, dependencies in direct_dependencies.items():
        assert all(
            positions[dependency] < positions[question_id]
            for dependency in dependencies
        )

    assert {fact.path for fact in outline_result.task_facts.output_templates} == set(
        OUTPUT_TEMPLATES
    )
    assert {fact.path for fact in outline_result.task_facts.attachments} == set(
        INPUT_FILES
    )
    assert outline_result.task_facts.expected_question_ids == (
        "ques1",
        "ques2",
        "ques3",
    )
    assert outline_result.task_facts.expected_question_count == 3
    assert {path.name: _file_state(path) for path in fixture_paths} == fixture_before
    assert {
        path.name: _file_state(tmp_path / path.name) for path in fixture_paths
    } == copied_before
    assert (
        M15ArtifactStore(tmp_path).read_json("m15/task_facts.json", TaskFacts)
        == outline_result.task_facts
    )
