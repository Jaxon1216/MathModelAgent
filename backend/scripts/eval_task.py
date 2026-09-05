"""E2E 评估脚本：读取 trace JSONL + work_dir 产物，输出 scorecard JSON。

用法:
    python scripts/eval_task.py --task-id {task_id}
    python scripts/eval_task.py --task-id {task_id} --baseline fixtures/baseline/2024高教杯C题/expected.json
    python scripts/eval_task.py --task-id {task_id} --baseline ... --save-scorecard
    python scripts/eval_task.py --task-id {new_id} --compare {old_task_id}
"""

from __future__ import annotations

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
DEFAULT_BASELINE_DIR = PROJECT_ROOT / "fixtures" / "baseline" / "2024高教杯C题"
SCORECARDS_DIR = DEFAULT_BASELINE_DIR / "scorecards"


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


def compute_agent_quality(
    events: list[dict],
    required_phases: list[str] | None = None,
) -> dict[str, Any]:
    """从 trace 事件计算 Agent 质量指标。"""
    execute_ok = execute_err = 0
    react_retry_total = 0
    completion_blocked = 0
    phase_fail = 0
    tool_counts: Counter = Counter()
    load_skill_tool_calls: set[tuple[str, str]] = set()
    successful_skill_loads: list[tuple[str, str, str]] = []
    turns_by_phase: dict[str, int] = {}
    started_phases: set[str] = set()
    ended_phases: dict[str, str] = {}
    summarized_phases: set[str] = set()

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
            status = payload.get("status")
            if status is None:
                status = "success" if payload.get("success", True) else "failed"
            if phase:
                ended_phases[phase] = status
            if status in {"failed", "blocked"}:
                phase_fail += 1

        if event_type == "phase.start" and phase:
            started_phases.add(phase)

        if event_type == "tool.call":
            tool_name = payload.get("tool_name", "unknown")
            tool_counts[tool_name] += 1
            tool_call_id = payload.get("tool_call_id")
            if (
                tool_name == "load_skill"
                and isinstance(phase, str)
                and isinstance(tool_call_id, str)
            ):
                load_skill_tool_calls.add((phase, tool_call_id))

        if event_type == "skill.load":
            tool_call_id = payload.get("tool_call_id")
            skill_name = payload.get("skill_name")
            if (
                payload.get("found") is True
                and payload.get("load_source") == "tool_call"
                and isinstance(phase, str)
                and isinstance(tool_call_id, str)
                and isinstance(skill_name, str)
                and (phase, tool_call_id) in load_skill_tool_calls
            ):
                successful_skill_loads.append((phase, tool_call_id, skill_name))

        if event_type == "subtask.summary":
            if phase:
                summarized_phases.add(phase)
                turns_by_phase[phase] = payload.get("turns", 0)

    total_exec = execute_ok + execute_err
    error_rate = round(execute_err / total_exec, 3) if total_exec > 0 else 0

    required = list(dict.fromkeys(required_phases or []))
    missing_phase_ends = [phase for phase in required if phase not in ended_phases]
    missing_summaries = [phase for phase in required if phase not in summarized_phases]
    degraded_phases = sorted(
        phase for phase, status in ended_phases.items() if status == "degraded"
    )
    loaded_skills_by_phase: dict[str, list[str]] = {}
    for phase, _, skill_name in successful_skill_loads:
        loaded_skills_by_phase.setdefault(phase, [])
        if skill_name not in loaded_skills_by_phase[phase]:
            loaded_skills_by_phase[phase].append(skill_name)
    for skill_names in loaded_skills_by_phase.values():
        skill_names.sort()

    return {
        "execute_error_rate": error_rate,
        "execute_ok": execute_ok,
        "execute_err": execute_err,
        "react_retry_total": react_retry_total,
        "completion_blocked": completion_blocked,
        "phase_fail": phase_fail,
        "phase_statuses": {
            phase: ended_phases.get(phase, "missing") for phase in required
        },
        "started_phases": sorted(started_phases),
        "missing_phase_ends": missing_phase_ends,
        "missing_phase_end_count": len(missing_phase_ends),
        "missing_subtask_summaries": missing_summaries,
        "missing_subtask_summary_count": len(missing_summaries),
        "degraded_phases": degraded_phases,
        "degraded_phase_count": len(degraded_phases),
        "turns_per_phase": turns_by_phase,
        "tool_calls_by_name": dict(tool_counts),
        "loaded_skills_by_phase": loaded_skills_by_phase,
    }


