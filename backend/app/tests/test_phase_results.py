"""阶段结果包和 Writer bounded material 的定向测试。"""

import json
from pathlib import Path

from app.utils.phase_results import (
    DATA_INVENTORY_BUDGET,
    IMAGE_LIST_BUDGET,
    PHASE_RESULT_BUDGET,
    WRITER_PROMPT_BUDGET,
    bound_data_inventory,
    bound_writer_user_prompt,
    build_phase_result,
    load_phase_result,
    render_image_list_for_writer,
    render_phase_result_for_writer,
    save_phase_result,
    validate_phase_result,
)


class FakeInterpreter:
    """提供 packet builder 所需的最小解释器接口。"""

    def get_code_output(self, phase: str) -> str:
        assert phase == "ques1"
        return (
            "accuracy=0.91\n"
            "结果：研究对象不要使用方法 A，具有周期性变化；"
            "必须使用方法 B，目标为最小化\n"
        )

    def get_section_artifacts(self, phase: str) -> list[str]:
        assert phase == "ques1"
        return ["ques1_result.csv", "ques1_plot.png", "missing.npy"]


class EchoInterpreter:
    """返回包含原始题面和未知过程 marker 的 Coder 输出。"""

    def get_code_output(self, phase: str) -> str:
        assert phase == "ques1"
        return (
            "原始题面：研究三种作物并最小化成本。"
            "禁止采用方法 A。请将结果保存为 result.csv。"
            "accuracy=0.91"
        )

    def get_section_artifacts(self, phase: str) -> list[str]:
        assert phase == "ques1"
        return []


class PublicContextEchoInterpreter:
    """返回题面回声、短片段和合法短结果。"""

    def get_code_output(self, phase: str) -> str:
        assert phase == "ques1"
        return (
            "研究三种作物的种植安排并最小化总成本与资源浪费同时满足土地和水资源约束。"
            "研究三种作物。"
            "accuracy=0.91。"
            "研究三种作物的种植安排并最小化总成本与资源浪费同时满足土地和水资源约束结果=0.91"
        )

    def get_section_artifacts(self, phase: str) -> list[str]:
        assert phase == "ques1"
        return []


class ManyArtifactsInterpreter:
    """返回大量真实产物路径，覆盖顺序扰动和重复引用。"""

    def __init__(self, paths: list[str]):
        self.paths = paths

    def get_code_output(self, phase: str) -> str:
        assert phase == "ques1"
        return "accuracy=0.91\n结果：批量产物已生成\n"

    def get_section_artifacts(self, phase: str) -> list[str]:
        assert phase == "ques1"
        return self.paths


def _write_many_artifacts(tmp_path: Path, count: int = 240) -> list[str]:
    paths: list[str] = []
    for index in range(count):
        suffix = ".png" if index % 2 else ".csv"
        relative = f"results/artifact_{count - index:04d}{suffix}"
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"artifact")
        paths.append(relative)
    return paths


def _write_csv_heavy_artifacts(
    tmp_path: Path,
    *,
    csv_count: int = 180,
    image_count: int = 10,
) -> tuple[list[str], list[str]]:
    csv_paths = [f"results/000_table_{index:04d}.csv" for index in range(csv_count)]
    image_paths = [
        f"results/999_plot_{index:04d}.png" for index in range(image_count)
    ]
    for relative in [*csv_paths, *image_paths]:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"artifact")
    return csv_paths, image_paths


def _raw_packet(paths: list[str]) -> dict:
    artifacts = [
        {
            "path": path,
            "kind": "image" if path.endswith(".png") else "csv",
            "exists": True,
        }
        for path in paths
    ]
    return {
        "version": 1,
        "task_id": "task",
        "phase": "ques1",
        "status": "success",
        "evidence_status": "available",
        "started_at": "2026-08-30T00:00:00Z",
        "ended_at": "2026-08-30T00:01:00Z",
        "model_reference": "modeler_solution:ques1",
        "error_summary": "unavailable",
        "summary": "结果摘要",
        "stdout": "accuracy=0.91\n" * 5000,
        "metrics": ["accuracy=0.91"],
        "limitations": ["execution_error: partial output retained"],
        "artifacts": artifacts,
        "images": [path for path in paths if path.endswith(".png")],
    }


