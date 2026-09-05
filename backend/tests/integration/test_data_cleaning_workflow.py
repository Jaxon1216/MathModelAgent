"""M1.5 表级 CleaningPlan Repair 闭环测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import app.orchestration.data_cleaning as data_cleaning_module
from app.agents.cleaning_planner import validate_repair_scope
from app.data import M15ArtifactStore, inspect_data_profiles
from app.data.cleaning import TableCleaningError, VerificationFailure
from app.domain.m15 import (
    CleaningOperation,
    CleaningPlan,
    DataIssue,
    Deliverable,
    OutlineQuestion,
    TaskOutline,
    ValidationExpectation,
)
from app.orchestration import DataCleaningBlockedError, DataCleaningWorkflow

OUTLINE_ID = "task-outline:repair-test"


class RecordingEventSink:
    """记录 DataCleaningWorkflow 子步骤事件。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event: str, **payload) -> None:
        """按发生顺序保存事件。"""
        self.events.append((event, payload))


def _assert_unique_step_terminals(sink: RecordingEventSink) -> None:
    """每个已开始子步骤必须按序且恰好结束一次。"""
    starts = [
        payload["step_id"] for event, payload in sink.events if event.endswith(".start")
    ]
    assert len(starts) == len(set(starts))
    for step_id in starts:
        events = [
            (event, payload)
            for event, payload in sink.events
            if payload["step_id"] == step_id
        ]
        assert events[0][0].endswith(".start")
        assert events[-1][0].endswith(".end")
        assert sum(event.endswith(".end") for event, _ in events) == 1
        assert all(payload["step_id"] == step_id for _, payload in events)


def _outline() -> TaskOutline:
    """构造单问题 TaskOutline。"""
    deliverable = Deliverable(
        deliverable_id="deliverable:report",
        description="提交报告",
    )
    return TaskOutline(
        schema_version="m1.5",
        artifact_id=OUTLINE_ID,
        source_artifact_ids=("task-facts:repair-test",),
        validation_status="validated",
        artifact_path="m15/task_outline.json",
        task_facts_id="task-facts:repair-test",
        questions=(
            OutlineQuestion(
                question_id="ques1",
                text="分析分组数据。",
                order_key=1,
                deliverables=(deliverable,),
            ),
        ),
        deliverables=(deliverable,),
    )


def _fill_operation(strategy: str) -> CleaningOperation:
    """构造要求键列无缺失的有限填充操作。"""
    return CleaningOperation.model_validate(
        {
            "operation_id": "clean-op:fill-group",
            "operation_type": "fill_missing",
            "target_columns": ("group",),
            "strategy": strategy,
            "reason": "恢复合并单元格造成的分组键缺失。",
            "postconditions": (
                ValidationExpectation(
                    rule_id="rule:group-non-null",
                    rule_type="missing_count",
                    columns=("group",),
                    operator="eq",
                    expected=0,
                ),
            ),
        }
    )


def _initial_plan(profile) -> CleaningPlan:
    """构造首次会验证失败的 forward-fill 计划。"""
    digest = profile.artifact_id.removeprefix("data-profile:")
    return CleaningPlan(
        schema_version="m1.5",
        artifact_id=f"cleaning-plan:{digest}",
        source_artifact_ids=(OUTLINE_ID, profile.artifact_id),
        validation_status="validated",
        artifact_path=f"m15/cleaning_plans/{digest}.json",
        task_outline_id=OUTLINE_ID,
        data_profile_id=profile.artifact_id,
        table_id=profile.table_id,
        operations=(_fill_operation("forward_fill"),),
    )


def _repair_plan(
    profile,
    current_plan: CleaningPlan,
    issues: tuple[DataIssue, ...],
    attempt: int,
    *,
    strategy: str,
) -> CleaningPlan:
    """构造只改失败规则对应操作的 repair 计划。"""
    digest = profile.artifact_id.removeprefix("data-profile:")
    return CleaningPlan(
        schema_version="m1.5",
        artifact_id=f"cleaning-plan:{digest}-repair{attempt}",
        source_artifact_ids=(
            OUTLINE_ID,
            profile.artifact_id,
            current_plan.artifact_id,
            *(issue.issue_id for issue in issues),
        ),
        validation_status="validated",
        artifact_path=f"m15/cleaning_plans/{digest}.repair{attempt}.json",
        task_outline_id=OUTLINE_ID,
        data_profile_id=profile.artifact_id,
        table_id=profile.table_id,
        operations=(_fill_operation(strategy),),
        repair_attempt=attempt,
        repaired_issue_ids=tuple(issue.issue_id for issue in issues),
    )