# ---- LLM 层指标 ----


def compute_llm_metrics(events: list[dict]) -> dict[str, Any]:
    """聚合 llm.response 事件的 token/延迟/调用次数。"""
    llm_call_count = 0
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_tokens = 0
    total_cache_read_tokens = 0
    total_reasoning_tokens = 0
    total_latency_ms = 0

    by_agent: dict[str, dict[str, int]] = defaultdict(
        lambda: {"calls": 0, "tokens": 0, "latency_ms": 0}
    )
    by_model: dict[str, dict[str, int]] = defaultdict(lambda: {"calls": 0, "tokens": 0})

    for ev in events:
        if ev.get("event") != "llm.response":
            continue

        payload = ev.get("payload", {})
        agent = ev.get("agent") or payload.get("agent") or "unknown"
        model = payload.get("model") or "unknown"

        prompt_tokens = int(payload.get("prompt_tokens") or 0)
        completion_tokens = int(payload.get("completion_tokens") or 0)
        tokens = int(payload.get("total_tokens") or prompt_tokens + completion_tokens)
        cache_read = int(payload.get("cache_read_tokens") or 0)
        reasoning = int(payload.get("reasoning_tokens") or 0)
        latency_ms = int(payload.get("latency_ms") or 0)

        llm_call_count += 1
        total_prompt_tokens += prompt_tokens
        total_completion_tokens += completion_tokens
        total_tokens += tokens
        total_cache_read_tokens += cache_read
        total_reasoning_tokens += reasoning
        total_latency_ms += latency_ms

        by_agent[agent]["calls"] += 1
        by_agent[agent]["tokens"] += tokens
        by_agent[agent]["latency_ms"] += latency_ms

        by_model[model]["calls"] += 1
        by_model[model]["tokens"] += tokens

    return {
        "llm_call_count": llm_call_count,
        "total_prompt_tokens": total_prompt_tokens,
        "total_completion_tokens": total_completion_tokens,
        "total_tokens": total_tokens,
        "total_cache_read_tokens": total_cache_read_tokens,
        "total_reasoning_tokens": total_reasoning_tokens,
        "total_latency_ms": total_latency_ms,
        "by_agent": dict(by_agent),
        "by_model": dict(by_model),
    }


# ---- 绘图质量指标 ----


def compute_figure_quality(
    events: list[dict],
    work_dir: Path,
    res_md_path: Path,
    expected_question_phases: list[str] | None = None,
) -> dict[str, Any]:
    """从 trace + work_dir 计算绘图质量指标。"""
    png_by_phase: dict[str, int] = {
        phase: 0
        for phase in (expected_question_phases or [])
        if phase.startswith("ques")
    }
    png_total = 0

    for ev in events:
        if ev.get("event") == "subtask.summary":
            phase = ev.get("phase", "")
            png_count = ev.get("payload", {}).get("png_count", 0)
            if phase:
                png_by_phase[phase] = png_count
            png_total += png_count

    if png_total == 0:
        for ev in events:
            if ev.get("event") == "artifact.created":
                if ev.get("payload", {}).get("kind") == "png":
                    png_total += 1

    work_dir_pngs = sorted(work_dir.glob("*.png")) if work_dir.exists() else []
    work_dir_png_count = len(work_dir_pngs)

    md_image_refs = 0
    if res_md_path.exists():
        md_text = res_md_path.read_text(encoding="utf-8")
        md_image_refs = len(
            re.findall(r"!\[.*?\]\(.+?\.(?:png|jpg|jpeg|svg)\)", md_text)
        )
    image_coverage = (
        round(md_image_refs / work_dir_png_count, 3) if work_dir_png_count > 0 else 0
    )

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


# ---- docx 深度检查 ----