def test_phase_result_keeps_real_artifacts_and_redacts_constraints(tmp_path: Path):
    (tmp_path / "ques1_result.csv").write_text("x\n1\n", encoding="utf-8")
    (tmp_path / "ques1_plot.png").write_bytes(b"png")

    packet = build_phase_result(
        tmp_path,
        task_id="task",
        phase="ques1",
        status="success",
        started_at="2026-08-30T00:00:00Z",
        ended_at="2026-08-30T00:01:00Z",
        interpreter=FakeInterpreter(),
        coder_summary="最终采用方法 B，accuracy=0.91",
        constraints=["不要使用方法 A", "必须使用方法 B"],
    )

    assert packet["evidence_status"] == "available"
    assert {item["path"] for item in packet["artifacts"]} == {
        "ques1_result.csv",
        "ques1_plot.png",
    }
    assert "不要使用方法 A" not in json.dumps(packet, ensure_ascii=False)
    assert "必须使用方法 B" not in json.dumps(packet, ensure_ascii=False)
    assert "具有周期性变化" in json.dumps(packet, ensure_ascii=False)
    assert "目标为最小化" in json.dumps(packet, ensure_ascii=False)
    assert len(json.dumps(packet, ensure_ascii=False)) <= PHASE_RESULT_BUDGET

    writer_material = render_phase_result_for_writer(
        packet,
        constraints=["不要使用方法 A", "必须使用方法 B"],
    )
    assert "不要使用方法 A" not in writer_material
    assert "必须使用方法 B" not in writer_material
    assert "具有周期性变化" in writer_material
    assert "目标为最小化" in writer_material

    save_phase_result(tmp_path, packet)
    loaded = load_phase_result(tmp_path, "ques1")
    assert loaded is not None
    assert validate_phase_result(tmp_path, loaded) == []


def test_phase_result_filters_unknown_coder_problem_and_process_echoes(
    tmp_path: Path,
):
    packet = build_phase_result(
        tmp_path,
        task_id="task",
        phase="ques1",
        status="success",
        started_at="2026-08-30T00:00:00Z",
        ended_at="2026-08-30T00:01:00Z",
        interpreter=EchoInterpreter(),
        coder_summary=(
            "原始题面：研究三种作物并最小化成本。"
            "按题目要求改用方法 B。结果准确率为0.91。"
        ),
    )

    serialized = json.dumps(packet, ensure_ascii=False)
    material = render_phase_result_for_writer(packet)

    assert "原始题面" not in serialized
    assert "禁止采用方法 A" not in serialized
    assert "请将结果保存为 result.csv" not in serialized
    assert "按题目要求改用方法 B" not in serialized
    assert "accuracy=0.91" in serialized
    assert "原始题面" not in material
    assert "禁止采用方法 A" not in material
    assert "请将结果保存为 result.csv" not in material
    assert "accuracy=0.91" in material


def test_phase_result_filters_exact_source_inspection_material_and_keeps_cleaned_facts(
    tmp_path: Path,
):
    inspection_body = (
        "inspection-entry: source.csv / Sheet1 rows=10 cols=2 "
        "cleaned_path=cleaned/source__Sheet1.csv"
    )
    packet = build_phase_result(
        tmp_path,
        task_id="task",
        phase="ques1",
        status="success",
        started_at="2026-08-30T00:00:00Z",
        ended_at="2026-08-30T00:01:00Z",
        interpreter=EchoInterpreter(),
        coder_summary=(
            f"{inspection_body} source_inspection.json\n"
            "cleaned/source__Sheet1.csv rows=10 accuracy=0.91 结论：清洗完成"
        ),
        inspection_text=inspection_body,
    )

    serialized = json.dumps(packet, ensure_ascii=False)
    material = render_phase_result_for_writer(packet)

    assert inspection_body not in serialized
    assert inspection_body not in material
    assert "source_inspection.json" not in serialized
    assert "source_inspection.json" not in material
    assert "cleaned/source__Sheet1.csv" in serialized
    assert "accuracy=0.91" in serialized
    assert "结论：清洗完成" in serialized