class RepairPlanner:
    """按测试策略返回一次或持续无效的数据修复。"""

    def __init__(self, initial: CleaningPlan, *, succeeds: bool) -> None:
        self.initial = initial
        self.succeeds = succeeds
        self.repairs: list[tuple[str, tuple[str, ...], int]] = []

    async def plan(self, outline, profiles):
        """返回唯一初始计划。"""
        assert outline.artifact_id == OUTLINE_ID
        assert [profile.table_id for profile in profiles] == [self.initial.table_id]
        return (self.initial,)

    async def repair(
        self,
        profile,
        current_plan,
        issues,
        *,
        repair_attempt,
    ):
        """只记录失败表和规则，并返回限定范围内的新计划。"""
        self.repairs.append(
            (
                profile.table_id,
                tuple(issue.rule_id for issue in issues),
                repair_attempt,
            )
        )
        strategy = (
            "backward_fill" if self.succeeds and repair_attempt == 1 else "forward_fill"
        )
        return _repair_plan(
            profile,
            current_plan,
            issues,
            repair_attempt,
            strategy=strategy,
        )


class CancellingRepairPlanner(RepairPlanner):
    """在已有失败证据落盘后模拟用户取消。"""

    async def repair(
        self,
        profile,
        current_plan,
        issues,
        *,
        repair_attempt,
    ):
        """保持 CancelledError 语义，不返回伪造失败。"""
        raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_successful_clean_and_verify_emit_stable_ordered_terminals(
    tmp_path: Path,
):
    """无 Repair 成功路径为 clean/verify 各记录 artifact 和唯一成功终态。"""
    (tmp_path / "input.csv").write_text(
        "group,value\nA,1\nB,2\n",
        encoding="utf-8",
    )
    (profile,) = inspect_data_profiles(tmp_path, OUTLINE_ID)
    sink = RecordingEventSink()

    await DataCleaningWorkflow(
        RepairPlanner(_initial_plan(profile), succeeds=True),
        event_sink=sink,
    ).run(
        work_dir=tmp_path,
        outline=_outline(),
        profiles=(profile,),
    )

    assert [event for event, _ in sink.events] == [
        "data_cleaning.clean.start",
        "data_cleaning.clean.artifact",
        "data_cleaning.clean.end",
        "data_cleaning.verify.start",
        "data_cleaning.verify.artifact",
        "data_cleaning.verify.end",
    ]
    _assert_unique_step_terminals(sink)
    starts = {
        event: payload for event, payload in sink.events if event.endswith(".start")
    }
    assert starts["data_cleaning.clean.start"]["attempt"] == 0
    assert starts["data_cleaning.verify.start"]["verify_round"] == 1
    for event, payload in sink.events:
        if event.endswith(".end"):
            assert payload["status"] == "success"
            assert payload["success"] is True
            assert payload["failure_kind"] is None
            assert payload["attempt"] == 0


@pytest.mark.asyncio
async def test_clean_failure_emits_issue_before_unique_failure_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """不可修复 clean 失败保留 issue，且不会伪造 verify 生命周期。"""
    (tmp_path / "input.csv").write_text(
        "group,value\nA,1\n",
        encoding="utf-8",
    )
    (profile,) = inspect_data_profiles(tmp_path, OUTLINE_ID)
    sink = RecordingEventSink()
    failure = VerificationFailure(
        table_id=profile.table_id,
        rule_id="clean:source-readable",
        expected="readable",
        actual="OSError",
        evidence_summary="无法读取源表。",
        repairable=False,
    )

    def fail_clean(*args, **kwargs):
        raise TableCleaningError(failure)

    monkeypatch.setattr(
        data_cleaning_module,
        "execute_cleaning_plan",
        fail_clean,
    )

    with pytest.raises(DataCleaningBlockedError) as exc_info:
        await DataCleaningWorkflow(
            RepairPlanner(_initial_plan(profile), succeeds=True),
            event_sink=sink,
        ).run(
            work_dir=tmp_path,
            outline=_outline(),
            profiles=(profile,),
        )

    assert exc_info.value.failure_kind == "clean"
    assert [event for event, _ in sink.events] == [
        "data_cleaning.clean.start",
        "data_cleaning.clean.issue",
        "data_cleaning.clean.end",
    ]
    _assert_unique_step_terminals(sink)
    _, issue = sink.events[1]
    _, terminal = sink.events[2]
    assert issue["issue_id"] == exc_info.value.issues[0].issue_id
    assert terminal["status"] == "failure"
    assert terminal["success"] is False
    assert terminal["failure_kind"] == "clean"
    assert terminal["attempt"] == 0