def inspect_docx(docx_path: Path) -> dict[str, Any]:
    """解析 docx，检查图片与公式对象是否存在。"""
    if not docx_path.exists() or docx_path.stat().st_size == 0:
        return {
            "docx_exists": False,
            "docx_has_images": False,
            "docx_has_math": False,
            "docx_paragraph_count": 0,
        }

    try:
        from docx import Document

        doc = Document(str(docx_path))
        paragraph_count = len(doc.paragraphs)

        has_images = False
        has_math = False
        for element in doc.element.body.iter():
            tag = element.tag
            if tag.endswith("}drawing") or tag.endswith("}pict"):
                has_images = True
            if tag.endswith("}oMath") or tag.endswith("}oMathPara"):
                has_math = True
            if has_images and has_math:
                break

        return {
            "docx_exists": True,
            "docx_has_images": has_images,
            "docx_has_math": has_math,
            "docx_paragraph_count": paragraph_count,
        }
    except Exception as exc:
        return {
            "docx_exists": True,
            "docx_has_images": False,
            "docx_has_math": False,
            "docx_paragraph_count": 0,
            "docx_error": str(exc),
        }


# ---- 论文结构指标 ----


def compute_paper_structure(
    res_json_path: Path,
    res_md_path: Path,
    res_docx_path: Path,
    required_sections: list[str] | None = None,
) -> dict[str, Any]:
    """从产物文件计算论文结构指标。"""
    empty_sections: list[str] = []
    missing_sections: list[str] = []
    invalid_sections: list[str] = []
    ref_count = 0
    in_text_cite_count = 0
    alt_is_filename_ratio = 0.0

    res_data: dict[str, Any] = {}
    if res_json_path.exists():
        try:
            res_data = json.loads(res_json_path.read_text(encoding="utf-8"))
            for key, value in res_data.items():
                content = value.get("response_content", "")
                if isinstance(content, str) and len(content.strip()) == 0:
                    empty_sections.append(key)
                elif not content:
                    empty_sections.append(key)
                elif _is_invalid_section_content(str(content)):
                    invalid_sections.append(key)
        except (json.JSONDecodeError, OSError):
            res_data = {}

    missing_sections = [
        section for section in (required_sections or []) if section not in res_data
    ]
    incomplete_sections = sorted(
        set(empty_sections) | set(missing_sections) | set(invalid_sections)
    )

    if res_md_path.exists():
        md_text = res_md_path.read_text(encoding="utf-8")
        ref_count = len(re.findall(r"\[\^\d+\]:", md_text))
        # 排除脚注定义行 [^n]:，只统计正文引用 [^n]
        in_text_cite_count = len(re.findall(r"\[\^\d+\](?!:)", md_text))
        img_matches = re.findall(r"!\[(.*?)\]\((.+?\.(?:png|jpg|jpeg|svg))\)", md_text)
        if img_matches:
            filename_alts = 0
            for alt, src in img_matches:
                src_name = src.split("/")[-1].rsplit(".", 1)[0]
                if (
                    not alt.strip()
                    or alt.strip() == src_name
                    or bool(re.match(r"^[a-zA-Z0-9_\-\.]+$", alt.strip()))
                ):
                    filename_alts += 1
            alt_is_filename_ratio = round(filename_alts / len(img_matches), 3)

    docx_info = inspect_docx(res_docx_path)

    return {
        "empty_sections": empty_sections,
        "missing_sections": missing_sections,
        "invalid_sections": invalid_sections,
        "incomplete_sections": incomplete_sections,
        "empty_section_count": len(incomplete_sections),
        "ref_count": ref_count,
        "in_text_cite_count": in_text_cite_count,
        "alt_is_filename_ratio": alt_is_filename_ratio,
        **docx_info,
    }