def test_phase_result_filters_definite_public_echo_and_keeps_short_or_ambiguous_results(
    tmp_path: Path,
):
    packet = build_phase_result(
        tmp_path,
        task_id="task",
        phase="ques1",
        status="success",
        started_at="2026-08-30T00:00:00Z",
        ended_at="2026-08-30T00:01:00Z",
        interpreter=PublicContextEchoInterpreter(),
        public_context=(
            "ques1: 研究三种作物的种植安排并最小化总成本与资源浪费"
            "同时满足土地和水资源约束"
        ),
    )

    serialized = json.dumps(packet, ensure_ascii=False)

    assert (
        "研究三种作物的种植安排并最小化总成本与资源浪费同时满足土地和水资源约束。"
        not in serialized
    )
    assert "研究三种作物。" in serialized
    assert "accuracy=0.91" in serialized
    assert (
        "研究三种作物的种植安排并最小化总成本与资源浪费同时满足土地和水资源约束结果=0.91"
        in serialized
    )
    assert "ques1:" not in serialized


class RawProblemEchoInterpreter:
    """返回完整 raw problem、短重合和合法结果。"""

    def __init__(self, raw_problem: str):
        self.raw_problem = raw_problem

    def get_code_output(self, phase: str) -> str:
        assert phase == "ques1"
        short_overlap = self.raw_problem[:31]
        bounded_overlap = self.raw_problem[:32]
        return (
            f"{self.raw_problem}。accuracy=0.91。"
            f"{bounded_overlap}。"
            f"{short_overlap}。"
            "目标函数值=12.5，模型验证通过。"
        )

    def get_section_artifacts(self, phase: str) -> list[str]:
        assert phase == "ques1"
        return []


def test_phase_result_uses_raw_problem_only_for_comparison_and_keeps_results(
    tmp_path: Path,
):
    raw_problem = (
        "研究三种作物的种植安排并最小化总成本与资源浪费同时满足土地和水资源约束"
    )
    packet = build_phase_result(
        tmp_path,
        task_id="task",
        phase="ques1",
        status="success",
        started_at="2026-08-30T00:00:00Z",
        ended_at="2026-08-30T00:01:00Z",
        interpreter=RawProblemEchoInterpreter(raw_problem),
        raw_problem=raw_problem,
    )

    serialized = json.dumps(packet, ensure_ascii=False)
    material = render_phase_result_for_writer(packet, raw_problem=raw_problem)

    assert raw_problem not in serialized
    assert raw_problem not in material
    assert "accuracy=0.91" in serialized
    assert "accuracy=0.91" in material
    assert raw_problem[:32] not in serialized
    assert raw_problem[:31] in serialized
    assert "目标函数值=12.5" in serialized


def test_many_real_artifacts_are_sorted_and_bounded(tmp_path: Path):
    paths = _write_many_artifacts(tmp_path)
    shuffled = list(reversed(paths)) + [paths[0], "results/missing.png"]

    packet = build_phase_result(
        tmp_path,
        task_id="task",
        phase="ques1",
        status="success",
        started_at="2026-08-30T00:00:00Z",
        ended_at="2026-08-30T00:01:00Z",
        interpreter=ManyArtifactsInterpreter(shuffled),
        error="partial output retained",
    )
    repeat = build_phase_result(
        tmp_path,
        task_id="task",
        phase="ques1",
        status="success",
        started_at="2026-08-30T00:00:00Z",
        ended_at="2026-08-30T00:01:00Z",
        interpreter=ManyArtifactsInterpreter(paths),
        error="partial output retained",
    )

    serialized = json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
    artifact_paths = [item["path"] for item in packet["artifacts"]]
    assert len(serialized) <= PHASE_RESULT_BUDGET
    assert packet["status"] == "success"
    assert packet["phase"] == "ques1"
    assert packet["evidence_status"] == "available"
    assert packet["limitations"] == ["execution_error: partial output retained"]
    assert packet["truncated"] is True
    assert packet["truncation"]["omitted_artifact_count"] > 0
    expected_paths = sorted(
        paths,
        key=lambda path: (not path.endswith((".png", ".jpg", ".jpeg")), path),
    )
    assert artifact_paths == expected_paths[: len(artifact_paths)]
    assert artifact_paths == [item["path"] for item in repeat["artifacts"]]
    assert "results/missing.png" not in artifact_paths
    assert all((tmp_path / path).is_file() for path in artifact_paths)
    assert all(
        path in artifact_paths and (tmp_path / path).is_file()
        for path in packet["images"]
    )