@pytest.mark.asyncio
async def test_single_table_repair_succeeds_and_preserves_issue_evidence(
    tmp_path: Path,
):
    """首次验证失败后只修复该表，重跑全部验证并保留 resolved 证据。"""
    (tmp_path / "input.csv").write_text(
        "group,value\n,1\nA,2\n",
        encoding="utf-8",
    )
    (profile,) = inspect_data_profiles(tmp_path, OUTLINE_ID)
    planner = RepairPlanner(_initial_plan(profile), succeeds=True)
    sink = RecordingEventSink()

    result = await DataCleaningWorkflow(planner, event_sink=sink).run(
        work_dir=tmp_path,
        outline=_outline(),
        profiles=(profile,),
    )

    assert planner.repairs == [(profile.table_id, ("rule:group-non-null",), 1)]
    assert result.plans[0].repair_attempt == 1
    assert [issue.status for issue in result.issues] == [
        "unresolved",
        "resolved",
    ]
    assert result.issues[0].rule_id == result.issues[1].rule_id
    for issue in result.issues:
        assert (
            M15ArtifactStore(tmp_path).read_json(
                issue.artifact_path,
                DataIssue,
            )
            == issue
        )

    assert [event for event, _ in sink.events] == [
        "data_cleaning.clean.start",
        "data_cleaning.clean.artifact",
        "data_cleaning.clean.end",
        "data_cleaning.verify.start",
        "data_cleaning.verify.issue",
        "data_cleaning.verify.end",
        "data_cleaning.repair.start",
        "data_cleaning.repair.issue",
        "data_cleaning.repair.artifact",
        "data_cleaning.repair.end",
        "data_cleaning.clean.start",
        "data_cleaning.clean.artifact",
        "data_cleaning.clean.end",
        "data_cleaning.verify.start",
        "data_cleaning.verify.artifact",
        "data_cleaning.verify.end",
    ]
    _assert_unique_step_terminals(sink)
    terminals = [payload for event, payload in sink.events if event.endswith(".end")]
    assert [terminal["status"] for terminal in terminals] == [
        "success",
        "failure",
        "success",
        "success",
        "success",
    ]
    assert [terminal["attempt"] for terminal in terminals] == [0, 0, 1, 1, 1]
    repair_events = [
        payload
        for event, payload in sink.events
        if event.startswith("data_cleaning.repair.")
    ]
    assert len({event["step_id"] for event in repair_events}) == 1
    assert {event["attempt"] for event in repair_events} == {1}
    repair_issue = next(
        payload
        for event, payload in sink.events
        if event == "data_cleaning.repair.issue"
    )
    assert repair_issue["issue_attempt"] == 0


@pytest.mark.asyncio
async def test_two_failed_repairs_persist_exhausted_issue_and_block(
    tmp_path: Path,
):
    """同一表两次 Repair 后仍失败时必须 exhausted 并阻断。"""
    (tmp_path / "input.csv").write_text(
        "group,value\n,1\nA,2\n",
        encoding="utf-8",
    )
    (profile,) = inspect_data_profiles(tmp_path, OUTLINE_ID)
    planner = RepairPlanner(_initial_plan(profile), succeeds=False)
    sink = RecordingEventSink()

    with pytest.raises(DataCleaningBlockedError) as exc_info:
        await DataCleaningWorkflow(planner, event_sink=sink).run(
            work_dir=tmp_path,
            outline=_outline(),
            profiles=(profile,),
        )

    assert exc_info.value.failure_kind == "repair"
    assert [repair[2] for repair in planner.repairs] == [1, 2]
    terminal = exc_info.value.issues[-1]
    assert terminal.status == "exhausted"
    assert terminal.repair_attempt == 2
    assert terminal.rule_id == "rule:group-non-null"
    assert (
        M15ArtifactStore(tmp_path).read_json(
            terminal.artifact_path,
            DataIssue,
        )
        == terminal
    )
    _assert_unique_step_terminals(sink)
    terminals = [payload for event, payload in sink.events if event.endswith(".end")]
    assert [terminal["status"] for terminal in terminals] == [
        "success",
        "failure",
        "success",
        "success",
        "failure",
        "success",
        "success",
        "failure",
    ]
    assert [terminal["attempt"] for terminal in terminals] == [
        0,
        0,
        1,
        1,
        1,
        2,
        2,
        2,
    ]
    assert sink.events[-1][0] == "data_cleaning.verify.end"
    assert sink.events[-1][1]["failure_kind"] == "verify"