def _is_invalid_section_content(content: str) -> bool:
    """识别只描述执行失败、不能视为论文正文的占位内容。"""
    normalized = " ".join(content.lower().split())
    markers = (
        "本阶段未完成",
        "本节未能生成完整正文",
        "任务失败",
        "搜索文献失败",
        "阶段结果不可用",
        "unavailable",
    )
    return any(marker.lower() in normalized for marker in markers)


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

    if "min_png_per_ques" in baseline:
        png_per = scorecard.get("figure_quality", {}).get("png_per_phase", {})
        threshold = baseline["min_png_per_ques"]
        for phase, count in png_per.items():
            if phase.startswith("ques"):
                ok = count >= threshold
                checks.append(
                    {
                        "metric": f"min_png_per_ques.{phase}",
                        "value": count,
                        "threshold": f">= {threshold}",
                        "pass": ok,
                    }
                )

    if "max_execute_error_rate" in baseline:
        actual = scorecard.get("agent_quality", {}).get("execute_error_rate", 0)
        threshold = baseline["max_execute_error_rate"]
        checks.append(
            {
                "metric": "max_execute_error_rate",
                "value": actual,
                "threshold": f"<= {threshold}",
                "pass": actual <= threshold,
            }
        )

    if "max_empty_sections" in baseline:
        actual = scorecard.get("paper_structure", {}).get("empty_section_count", 0)
        threshold = baseline["max_empty_sections"]
        checks.append(
            {
                "metric": "max_empty_sections",
                "value": actual,
                "threshold": f"<= {threshold}",
                "pass": actual <= threshold,
            }
        )

    if "min_image_coverage" in baseline:
        actual = scorecard.get("figure_quality", {}).get("image_coverage", 0)
        threshold = baseline["min_image_coverage"]
        checks.append(
            {
                "metric": "min_image_coverage",
                "value": actual,
                "threshold": f">= {threshold}",
                "pass": actual >= threshold,
            }
        )

    if "must_call_tools" in baseline:
        tool_counts = scorecard.get("agent_quality", {}).get("tool_calls_by_name", {})
        for tool_name in baseline["must_call_tools"]:
            called = tool_name in tool_counts
            checks.append(
                {
                    "metric": f"must_call_tools.{tool_name}",
                    "value": "called" if called else "missing",
                    "threshold": "called",
                    "pass": called,
                }
            )

    if "required_skill_loads_by_phase" in baseline:
        loaded_by_phase = scorecard.get("agent_quality", {}).get(
            "loaded_skills_by_phase",
            {},
        )
        for phase, expected_skills in baseline["required_skill_loads_by_phase"].items():
            actual = set(loaded_by_phase.get(phase, []))
            for skill_name in expected_skills:
                called = skill_name in actual
                checks.append(
                    {
                        "metric": f"required_skill_loads_by_phase.{phase}.{skill_name}",
                        "value": "called" if called else "missing",
                        "threshold": "called",
                        "pass": called,
                    }
                )

    all_pass = all(c["pass"] for c in checks) if checks else None
    return {
        "all_pass": all_pass,
        "checks": checks,
    }


# ---- Scorecard 保存与对比 ----


def build_scorecard(task_id: str, baseline_path: str | None = None) -> dict[str, Any]:
    """构建完整 scorecard。"""
    work_dir = WORK_DIR_ROOT / task_id
    events = load_trace(task_id)
    if not events:
        print("[eval] 无 trace 事件，仅基于产物输出", file=sys.stderr)

    baseline = load_baseline(baseline_path) if baseline_path else {}
    required_phases = baseline.get("required_solution_phases", [])
    question_phases = [
        phase for phase in required_phases if str(phase).startswith("ques")
    ]
    required_sections = baseline.get("required_paper_sections", [])

    scorecard: dict[str, Any] = {
        "task_id": task_id,
        "agent_quality": compute_agent_quality(events, required_phases),
        "llm_metrics": compute_llm_metrics(events),
        "figure_quality": compute_figure_quality(
            events,
            work_dir,
            work_dir / "res.md",
            expected_question_phases=question_phases,
        ),
        "paper_structure": compute_paper_structure(
            work_dir / "res.json",
            work_dir / "res.md",
            work_dir / "res.docx",
            required_sections=required_sections,
        ),
    }

    if baseline_path:
        scorecard["regression_check"] = check_regression(scorecard, baseline)

    return scorecard