def test_extreme_packet_keeps_metadata_and_prioritizes_images(tmp_path: Path):
    csv_paths, image_paths = _write_csv_heavy_artifacts(tmp_path)
    packet = build_phase_result(
        tmp_path,
        task_id="task-10-2",
        phase="ques1",
        status="success",
        started_at="2026-08-30T00:00:00Z",
        ended_at="2026-08-30T00:01:00Z",
        interpreter=ManyArtifactsInterpreter([*csv_paths, *image_paths]),
        coder_summary="accuracy=0.91\n" + ("结果：" + "x" * 4000),
        model_reference="modeler_solution:ques1:v10",
    )

    serialized = json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
    assert len(serialized) <= PHASE_RESULT_BUDGET
    assert packet["version"] == 1
    assert packet["task_id"] == "task-10-2"
    assert packet["phase"] == "ques1"
    assert packet["status"] == "success"
    assert packet["evidence_status"] == "available"
    assert packet["started_at"] == "2026-08-30T00:00:00Z"
    assert packet["ended_at"] == "2026-08-30T00:01:00Z"
    assert packet["model_reference"] == "modeler_solution:ques1:v10"

    artifact_paths = [item["path"] for item in packet["artifacts"]]
    assert artifact_paths[: len(image_paths)] == sorted(image_paths)
    assert set(image_paths).issubset(artifact_paths)
    assert artifact_paths[len(image_paths) :] == sorted(
        path for path in artifact_paths if path not in image_paths
    )
    assert packet["images"] == sorted(image_paths)
    assert packet["truncation"]["omitted_artifact_count"] > 0
    assert packet["truncation"]["omitted_image_count"] == 0


def test_save_phase_result_caps_custom_budget_and_file_size(tmp_path: Path):
    paths = _write_many_artifacts(tmp_path, count=180)
    packet = _raw_packet(paths)

    save_phase_result(tmp_path, packet)
    default_serialized = (tmp_path / "phase_results" / "ques1.json").read_text(
        encoding="utf-8"
    )
    assert len(default_serialized) <= PHASE_RESULT_BUDGET

    packet["phase"] = "ques2"
    save_phase_result(tmp_path, packet, budget=PHASE_RESULT_BUDGET * 2)
    capped_serialized = (tmp_path / "phase_results" / "ques2.json").read_text(
        encoding="utf-8"
    )
    capped = json.loads(capped_serialized)
    assert len(capped_serialized) <= PHASE_RESULT_BUDGET
    assert capped["truncated"] is True
    assert capped["truncation"]["omitted_artifact_count"] > 0

    packet["phase"] = "ques3"
    custom_budget = 1000
    save_phase_result(tmp_path, packet, budget=custom_budget)
    custom_serialized = (tmp_path / "phase_results" / "ques3.json").read_text(
        encoding="utf-8"
    )
    custom = json.loads(custom_serialized)
    assert len(custom_serialized) <= custom_budget
    assert custom["status"] == "success"
    assert custom["phase"] == "ques3"
    assert custom["evidence_status"] == "available"
    assert custom["limitations"] == ["execution_error: partial output retained"]
    assert all((tmp_path / item["path"]).is_file() for item in custom["artifacts"])


def test_missing_evidence_material_forbids_exact_claims():
    packet = {
        "task_id": "task",
        "phase": "ques2",
        "status": "failed",
        "evidence_status": "missing",
        "artifacts": [],
        "metrics": [],
        "limitations": ["metrics: unavailable"],
        "stdout": "unavailable",
    }

    material = render_phase_result_for_writer(packet)

    assert "证据状态：missing" in material
    assert "不得编造精确数字" in material


