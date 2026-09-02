"""任务产物 manifest 的幂等、去重和部分失败测试。"""

import hashlib
import json
from pathlib import Path

from app.utils.task_manifest import (
    MANIFEST_FILENAME,
    build_task_manifest,
    load_task_manifest,
    update_task_manifest,
)
from app.utils.phase_results import (
    PHASE_RESULT_BUDGET,
    build_phase_result,
    save_phase_result,
)


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _write_phase_packet(
    work_dir: Path,
    phase: str,
    *,
    status: str,
    artifacts: list[str],
) -> None:
    _write_json(
        work_dir / "phase_results" / f"{phase}.json",
        {
            "version": 1,
            "task_id": "task-7",
            "phase": phase,
            "status": status,
            "evidence_status": "available" if status == "success" else "missing",
            "artifacts": [{"path": path, "kind": "file"} for path in artifacts],
        },
    )


class _ExtremePacketInterpreter:
    """返回足以触发 6000 字符 packet 截断的真实产物集合。"""

    def __init__(self, paths: list[str]):
        self.paths = paths

    def get_code_output(self, phase: str) -> str:
        return "accuracy=0.91\n结果：批量产物已生成\n"

    def get_section_artifacts(self, phase: str) -> list[str]:
        return self.paths


def test_manifest_indexes_inputs_outputs_and_hashes_without_moving_files(
    tmp_path: Path,
):
    source = tmp_path / "附件1.xlsx"
    source.write_bytes(b"source")
    (tmp_path / "result1.xlsx").write_bytes(b"template")
    (tmp_path / "cleaned").mkdir()
    (tmp_path / "cleaned" / "附件1__Sheet1.csv").write_text(
        "x\n1\n", encoding="utf-8"
    )
    (tmp_path / "ques1_plot.png").write_bytes(b"png")
    (tmp_path / "ques1_result.npy").write_bytes(b"npy")
    (tmp_path / "notebook.ipynb").write_text("{}", encoding="utf-8")
    _write_json(tmp_path / "source_inspection.json", {"version": 1})
    _write_json(tmp_path / "data_contract.json", {"version": 1})
    _write_phase_packet(
        tmp_path,
        "ques1",
        status="success",
        artifacts=["ques1_plot.png", "ques1_result.npy"],
    )
    (tmp_path / "res.md").write_text("# result", encoding="utf-8")
    (tmp_path / "res.json").write_text("{}", encoding="utf-8")
    (tmp_path / "res.docx").write_bytes(b"docx")

    manifest = build_task_manifest(
        tmp_path,
        task_id="task-7",
        status="completed",
        current_phase=None,
        completed_phases=["eda", "ques1"],
        generated_at="2026-08-30T00:00:00+00:00",
    )

    by_path = {item["path"]: item for item in manifest["artifacts"]}
    assert len(by_path) == len(manifest["artifacts"])
    assert manifest["data_status"] == "source_data"
    assert by_path["附件1.xlsx"]["kind"] == "source_data"
    assert by_path["result1.xlsx"]["kind"] == "result_template"
    assert by_path["result1.xlsx"]["status"] == "skipped"
    assert by_path["source_inspection.json"]["phase"] == "eda"
    assert by_path["data_contract.json"]["phase"] == "eda"
    assert by_path["cleaned/附件1__Sheet1.csv"]["kind"] == "cleaned_csv"
    assert by_path["phase_results/ques1.json"]["status"] == "success"
    assert by_path["ques1_plot.png"]["phase"] == "ques1"
    assert by_path["notebook.ipynb"]["kind"] == "notebook"
    assert by_path["res.docx"]["kind"] == "result_docx"
    assert by_path["附件1.xlsx"]["bytes"] == len(b"source")
    assert by_path["附件1.xlsx"]["sha256"] == hashlib.sha256(b"source").hexdigest()
    assert manifest["status"] == "completed"
    assert manifest["completed_phases"] == ["eda", "ques1"]

    assert not (tmp_path / "cleaned" / "附件1__Sheet1.csv").is_symlink()
    assert source.exists()


def test_manifest_is_idempotent_and_preserves_completed_phases(tmp_path: Path):
    (tmp_path / "source.csv").write_text("x\n1\n", encoding="utf-8")
    first = update_task_manifest(
        tmp_path,
        task_id="task-7",
        status="running",
        current_phase="eda",
        generated_at="2026-08-30T00:00:00+00:00",
    )
    second = update_task_manifest(
        tmp_path,
        task_id="task-7",
        status="running",
        current_phase="ques2",
        completed_phases=["eda", "ques1"],
        generated_at="2026-08-30T00:01:00+00:00",
    )
    third = update_task_manifest(
        tmp_path,
        task_id="task-7",
        status="running",
        current_phase="ques2",
        completed_phases=["ques1", "eda"],
        generated_at="2026-08-30T00:01:00+00:00",
    )

    assert first["created_at"] == "2026-08-30T00:00:00+00:00"
    assert second["completed_phases"] == ["eda", "ques1"]
    assert third["completed_phases"] == second["completed_phases"]
    assert [item["path"] for item in third["artifacts"]] == [
        item["path"] for item in second["artifacts"]
    ]
    assert load_task_manifest(tmp_path) == third
    assert (tmp_path / MANIFEST_FILENAME).is_file()