def save_scorecard(scorecard: dict[str, Any], task_id: str) -> Path:
    """将 scorecard 写入 fixtures/baseline/.../scorecards/。"""
    SCORECARDS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = SCORECARDS_DIR / f"{task_id}.json"
    out_path.write_text(
        json.dumps(scorecard, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[eval] scorecard 已保存: {out_path}", file=sys.stderr)
    return out_path


def load_scorecard(task_id: str) -> dict[str, Any] | None:
    """从 scorecards 目录加载已保存的 scorecard。"""
    path = SCORECARDS_DIR / f"{task_id}.json"
    if not path.exists():
        print(f"[eval] scorecard 不存在: {path}", file=sys.stderr)
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _flatten_metrics(scorecard: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """将 scorecard 中可比较的标量指标展平为 metric_path -> value。"""
    flat: dict[str, Any] = {}
    skip_keys = {
        "by_agent",
        "by_model",
        "turns_per_phase",
        "png_per_phase",
        "tool_calls_by_name",
        "loaded_skills_by_phase",
        "phase_statuses",
        "started_phases",
        "missing_phase_ends",
        "missing_subtask_summaries",
        "degraded_phases",
        "empty_sections",
        "missing_sections",
        "invalid_sections",
        "incomplete_sections",
        "checks",
    }

    for key, value in scorecard.items():
        if key in ("task_id", "regression_check", "scorecard_compare"):
            continue
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            if key in skip_keys:
                flat[path] = value
            else:
                flat.update(_flatten_metrics(value, path))
        else:
            flat[path] = value
    return flat


def compare_scorecards(
    new_scorecard: dict[str, Any], old_scorecard: dict[str, Any]
) -> dict[str, Any]:
    """对比两次 scorecard，输出数值指标增减。"""
    old_flat = _flatten_metrics(old_scorecard)
    new_flat = _flatten_metrics(new_scorecard)

    all_keys = sorted(set(old_flat) | set(new_flat))
    diffs: list[dict[str, Any]] = []

    for key in all_keys:
        old_val = old_flat.get(key)
        new_val = new_flat.get(key)
        if old_val == new_val:
            continue
        entry: dict[str, Any] = {
            "metric": key,
            "old": old_val,
            "new": new_val,
        }
        if isinstance(old_val, (int, float)) and isinstance(new_val, (int, float)):
            entry["delta"] = round(new_val - old_val, 3)
            direction = _metric_direction(key)
            if direction == "higher":
                entry["improved"] = new_val > old_val
            elif direction == "lower":
                entry["improved"] = new_val < old_val
        diffs.append(entry)

    return {
        "old_task_id": old_scorecard.get("task_id"),
        "new_task_id": new_scorecard.get("task_id"),
        "diffs": diffs,
    }


def _metric_direction(metric: str) -> str | None:
    """返回已知标量指标的改进方向，未知指标不猜测。"""
    lower_markers = (
        "error",
        "fail",
        "missing",
        "empty",
        "invalid",
        "degraded",
        "retry",
        "blocked",
        "duplicate",
        "latency",
        "tokens",
        "llm_call_count",
    )
    higher_markers = (
        "coverage",
        "docx_exists",
        "docx_has_images",
        "docx_has_math",
        "ref_count",
        "in_text_cite_count",
    )
    if any(marker in metric for marker in lower_markers):
        return "lower"
    if any(marker in metric for marker in higher_markers):
        return "higher"
    return None


# ---- 主入口 ----


def main() -> None:
    parser = argparse.ArgumentParser(
        description="E2E 评估：读取 trace + work_dir，输出 scorecard JSON"
    )
    parser.add_argument("--task-id", required=True, help="任务 ID")
    parser.add_argument(
        "--baseline", default=None, help="基线 expected.json 路径（可选，用于回归检测）"
    )
    parser.add_argument(
        "--save-scorecard",
        action="store_true",
        help="将 scorecard 保存到 fixtures/baseline/2024高教杯C题/scorecards/",
    )
    parser.add_argument(
        "--compare",
        default=None,
        metavar="OLD_TASK_ID",
        help="与已保存的 scorecard 对比差异",
    )
    args = parser.parse_args()

    task_id: str = args.task_id
    scorecard = build_scorecard(task_id, args.baseline)

    if args.compare:
        old_scorecard = load_scorecard(args.compare)
        if old_scorecard:
            scorecard["scorecard_compare"] = compare_scorecards(
                scorecard, old_scorecard
            )

    if args.save_scorecard:
        save_scorecard(scorecard, task_id)

    print(json.dumps(scorecard, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