def test_successful_phase_keeps_bounded_summary_without_metrics():
    packet = {
        "task_id": "task",
        "phase": "ques1",
        "status": "success",
        "evidence_status": "ready_with_limitations",
        "summary": "模型：岭回归；方法：交叉验证；结论：预测误差保持稳定。",
        "metrics": [],
        "limitations": ["metrics: unavailable"],
        "artifacts": [],
        "stdout": "unavailable",
    }

    material = render_phase_result_for_writer(packet)

    assert "收工摘要：" in material
    assert "岭回归" in material
    assert "结论：预测误差保持稳定" in material
    assert "指标与事实：\n- unavailable" in material
    assert "阶段结果包：unavailable" not in material


def test_writer_material_budgets_keep_facts_and_drop_explanations():
    inventory = "\n".join(
        [
            "数据表清单:",
            "- cleaned/input.csv: 120 行 × 4 列；列: x, y, score, status",
            "status: readable",
            "指标: rows=120 columns=4",
            "限制: scan_incomplete",
            "stdout: " + "解释器输出 " * 4000,
            "解释文本: " + "这是过程说明 " * 4000,
            "清洗摘要:",
            "这是一段不应进入 Writer 的说明 " * 4000,
        ]
    )
    bounded_inventory = bound_data_inventory(inventory)

    packet = {
        "phase": "ques1",
        "status": "success",
        "evidence_status": "available",
        "model_reference": "modeler_solution:ques1",
        "artifacts": [
            {"path": "results/plot.png", "kind": "image"},
            {"path": "results/table.csv", "kind": "csv"},
        ],
        "metrics": ["accuracy=0.91"],
        "limitations": ["scan_incomplete"],
        "stdout": "stdout should not be rendered",
        "summary": "explanation should not be rendered",
    }
    phase_material = render_phase_result_for_writer(packet)

    assert len(bounded_inventory) <= DATA_INVENTORY_BUDGET
    assert "cleaned/input.csv" in bounded_inventory
    assert "status: readable" in bounded_inventory
    assert "rows=120" in bounded_inventory
    assert "scan_incomplete" in bounded_inventory
    assert "解释器输出" not in bounded_inventory
    assert "过程说明" not in bounded_inventory
    assert "不应进入 Writer" not in bounded_inventory

    assert len(phase_material) <= 12000
    assert "results/plot.png" in phase_material
    assert "accuracy=0.91" in phase_material
    assert "scan_incomplete" in phase_material
    assert "stdout should not be rendered" not in phase_material
    assert "explanation should not be rendered" not in phase_material


def test_image_list_and_final_prompt_have_independent_hard_budgets():
    images = [f"results/plot_{index:04d}.png" for index in range(1000)]
    images.append("results/not-an-image.csv")
    image_material = render_image_list_for_writer(images)

    base_prompt = "\n".join(
        [
            "path=cleaned/input.csv",
            "状态：success",
            "指标：accuracy=0.91",
            "限制：scan_incomplete",
            "stdout: " + "discard this stdout " * 2000,
            "解释文本：" + "discard this explanation " * 2000,
            "模板上下文：" + "模板 " * 3000,
        ]
    )
    final_prompt = bound_writer_user_prompt(base_prompt, images[:40])

    assert len(image_material) <= IMAGE_LIST_BUDGET
    assert len(final_prompt) <= WRITER_PROMPT_BUDGET
    assert "results/plot_0000.png" in image_material
    assert "results/not-an-image.csv" not in image_material
    assert "results/plot_0000.png" in final_prompt
    assert "path=cleaned/input.csv" in final_prompt
    assert "状态：success" in final_prompt
    assert "accuracy=0.91" in final_prompt
    assert "scan_incomplete" in final_prompt
    assert "discard this stdout" not in final_prompt
    assert "discard this explanation" not in final_prompt


def test_failed_phase_material_hides_partial_exact_evidence():
    packet = {
        "task_id": "task",
        "phase": "ques2",
        "status": "failed",
        "evidence_status": "missing",
        "error_summary": "达到最大重试次数",
        "artifacts": [{"path": "partial.png", "kind": "image"}],
        "metrics": ["accuracy=0.99"],
        "limitations": ["execution_error: partial output retained"],
        "stdout": "accuracy=0.99",
    }

    material = render_phase_result_for_writer(packet)

    assert "状态：failed" in material
    assert "失败原因：达到最大重试次数" in material
    assert "不能把该问题写成已完成" in material
    assert "accuracy=0.99" not in material
