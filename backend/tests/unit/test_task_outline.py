"""TaskOutline 领域校验与稳定依赖调度测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.m15 import Deliverable, OutlineQuestion, TaskOutline
from app.orchestration.task_outline import build_task_schedule


def _deliverable() -> Deliverable:
    """构造所有问题共享的文字交付物。"""
    return Deliverable(
        deliverable_id="deliverable:report",
        description="提交建模结论",
    )


def _question(
    question_id: str,
    *,
    order_key: int,
    depends_on: tuple[str, ...] = (),
) -> OutlineQuestion:
    """构造一个问题节点。"""
    return OutlineQuestion.model_validate(
        {
            "question_id": question_id,
            "text": f"{question_id} 原文",
            "depends_on": depends_on,
            "order_key": order_key,
            "deliverables": (_deliverable(),),
        }
    )


def _outline(questions: tuple[OutlineQuestion, ...]) -> TaskOutline:
    """构造指定依赖图的 TaskOutline。"""
    return TaskOutline(
        schema_version="m1.5",
        artifact_id="task-outline:root",
        source_artifact_ids=("task-facts:root",),
        validation_status="validated",
        artifact_path="m15/task_outline.json",
        task_facts_id="task-facts:root",
        questions=questions,
        deliverables=(_deliverable(),),
    )


def test_outline_rejects_missing_question_id():
    """问题集合必须严格连续覆盖 ques1..quesN。"""
    with pytest.raises(ValidationError, match="严格覆盖 ques1..quesN"):
        _outline(
            (
                _question("ques1", order_key=1),
                _question("ques3", order_key=2),
            )
        )


def test_outline_rejects_duplicate_question_id():
    """重复问题节点不能被顺序键掩盖。"""
    with pytest.raises(ValidationError, match="问题标识不能重复"):
        _outline(
            (
                _question("ques1", order_key=1),
                _question("ques1", order_key=2),
            )
        )


def test_outline_rejects_unknown_and_self_dependencies():
    """直接依赖只能引用其他已声明问题。"""
    with pytest.raises(ValidationError, match="未知依赖"):
        _outline((_question("ques1", order_key=1, depends_on=("ques2",)),))

    with pytest.raises(ValidationError, match="不能依赖自身"):
        _question("ques1", order_key=1, depends_on=("ques1",))


def test_outline_rejects_duplicate_dependency_and_cycle():
    """重复边与任意长度的依赖环均应失败。"""
    with pytest.raises(ValidationError, match="直接依赖不能重复"):
        _question("ques2", order_key=2, depends_on=("ques1", "ques1"))

    with pytest.raises(ValidationError, match="不能形成环"):
        _outline(
            (
                _question("ques1", order_key=1, depends_on=("ques2",)),
                _question("ques2", order_key=2, depends_on=("ques1",)),
            )
        )


def test_stable_topological_layers_preserve_serial_compatibility_order():
    """节点输入顺序不影响同层排序，依赖节点始终进入后续层。"""
    outline = _outline(
        (
            _question("ques2", order_key=2),
            _question("ques1", order_key=1),
            _question("ques3", order_key=3, depends_on=("ques2",)),
        )
    )

    schedule = build_task_schedule(outline)

    assert [layer.question_ids for layer in schedule.layers] == [
        ("ques1", "ques2"),
        ("ques3",),
    ]
    assert [layer.parallel_candidate for layer in schedule.layers] == [True, False]
    assert schedule.execution_order == ("ques1", "ques2", "ques3")
    assert schedule.ques_count == outline.ques_count == 3


def test_independent_questions_share_one_parallel_candidate_layer():
    """无依赖问题处于同一候选层，但仍有确定性的串行展开顺序。"""
    outline = _outline(
        (
            _question("ques2", order_key=2),
            _question("ques1", order_key=1),
        )
    )

    schedule = build_task_schedule(outline)

    assert len(schedule.layers) == 1
    assert schedule.layers[0].parallel_candidate is True
    assert schedule.execution_order == ("ques1", "ques2")