@pytest.mark.asyncio
async def test_cancellation_preserves_issue_and_removes_unverified_cleaned_file(
    tmp_path: Path,
):
    """Repair 等待期间取消时保留 issue，且不遗留未验证 cleaned 或临时文件。"""
    (tmp_path / "input.csv").write_text(
        "group,value\n,1\nA,2\n",
        encoding="utf-8",
    )
    (profile,) = inspect_data_profiles(tmp_path, OUTLINE_ID)
    planner = CancellingRepairPlanner(_initial_plan(profile), succeeds=False)
    sink = RecordingEventSink()

    with pytest.raises(asyncio.CancelledError):
        await DataCleaningWorkflow(planner, event_sink=sink).run(
            work_dir=tmp_path,
            outline=_outline(),
            profiles=(profile,),
        )

    issue_files = list((tmp_path / "m15" / "issues").glob("*.json"))
    assert len(issue_files) == 1
    assert (
        M15ArtifactStore(tmp_path)
        .read_json(
            issue_files[0].relative_to(tmp_path).as_posix(),
            DataIssue,
        )
        .status
        == "unresolved"
    )
    assert not list((tmp_path / "cleaned").glob("*.csv"))
    assert not list(tmp_path.rglob("*.tmp"))
    _assert_unique_step_terminals(sink)
    assert [event for event, _ in sink.events][-3:] == [
        "data_cleaning.repair.start",
        "data_cleaning.repair.issue",
        "data_cleaning.repair.end",
    ]
    terminal = sink.events[-1][1]
    assert terminal["status"] == "cancelled"
    assert terminal["success"] is False
    assert terminal["failure_kind"] == "cancelled"
    assert terminal["attempt"] == 1


@pytest.mark.asyncio
async def test_mixed_verifier_failures_persist_and_return_every_issue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """不可修复失败阻断前也必须保留同批次的可修复失败。"""
    (tmp_path / "input.csv").write_text(
        "group,value\nA,1\nB,2\n",
        encoding="utf-8",
    )
    (profile,) = inspect_data_profiles(tmp_path, OUTLINE_ID)
    planner = RepairPlanner(_initial_plan(profile), succeeds=True)
    failures = (
        VerificationFailure(
            table_id=profile.table_id,
            rule_id="rule:repairable",
            expected=0,
            actual=1,
            evidence_summary="可通过局部计划修复。",
        ),
        VerificationFailure(
            table_id=profile.table_id,
            rule_id="verify:cleaned-sha256",
            expected="expected",
            actual="replaced",
            evidence_summary="cleaned 文件指纹漂移。",
            repairable=False,
        ),
    )
    monkeypatch.setattr(
        data_cleaning_module,
        "verify_cleaned_table",
        lambda *args, **kwargs: failures,
    )

    with pytest.raises(DataCleaningBlockedError) as exc_info:
        await DataCleaningWorkflow(planner).run(
            work_dir=tmp_path,
            outline=_outline(),
            profiles=(profile,),
        )

    assert planner.repairs == []
    assert [issue.rule_id for issue in exc_info.value.issues] == [
        "rule:repairable",
        "verify:cleaned-sha256",
    ]
    store = M15ArtifactStore(tmp_path)
    assert all(
        store.read_json(issue.artifact_path, DataIssue) == issue
        for issue in exc_info.value.issues
    )