def test_manifest_keeps_prior_artifacts_and_marks_partial_failure(tmp_path: Path):
    (tmp_path / "source.csv").write_text("x\n1\n", encoding="utf-8")
    (tmp_path / "ques1_plot.png").write_bytes(b"q1")
    _write_phase_packet(
        tmp_path,
        "ques1",
        status="success",
        artifacts=["ques1_plot.png"],
    )
    update_task_manifest(
        tmp_path,
        task_id="task-7",
        status="running",
        current_phase="ques1",
        completed_phases=["eda", "ques1"],
        generated_at="2026-08-30T00:00:00+00:00",
    )

    _write_phase_packet(
        tmp_path,
        "ques2",
        status="failed",
        artifacts=[],
    )
    manifest = update_task_manifest(
        tmp_path,
        task_id="task-7",
        status="partial_failure",
        current_phase="ques2",
        failed_phase="ques2",
        failure_reason="coder status: failed",
        generated_at="2026-08-30T00:01:00+00:00",
    )

    by_path = {item["path"]: item for item in manifest["artifacts"]}
    assert manifest["status"] == "partial_failure"
    assert manifest["current_phase"] == "ques2"
    assert manifest["completed_phases"] == ["eda", "ques1"]
    assert manifest["failed_phase"] == "ques2"
    assert manifest["failure"]["reason"] == "coder status: failed"
    assert by_path["phase_results/ques1.json"]["exists"] is True
    assert by_path["phase_results/ques2.json"]["status"] == "failed"


def test_manifest_deduplicates_cross_phase_artifact_references(tmp_path: Path):
    (tmp_path / "shared.png").write_bytes(b"png")
    _write_phase_packet(
        tmp_path,
        "ques1",
        status="success",
        artifacts=["shared.png"],
    )
    _write_phase_packet(
        tmp_path,
        "ques2",
        status="success",
        artifacts=["shared.png"],
    )

    manifest = build_task_manifest(
        tmp_path,
        task_id="task-7",
        generated_at="2026-08-30T00:00:00+00:00",
    )

    paths = [item["path"] for item in manifest["artifacts"]]
    assert paths.count("shared.png") == 1
    shared = next(item for item in manifest["artifacts"] if item["path"] == "shared.png")
    assert shared["phase"] == "multiple"
    assert manifest["phase_conflicts"] == [
        {"path": "shared.png", "phases": ["ques1", "ques2"]}
    ]


def test_manifest_does_not_create_fake_inspection_for_only_templates(tmp_path: Path):
    (tmp_path / "result1.csv").write_text("answer\n", encoding="utf-8")

    manifest = build_task_manifest(
        tmp_path,
        task_id="task-7",
        generated_at="2026-08-30T00:00:00+00:00",
    )

    paths = {item["path"] for item in manifest["artifacts"]}
    assert manifest["data_status"] == "not_applicable"
    assert "result1.csv" in paths
    assert "source_inspection.json" not in paths
    assert "data_contract.json" not in paths
    assert {item["path"] for item in manifest["artifacts"] if not item["exists"]} == {
        "res.json",
        "res.md",
        "res.docx",
    }


def test_manifest_recovers_phase_after_extreme_packet_truncation(tmp_path: Path):
    paths = [f"results/artifact_{index:04d}.csv" for index in range(240)]
    for relative in paths:
        artifact = tmp_path / relative
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"artifact")

    packet = build_phase_result(
        tmp_path,
        task_id="task-7",
        phase="ques1",
        status="success",
        started_at="2026-08-30T00:00:00Z",
        ended_at="2026-08-30T00:01:00Z",
        interpreter=_ExtremePacketInterpreter(paths),
    )
    save_phase_result(tmp_path, packet)
    packet_text = (tmp_path / "phase_results" / "ques1.json").read_text(
        encoding="utf-8"
    )
    packet_paths = {item["path"] for item in packet["artifacts"]}
    omitted_paths = set(paths) - packet_paths

    assert packet["truncated"] is True
    assert omitted_paths
    assert len(packet_text) <= PHASE_RESULT_BUDGET
    assert "phase_artifacts" not in packet

    manifest = build_task_manifest(
        tmp_path,
        task_id="task-7",
        status="completed",
        completed_phases=["ques1"],
        phase_artifacts={"ques1": paths},
        generated_at="2026-08-30T00:00:00+00:00",
    )

    by_path = {item["path"]: item for item in manifest["artifacts"]}
    assert all(by_path[path]["phase"] == "ques1" for path in paths)
    assert all(path not in packet_text for path in omitted_paths)
    assert manifest["phase_conflicts"] == []

    update_task_manifest(
        tmp_path,
        task_id="task-7",
        status="completed",
        completed_phases=["ques1"],
        phase_artifacts={"ques1": paths},
        generated_at="2026-08-30T00:00:00+00:00",
    )
    refreshed = update_task_manifest(
        tmp_path,
        task_id="task-7",
        status="completed",
        completed_phases=["ques1"],
        generated_at="2026-08-30T00:00:00+00:00",
    )
    refreshed_by_path = {item["path"]: item for item in refreshed["artifacts"]}
    assert all(refreshed_by_path[path]["phase"] == "ques1" for path in paths)
