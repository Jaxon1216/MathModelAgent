"""E2E 评估脚本：读取 trace JSONL + work_dir 产物，输出 scorecard JSON。

用法:
    python scripts/eval_task.py --task-id {task_id}
    python scripts/eval_task.py --task-id {task_id} --baseline fixtures/baseline/social-media/expected.json
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
TRACES_DIR = PROJECT_ROOT / "logs" / "traces"
WORK_DIR_ROOT = PROJECT_ROOT / "project" / "work_dir"


# ---- Trace 读取 ----

def load_trace(task_id: str) -> list[dict]:
    """读取 JSONL trace 文件，返回事件列表。"""
    trace_path = TRACES_DIR / f"{task_id}.jsonl"
    if not trace_path.exists():
        print(f"[eval] trace 文件不存在: {trace_path}", file=sys.stderr)
        return []
    events = []
    with open(trace_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return events


# ---- Agent 质量指标 ----

def compute_agent_quality(events: list[dict]) -> dict[str, Any]:
    """从 trace 事件计算 Agent 质量指标。"""
    execute_ok = execute_err = 0
    react_retry_total = 0
    completion_blocked = 0
    phase_fail = 0
    tool_counts: Counter = Counter()
    turns_by_phase: dict[str, int] = {}

    for ev in events:
        event_type = ev.get("event", "")
        payload = ev.get("payload", {})
        phase = ev.get("phase", "")

        if event_type == "execute.done":
            if payload.get("error"):
                execute_err += 1
            else:
                execute_ok += 1

        if event_type == "react.reflect":
            react_retry_total += 1

        if event_type == "react.completion_check":
            if payload.get("blocked_exit"):
                completion_blocked += 1

        if event_type == "phase.end":
            if not payload.get("success", True):
                phase_fail += 1

        if event_type == "tool.call":
            tool_counts[payload.get("tool_name", "unknown")] += 1

        if event_type == "subtask.summary":
            if phase:
                turns_by_phase[phase] = payload.get("turns", 0)

    total_exec = execute_ok + execute_err
    error_rate = round(execute_err / total_exec, 3) if total_exec > 0 else 0

    return {
        "execute_error_rate": error_rate,
        "execute_ok": execute_ok,
        "execute_err": execute_err,
        "react_retry_total": react_retry_total,
        "completion_blocked": completion_blocked,
        "phase_fail": phase_fail,
        "turns_per_phase": turns_by_phase,
        "tool_calls_by_name": dict(tool_counts),
    }


# ---- 绘图质量指标 ----

def compute_figure_quality(
    events: list[dict], work_dir: Path, res_md_path: Path
) -> dict[str, Any]:
    """从 trace + work_dir 计算绘图质量指标。"""
    png_by_phase: dict[str, int] = {}
    png_total = 0

    for ev in events:
        if ev.get("event") == "subtask.summary":
            phase = ev.get("phase", "")
            png_count = ev.get("payload", {}).get("png_count", 0)
            if phase:
                png_by_phase[phase] = png_count
            png_total += png_count

    # 备份：从 artifact.created 统计 png
    if png_total == 0:
        for ev in events:
            if ev.get("event") == "artifact.created":
                if ev.get("payload", {}).get("kind") == "png":
                    png_total += 1

    # work_dir 中的 png 文件数
    work_dir_pngs = sorted(work_dir.glob("*.png")) if work_dir.exists() else []
    work_dir_png_count = len(work_dir_pngs)

    # image_coverage: res.md 中 ![alt](file) 的引用数 / work_dir png 数
    md_image_refs = 0
    if res_md_path.exists():
        md_text = res_md_path.read_text(encoding="utf-8")
        md_image_refs = len(re.findall(r"!\[.*?\]\(.+?\.(?:png|jpg|jpeg|svg)\)", md_text))
    image_coverage = (
        round(md_image_refs / work_dir_png_count, 3) if work_dir_png_count > 0 else 0
    )

    # duplicate png names across phases: 从 subtask.summary 无法直接比较跨 phase 文件名
    # 用 artifact.created 的 path 字段来检测
    png_paths_by_phase: dict[str, set] = defaultdict(set)
    for ev in events:
        if ev.get("event") == "artifact.created":
            p = ev.get("payload", {})
            if p.get("kind") == "png":
                phase = ev.get("phase", "unknown")
                png_paths_by_phase[phase].add(p.get("path", ""))

    duplicate_count = 0
    all_names: set = set()
    for paths in png_paths_by_phase.values():
        for p in paths:
            name = Path(p).name
            if name in all_names:
                duplicate_count += 1
            all_names.add(name)

    return {
        "png_per_phase": png_by_phase,
        "png_total_from_trace": png_total,
        "png_in_work_dir": work_dir_png_count,
        "image_coverage": image_coverage,
        "md_image_refs": md_image_refs,
        "duplicate_png_names": duplicate_count,
    }


# ---- 论文结构指标 ----

def compute_paper_structure(
    res_json_path: Path, res_md_path: Path, res_docx_path: Path
) -> dict[str, Any]:
    """从产物文件计算论文结构指标。"""
    empty_sections: list[str] = []
    ref_count = 0
    in_text_cite_count = 0
    alt_is_filename_ratio = 0.0

    # res.json
    if res_json_path.exists():
        try:
            res_data = json.loads(res_json_path.read_text(encoding="utf-8"))
            for key, value in res_data.items():
                content = value.get("response_content", "")
                if isinstance(content, str) and len(content.strip()) == 0:
                    empty_sections.append(key)
                elif not content:
                    empty_sections.append(key)
        except (json.JSONDecodeError, OSError):
            pass

    # res.md
    if res_md_path.exists():
        md_text = res_md_path.read_text(encoding="utf-8")
        # 脚注定义
        ref_count = len(re.findall(r"\[\^\d+\]:", md_text))
        # 文中引用
        in_text_cite_count = len(re.findall(r"\[\^\d+\]", md_text))
        # 图片 alt 质量：检测 alt 是否为纯文件名（不含中文描述）
        img_matches = re.findall(r"!\[(.*?)\]\((.+?\.(?:png|jpg|jpeg|svg))\)", md_text)
        if img_matches:
            filename_alts = 0
            for alt, src in img_matches:
                src_name = src.split("/")[-1].rsplit(".", 1)[0]
                # alt 为空、alt 等于文件名、alt 纯英文数字下划线 → 可能是文件名
                if (
                    not alt.strip()
                    or alt.strip() == src_name
                    or bool(re.match(r"^[a-zA-Z0-9_\-\.]+$", alt.strip()))
                ):
                    filename_alts += 1
            alt_is_filename_ratio = round(filename_alts / len(img_matches), 3)

    # docx smoke
    docx_smoke = res_docx_path.exists() and res_docx_path.stat().st_size > 0

    return {
        "empty_sections": empty_sections,
        "empty_section_count": len(empty_sections),
        "ref_count": ref_count,
        "in_text_cite_count": in_text_cite_count,
        "alt_is_filename_ratio": alt_is_filename_ratio,
        "docx_smoke": docx_smoke,
    }


# ---- 基线回归检测 ----

def load_baseline(baseline_path: str) -> dict:
    """读取基线 expected.json。"""
    path = Path(baseline_path)
    if not path.exists():
        print(f"[eval] 基线文件不存在: {path}", file=sys.stderr)
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def check_regression(scorecard: dict, baseline: dict) -> dict[str, Any]:
    """对比 scorecard 与 baseline，判定是否退化。"""
    checks: list[dict[str, Any]] = []

    # min_png_per_ques: 每个 ques 至少 N 张图
    if "min_png_per_ques" in baseline:
        png_per = scorecard.get("figure_quality", {}).get("png_per_phase", {})
        threshold = baseline["min_png_per_ques"]
        for phase, count in png_per.items():
            if phase.startswith("ques"):
                ok = count >= threshold
                checks.append({
                    "metric": f"min_png_per_ques.{phase}",
                    "value": count,
                    "threshold": f">= {threshold}",
                    "pass": ok,
                })

    # max_execute_error_rate
    if "max_execute_error_rate" in baseline:
        actual = scorecard.get("agent_quality", {}).get("execute_error_rate", 0)
        threshold = baseline["max_execute_error_rate"]
        checks.append({
            "metric": "max_execute_error_rate",
            "value": actual,
            "threshold": f"<= {threshold}",
            "pass": actual <= threshold,
        })

    # max_empty_sections
    if "max_empty_sections" in baseline:
        actual = scorecard.get("paper_structure", {}).get("empty_section_count", 0)
        threshold = baseline["max_empty_sections"]
        checks.append({
            "metric": "max_empty_sections",
            "value": actual,
            "threshold": f"<= {threshold}",
            "pass": actual <= threshold,
        })

    # min_image_coverage
    if "min_image_coverage" in baseline:
        actual = scorecard.get("figure_quality", {}).get("image_coverage", 0)
        threshold = baseline["min_image_coverage"]
        checks.append({
            "metric": "min_image_coverage",
            "value": actual,
            "threshold": f">= {threshold}",
            "pass": actual >= threshold,
        })

    # must_call_tools
    if "must_call_tools" in baseline:
        tool_counts = scorecard.get("agent_quality", {}).get("tool_calls_by_name", {})
        for tool_name in baseline["must_call_tools"]:
            called = tool_name in tool_counts
            checks.append({
                "metric": f"must_call_tools.{tool_name}",
                "value": "called" if called else "missing",
                "threshold": "called",
                "pass": called,
            })

    all_pass = all(c["pass"] for c in checks) if checks else None
    return {
        "all_pass": all_pass,
        "checks": checks,
    }


# ---- 主入口 ----

def main() -> None:
    parser = argparse.ArgumentParser(
        description="E2E 评估：读取 trace + work_dir，输出 scorecard JSON"
    )
    parser.add_argument("--task-id", required=True, help="任务 ID")
    parser.add_argument(
        "--baseline", default=None, help="基线 expected.json 路径（可选，用于回归检测）"
    )
    args = parser.parse_args()

    task_id: str = args.task_id
    baseline_path: str | None = args.baseline

    work_dir = WORK_DIR_ROOT / task_id

    events = load_trace(task_id)
    if not events:
        print(f"[eval] 无 trace 事件，仅基于产物输出", file=sys.stderr)

    scorecard: dict[str, Any] = {
        "task_id": task_id,
        "agent_quality": compute_agent_quality(events),
        "figure_quality": compute_figure_quality(events, work_dir, work_dir / "res.md"),
        "paper_structure": compute_paper_structure(
            work_dir / "res.json", work_dir / "res.md", work_dir / "res.docx"
        ),
    }

    if baseline_path:
        baseline = load_baseline(baseline_path)
        scorecard["regression_check"] = check_regression(scorecard, baseline)

    print(json.dumps(scorecard, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