def test_repair_scope_rejects_foreign_issue(
    tmp_path: Path,
):
    """Repair 不能消费非当前 plan 产生的 issue。"""
    (tmp_path / "input.csv").write_text(
        "group,value\n,1\nA,2\n",
        encoding="utf-8",
    )
    (profile,) = inspect_data_profiles(tmp_path, OUTLINE_ID)
    current = _initial_plan(profile)
    issue = DataIssue(
        schema_version="m1.5",
        artifact_id="data-issue:foreign",
        source_artifact_ids=(profile.artifact_id,),
        validation_status="validated",
        artifact_path="m15/issues/foreign.json",
        issue_id="data-issue:foreign",
        table_id=profile.table_id,
        rule_id="rule:group-non-null",
        expected=0,
        actual=1,
        evidence_summary="该 issue 未引用当前计划。",
        repair_attempt=0,
        status="unresolved",
    )
    repaired = current.model_copy(
        update={
            "artifact_id": "cleaning-plan:invalid-repair",
            "artifact_path": "m15/cleaning_plans/invalid-repair.json",
            "source_artifact_ids": (
                OUTLINE_ID,
                profile.artifact_id,
                current.artifact_id,
                issue.issue_id,
            ),
            "repair_attempt": 1,
            "repaired_issue_ids": (issue.issue_id,),
        }
    )

    with pytest.raises(ValueError, match="当前 CleaningPlan"):
        validate_repair_scope(current, repaired, (issue,))


def test_repair_scope_rejects_non_incrementing_attempt(tmp_path: Path):
    """Repair plan 必须严格递增 attempt，防止绕过两次上限。"""
    (tmp_path / "input.csv").write_text(
        "group,value\n,1\nA,2\n",
        encoding="utf-8",
    )
    (profile,) = inspect_data_profiles(tmp_path, OUTLINE_ID)
    current = _initial_plan(profile)
    issue = DataIssue(
        schema_version="m1.5",
        artifact_id="data-issue:current",
        source_artifact_ids=(profile.artifact_id, current.artifact_id),
        validation_status="validated",
        artifact_path="m15/issues/current.json",
        issue_id="data-issue:current",
        table_id=profile.table_id,
        rule_id="rule:group-non-null",
        expected=0,
        actual=1,
        evidence_summary="当前计划执行后仍有缺失。",
        repair_attempt=0,
        status="unresolved",
    )
    repaired = current.model_copy(
        update={
            "artifact_id": "cleaning-plan:invalid-attempt",
            "artifact_path": "m15/cleaning_plans/invalid-attempt.json",
            "source_artifact_ids": (
                OUTLINE_ID,
                profile.artifact_id,
                current.artifact_id,
                issue.issue_id,
            ),
            "repair_attempt": 0,
            "repaired_issue_ids": (issue.issue_id,),
        }
    )

    with pytest.raises(ValueError, match="严格递增"):
        validate_repair_scope(current, repaired, (issue,))


def test_repair_scope_rejects_reinterpreted_failed_rule(tmp_path: Path):
    """Repair 不能保留 rule_id 却替换规则类型、目标或期望。"""
    (tmp_path / "input.csv").write_text(
        "group,value\n,1\nA,2\n",
        encoding="utf-8",
    )
    (profile,) = inspect_data_profiles(tmp_path, OUTLINE_ID)
    current = _initial_plan(profile)
    issue = DataIssue(
        schema_version="m1.5",
        artifact_id="data-issue:current-rule",
        source_artifact_ids=(profile.artifact_id, current.artifact_id),
        validation_status="validated",
        artifact_path="m15/issues/current-rule.json",
        issue_id="data-issue:current-rule",
        table_id=profile.table_id,
        rule_id="rule:group-non-null",
        expected=0,
        actual=1,
        evidence_summary="group 仍有缺失。",
        repair_attempt=0,
        status="unresolved",
    )
    changed_operation = CleaningOperation(
        operation_id="clean-op:fill-group",
        operation_type="fill_missing",
        target_columns=("group",),
        strategy="backward_fill",
        reason="尝试通过换义规则绕过失败。",
        postconditions=(
            ValidationExpectation(
                rule_id="rule:group-non-null",
                rule_type="key_non_null",
                columns=("group",),
                operator="eq",
                expected=True,
            ),
        ),
    )
    repaired = _repair_plan(
        profile,
        current,
        (issue,),
        1,
        strategy="backward_fill",
    ).model_copy(update={"operations": (changed_operation,)})

    with pytest.raises(ValueError, match="规则定义"):
        validate_repair_scope(current, repaired, (issue,))
