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
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.utils.data_contract import is_result_template  # noqa: E402
from app.utils.source_inspection import SUPPORTED_SUFFIXES  # noqa: E402

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
    res_json_path: Path, res_md_path: Path, res_docx_path: Path
) -> dict[str, Any]:
    """从产物文件计算论文结构指标。"""
    empty_sections: list[str] = []
    ref_count = 0
    in_text_cite_count = 0
    alt_is_filename_ratio = 0.0

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
        "empty_section_count": len(empty_sections),
        "ref_count": ref_count,
        "in_text_cite_count": in_text_cite_count,
        "alt_is_filename_ratio": alt_is_filename_ratio,
        **docx_info,
    }


# ---- 数据交接、Writer 泄漏和 manifest 指标 ----


def _latest_event(events: list[dict], event_name: str) -> dict | None:
    """返回某类事件最后一次记录。"""
    for event in reversed(events):
        if event.get("event") == event_name:
            return event
    return None


def _load_json_object(path: Path) -> dict[str, Any] | None:
    """读取 JSON 对象；评估不因单个损坏产物中断。"""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _as_nonnegative_int(value: Any) -> int | None:
    """只接受可验证的非负整数，缺失或非法值返回 ``None``。"""
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


_RAW_QUESTION_KEYS = (
    "raw_ques_all",
    "raw_problem_text",
    "original_problem_text",
    "source_problem_text",
)
_WRITER_HANDOFF_TEXT_KEYS = (
    "prompt",
    "material",
    "writer_prompt",
    "writer_material",
    "public_context",
)


def _collect_text_values(
    value: Any,
    *,
    excluded_keys: frozenset[str] = frozenset(),
) -> list[str]:
    """从 handoff 的文本或结构化上下文中提取可验证字符串。"""
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, Mapping):
        values: list[str] = []
        for key, nested in value.items():
            if str(key) in excluded_keys:
                continue
            values.extend(
                _collect_text_values(nested, excluded_keys=excluded_keys)
            )
        return values
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        values = []
        for nested in value:
            values.extend(_collect_text_values(nested, excluded_keys=excluded_keys))
        return values
    return []


def _find_named_text(value: Any, names: frozenset[str]) -> str | None:
    """递归寻找命名的原始题面参考文本。"""
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key) in names:
                candidates = _collect_text_values(nested)
                if candidates:
                    return "\n".join(candidates)
        for nested in value.values():
            found = _find_named_text(nested, names)
            if found is not None:
                return found
    return None


def _normalize_leakage_text(text: str) -> str:
    """只折叠空白并统一大小写，保留题面标点以避免误判。"""
    return " ".join(text.casefold().split())


def _writer_handoff_evidence(payload: Mapping[str, Any]) -> tuple[str | None, list[str]]:
    """返回原始题面参考和 Writer 实际收到的文本。"""
    raw_reference = _find_named_text(payload, frozenset(_RAW_QUESTION_KEYS))
    handoff_texts: list[str] = []
    excluded_keys = frozenset(_RAW_QUESTION_KEYS)
    for key in _WRITER_HANDOFF_TEXT_KEYS:
        if key in payload:
            handoff_texts.extend(
                _collect_text_values(payload[key], excluded_keys=excluded_keys)
            )
    return raw_reference, handoff_texts


def _raw_question_is_in_handoff(
    raw_reference: str | None, handoff_texts: Sequence[str]
) -> bool | None:
    """通过原始题面参考和实际 handoff 文本判断是否发生泄漏。"""
    if not raw_reference or not handoff_texts:
        return None
    normalized_reference = _normalize_leakage_text(raw_reference)
    if not normalized_reference:
        return None
    return any(
        normalized_reference in _normalize_leakage_text(text)
        for text in handoff_texts
    )


def _actual_source_files(work_dir: Path) -> list[Path]:
    """返回任务根目录中实际存在且非结果模板的表格文件。"""
    if not work_dir.is_dir():
        return []
    return sorted(
        path
        for path in work_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() in SUPPORTED_SUFFIXES
        and not is_result_template(path.name)
    )


def _actual_source_sheets(
    work_dir: Path, source_files: list[Path]
) -> tuple[set[tuple[str, str]], bool]:
    """读取实际源文件的 sheet 名；解析失败时标记 sheet 证据不完整。"""
    sheets: set[tuple[str, str]] = set()
    complete = True
    for path in source_files:
        relpath = path.relative_to(work_dir).as_posix()
        if path.suffix.lower() == ".csv":
            sheets.add((relpath, "Sheet1"))
            continue
        try:
            workbook = pd.ExcelFile(path)
            try:
                sheets.update((relpath, str(name)) for name in workbook.sheet_names)
            finally:
                workbook.close()
        except Exception:
            complete = False
    return sheets, complete


def _report_sheets(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """读取报告中的合法 sheet 条目，避免坏报告中断评估。"""
    sheets = entry.get("sheets")
    if not isinstance(sheets, list):
        return []
    return [sheet for sheet in sheets if isinstance(sheet, dict)]


def _scan_is_incomplete(sheet: dict[str, Any]) -> bool:
    """读取 sheet 扫描状态；非法结构按缺证据处理。"""
    scan = sheet.get("scan")
    return isinstance(scan, dict) and scan.get("complete") is False


def _report_warnings(value: Any) -> list[Any]:
    """读取报告中的 warning 列表；非法字段按缺证据处理。"""
    return value if isinstance(value, list) else []


def compute_source_inspection_quality(
    events: list[dict], work_dir: Path
) -> dict[str, Any]:
    """统计源文件/sheet 探查覆盖、风险警告和扫描完整性。"""
    report_path = work_dir / "source_inspection.json"
    report = _load_json_object(report_path)
    trace_event = _latest_event(events, "source.inspection")
    trace_payload = (trace_event or {}).get("payload", {})
    if not isinstance(trace_payload, dict):
        trace_payload = {}
    rendered_chars = _as_nonnegative_int(trace_payload.get("rendered_chars"))
    render_budget = _as_nonnegative_int(trace_payload.get("render_budget"))
    actual_paths = _actual_source_files(work_dir)
    actual_source_files = {
        path.relative_to(work_dir).as_posix() for path in actual_paths
    }
    actual_sheets, sheet_evidence_complete = _actual_source_sheets(
        work_dir, actual_paths
    )

    if report is None:
        has_source_data = bool(actual_source_files)
        return {
            "exists": report_path.is_file(),
            "status": "unavailable" if has_source_data else "not_applicable",
            "data_status": "source_data" if has_source_data else "no_source_data",
            "source_file_count": len(actual_source_files),
            "indexed_source_file_count": None,
            "file_coverage": None,
            "sheet_count": None,
            "sheet_coverage": None,
            "readable_sheet_count": None,
            "readable_sheet_coverage": None,
            "warning_count": None,
            "warnings_recorded": None,
            "bounded_sheet_count": None,
            "scan_complete": None,
            "rendered_chars": rendered_chars,
            "render_budget": render_budget,
            "render_over_budget": (
                rendered_chars > render_budget
                if rendered_chars is not None and render_budget is not None
                else None
            ),
        }

    source_entries = [
        entry
        for entry in report.get("files") or []
        if isinstance(entry, dict) and entry.get("role") == "source_data"
    ]
    indexed_files = {
        str(entry.get("path") or "").replace("\\", "/") for entry in source_entries
    }
    candidate_sheets = [
        sheet for entry in source_entries for sheet in _report_sheets(entry)
    ]
    readable_sheet_count = sum(
        sheet.get("readable") is True for sheet in candidate_sheets
    )
    warning_values = [
        str(warning)
        for entry in source_entries
        for sheet in _report_sheets(entry)
        for warning in _report_warnings(sheet.get("warnings"))
    ]
    warning_values.extend(
        str(warning)
        for entry in source_entries
        for warning in _report_warnings(entry.get("warnings"))
        if str(warning).split(":", 1)[0]
        not in {str(sheet.get("name")) for sheet in _report_sheets(entry)}
    )
    bounded_sheet_count = sum(_scan_is_incomplete(sheet) for sheet in candidate_sheets)
    indexed_sheet_paths = {
        (
            str(entry.get("path") or "").replace("\\", "/"),
            str(sheet.get("name") or ""),
        )
        for entry in source_entries
        for sheet in _report_sheets(entry)
        if sheet.get("name")
    }
    file_coverage = (
        len(actual_source_files & indexed_files) / len(actual_source_files)
        if actual_source_files
        else None
    )
    sheet_coverage = (
        len(actual_sheets & indexed_sheet_paths) / len(actual_sheets)
        if actual_sheets and sheet_evidence_complete
        else None
    )
    readable_coverage = (
        readable_sheet_count / len(actual_sheets)
        if actual_sheets and sheet_evidence_complete
        else None
    )
    report_status = report.get("status")
    warning_evidence = bool(source_entries) and all(
        isinstance(entry.get("warnings"), list)
        and isinstance(entry.get("sheets"), list)
        and all(
            isinstance(sheet, dict) and isinstance(sheet.get("warnings"), list)
            for sheet in entry.get("sheets")
        )
        for entry in source_entries
    )
    return {
        "exists": True,
        "status": report_status if isinstance(report_status, str) else "unavailable",
        "data_status": report.get("data_status"),
        "source_file_count": len(actual_source_files),
        "indexed_source_file_count": len(actual_source_files & indexed_files),
        "file_coverage": (
            round(file_coverage, 3) if file_coverage is not None else None
        ),
        "sheet_count": len(actual_sheets) if sheet_evidence_complete else None,
        "indexed_sheet_count": (
            len(actual_sheets & indexed_sheet_paths)
            if sheet_evidence_complete
            else None
        ),
        "sheet_coverage": (
            round(sheet_coverage, 3) if sheet_coverage is not None else None
        ),
        "readable_sheet_count": (readable_sheet_count if candidate_sheets else None),
        "readable_sheet_coverage": (
            round(readable_coverage, 3) if readable_coverage is not None else None
        ),
        "warning_count": len(warning_values) if warning_evidence else None,
        "warnings_recorded": warning_evidence if source_entries else None,
        "bounded_sheet_count": bounded_sheet_count if candidate_sheets else None,
        "scan_complete": (bounded_sheet_count == 0 if candidate_sheets else None),
        "rendered_chars": rendered_chars,
        "render_budget": render_budget,
        "render_over_budget": (
            rendered_chars > render_budget
            if rendered_chars is not None and render_budget is not None
            else None
        ),
    }


def _add_budget_observation(
    observations: dict[str, dict[str, Any]],
    *,
    kind: str,
    chars: Any,
    budget: Any,
    truncated: Any = False,
    phase: str = "",
) -> None:
    """把 trace 中的一次交接预算记录聚合到稳定结构。"""
    char_count = _as_nonnegative_int(chars)
    budget_value = _as_nonnegative_int(budget)
    if char_count is None:
        return
    item = observations.setdefault(
        kind,
        {
            "count": 0,
            "max_chars": None,
            "budget": None,
            "over_budget_count": 0,
            "truncated_count": 0,
            "phases": {},
            "budget_observation_count": 0,
            "budget_missing_count": 0,
            "truncation_observation_count": 0,
        },
    )
    item["count"] += 1
    item["max_chars"] = (
        char_count if item["max_chars"] is None else max(item["max_chars"], char_count)
    )
    if budget_value is not None:
        item["budget"] = (
            budget_value
            if item["budget"] is None
            else max(item["budget"], budget_value)
        )
        item["budget_observation_count"] += 1
    if budget_value is not None and char_count > budget_value:
        item["over_budget_count"] += 1
    if budget_value is None:
        item["budget_missing_count"] += 1
    if isinstance(truncated, bool):
        item["truncation_observation_count"] += 1
        if truncated:
            item["truncated_count"] += 1
    if phase:
        previous = item["phases"].get(phase)
        item["phases"][phase] = (
            char_count if previous is None else max(previous, char_count)
        )


def compute_handoff_quality(events: list[dict], work_dir: Path) -> dict[str, Any]:
    """统计交接包、phase packet、技能材料和 history 的字符预算。"""
    observations: dict[str, dict[str, Any]] = {}
    incomplete_budget_event = False
    for event in events:
        if event.get("event") != "handoff.budget":
            continue
        payload = event.get("payload", {})
        if (
            not isinstance(payload, dict)
            or _as_nonnegative_int(payload.get("chars")) is None
        ):
            incomplete_budget_event = True
            continue
        _add_budget_observation(
            observations,
            kind=str(payload.get("kind") or "unknown"),
            chars=payload.get("chars"),
            budget=payload.get("budget"),
            truncated=payload.get("truncated"),
            phase=str(event.get("phase") or payload.get("phase") or ""),
        )

    packet_events: dict[str, dict] = {}
    for event in events:
        if event.get("event") != "phase.result":
            continue
        payload = event.get("payload", {})
        packet_path = payload.get("path") if isinstance(payload, dict) else None
        if isinstance(packet_path, str) and packet_path:
            packet_events[Path(packet_path).as_posix()] = event

    packet_dir = work_dir / "phase_results"
    packet_files = sorted(packet_dir.glob("*.json")) if packet_dir.is_dir() else []
    for packet_file in packet_files:
        try:
            packet_text = packet_file.read_text(encoding="utf-8")
        except OSError:
            continue
        packet = _load_json_object(packet_file) or {}
        relpath = packet_file.relative_to(work_dir).as_posix()
        packet_event = packet_events.get(relpath)
        packet_payload = packet_event.get("payload", {}) if packet_event else {}
        if not isinstance(packet_payload, dict):
            packet_payload = {}
        packet_phase = packet.get("phase") or (
            packet_event.get("phase") if packet_event else None
        )
        _add_budget_observation(
            observations,
            kind="phase_packet",
            chars=len(packet_text),
            budget=packet_payload.get("packet_budget"),
            truncated=(
                packet.get("truncated")
                if isinstance(packet.get("truncated"), bool)
                else packet_payload.get("truncated")
            ),
            phase=str(packet_phase or packet_file.stem),
        )

    history_max_chars: int | None = None
    history_max_messages: int | None = None
    history_by_phase: dict[str, dict[str, Any]] = {}
    history_budget: int | None = None
    compression_count = 0
    history_event_count = 0
    history_budget_missing = False
    for event in events:
        if event.get("event") not in {
            "history.update",
            "context.compress",
            "subtask.summary",
        }:
            continue
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            continue
        chars = _as_nonnegative_int(payload.get("history_chars"))
        messages = _as_nonnegative_int(payload.get("history_messages"))
        budget = _as_nonnegative_int(payload.get("history_budget_chars"))
        if chars is None and messages is None and budget is None:
            continue
        history_event_count += 1
        if (chars is not None or messages is not None) and budget is None:
            history_budget_missing = True
        if chars is not None:
            history_max_chars = (
                chars if history_max_chars is None else max(history_max_chars, chars)
            )
        if messages is not None:
            history_max_messages = (
                messages
                if history_max_messages is None
                else max(history_max_messages, messages)
            )
        if budget is not None:
            history_budget = (
                budget if history_budget is None else max(history_budget, budget)
            )
        phase = str(
            event.get("phase")
            or payload.get("phase")
            or payload.get("history_scope")
            or "unknown"
        )
        item = history_by_phase.setdefault(
            phase, {"max_chars": None, "max_messages": None, "updates": 0}
        )
        if chars is not None:
            item["max_chars"] = (
                chars if item["max_chars"] is None else max(item["max_chars"], chars)
            )
        if messages is not None:
            item["max_messages"] = (
                messages
                if item["max_messages"] is None
                else max(item["max_messages"], messages)
            )
        item["updates"] += 1
        compression_count += int(event.get("event") == "context.compress")

    skill_event_count = sum(event.get("event") == "skill.load" for event in events)
    skill_values = [
        value
        for event in events
        if event.get("event") == "skill.load"
        for value in [
            _as_nonnegative_int((event.get("payload") or {}).get("body_chars"))
        ]
        if value is not None
    ]
    skill_chars = (
        sum(skill_values)
        if skill_event_count and len(skill_values) == skill_event_count
        else None
    )
    history = {
        "max_chars": history_max_chars,
        "max_messages": history_max_messages,
        "budget": history_budget,
        "over_budget": (
            history_max_chars > history_budget
            if history_max_chars is not None and history_budget is not None
            else None
        ),
        "by_phase": history_by_phase,
        "compression_count": compression_count,
        "status": "available" if history_event_count else "unavailable",
    }
    budget_incomplete = (
        incomplete_budget_event
        or history_budget_missing
        or any(item["budget_missing_count"] for item in observations.values())
    )
    has_budget_evidence = bool(
        history_budget is not None
        or any(item["budget_observation_count"] for item in observations.values())
    )
    budget_violation_count = None
    if has_budget_evidence and not budget_incomplete:
        budget_violation_count = sum(
            int(value.get("over_budget_count", 0)) for value in observations.values()
        ) + int(history["over_budget"] is True)
    for item in observations.values():
        if item["budget_missing_count"]:
            item["over_budget_count"] = None
        if item["truncation_observation_count"] != item["count"]:
            item["truncated_count"] = None
        item.pop("budget_observation_count")
        item.pop("budget_missing_count")
        item.pop("truncation_observation_count")
    return {
        "budgets": observations,
        "history": history,
        "skill_preload_chars": skill_chars,
        "budget_violation_count": budget_violation_count,
        "status": (
            "available"
            if observations or history_event_count or skill_values
            else "unavailable"
        ),
    }


def compute_writer_leakage(events: list[dict]) -> dict[str, Any]:
    """从 Writer 实际交接文本和原始题面参考计算泄漏指标。"""
    handoffs = [
        event.get("payload", {})
        for event in events
        if event.get("event") == "writer.handoff"
    ]
    raw_values = [
        _raw_question_is_in_handoff(*_writer_handoff_evidence(payload))
        if isinstance(payload, Mapping)
        else None
        for payload in handoffs
    ]
    raw_reference_count = sum(
        raw_reference is not None
        for payload in handoffs
        if isinstance(payload, Mapping)
        for raw_reference, _ in [_writer_handoff_evidence(payload)]
    )
    handoff_text_count = sum(
        bool(handoff_texts)
        for payload in handoffs
        if isinstance(payload, Mapping)
        for _, handoff_texts in [_writer_handoff_evidence(payload)]
    )
    evidence_gaps: list[str] = []
    if not handoffs:
        evidence_gaps.append("writer_handoff_unavailable")
    elif raw_reference_count < len(handoffs):
        evidence_gaps.append("raw_problem_reference_unavailable")
    if handoffs and handoff_text_count < len(handoffs):
        evidence_gaps.append("writer_handoff_text_unavailable")
    constraint_values = [
        payload["constraint_leakage"]
        for payload in handoffs
        if isinstance(payload, Mapping)
        and isinstance(payload.get("constraint_leakage"), bool)
    ]
    echo_values = [
        value
        for payload in handoffs
        if isinstance(payload, Mapping)
        for value in [_as_nonnegative_int(payload.get("constraint_echo_count"))]
        if value is not None
    ]
    prompt_values = [
        value
        for payload in handoffs
        if isinstance(payload, Mapping)
        for value in [_as_nonnegative_int(payload.get("prompt_chars"))]
        if value is not None
    ]
    raw_complete = bool(handoffs) and all(
        isinstance(value, bool) for value in raw_values
    )
    constraint_complete = bool(handoffs) and len(constraint_values) == len(handoffs)
    echo_complete = bool(handoffs) and len(echo_values) == len(handoffs)
    prompt_complete = bool(handoffs) and len(prompt_values) == len(handoffs)
    return {
        "checked_count": len(handoffs),
        "raw_ques_all_leaks": (
            sum(int(value) for value in raw_values if isinstance(value, bool))
            if raw_complete
            else None
        ),
        "raw_ques_all_status": "available" if raw_complete else "unavailable",
        "raw_reference_count": raw_reference_count,
        "raw_reference_missing_count": len(handoffs) - raw_reference_count,
        "handoff_text_count": handoff_text_count,
        "handoff_text_missing_count": len(handoffs) - handoff_text_count,
        "evidence_gaps": evidence_gaps,
        "constraint_leaks": (
            sum(constraint_values) if constraint_complete else None
        ),
        "constraint_echo_count": sum(echo_values) if echo_complete else None,
        "max_prompt_chars": max(prompt_values) if prompt_complete else None,
        "status": "available" if raw_complete else "unavailable",
    }


def compute_manifest_quality(events: list[dict], work_dir: Path) -> dict[str, Any]:
    """检查 task_manifest 的条目、文件引用、重复和跨 phase 冲突。"""
    manifest_path = work_dir / "task_manifest.json"
    manifest = _load_json_object(manifest_path)
    if manifest is None:
        return {
            "exists": manifest_path.is_file(),
            "valid": None,
            "status": "unavailable",
            "missing_required_entries": ["task_manifest.json"],
            "duplicate_paths": None,
            "stale_entries": None,
            "missing_references": None,
            "phase_conflict_count": None,
            "missing_file_count": None,
            "status_consistent": None,
            "artifact_count": None,
            "completeness_pass": None,
        }

    raw_artifacts = manifest.get("artifacts")
    artifacts = raw_artifacts if isinstance(raw_artifacts, list) else []
    paths = [
        str(item.get("path"))
        for item in artifacts
        if isinstance(item, dict) and item.get("path")
    ]
    duplicate_paths = sorted(
        path for path, count in Counter(paths).items() if count > 1
    )
    invalid_entries = [
        str(index)
        for index, item in enumerate(artifacts)
        if not isinstance(item, dict)
        or not item.get("path")
        or not item.get("kind")
        or "phase" not in item
        or "exists" not in item
    ]
    if not isinstance(raw_artifacts, list):
        invalid_entries.append("artifacts")
    stale_entries: list[str] = []
    for item in artifacts:
        if not isinstance(item, dict) or not item.get("path"):
            continue
        path = work_dir / str(item["path"])
        actual_exists = path.is_file()
        declared_exists = item.get("exists")
        if isinstance(declared_exists, bool) and declared_exists != actual_exists:
            stale_entries.append(str(item["path"]))

    by_path = {
        str(item["path"]): item
        for item in artifacts
        if isinstance(item, dict) and item.get("path")
    }
    required = {"res.json", "res.md", "res.docx"}
    if _actual_source_files(work_dir):
        required.add("source_inspection.json")
        required.add("data_contract.json")
    missing_required = sorted(path for path in required if path not in by_path)

    missing_references: list[str] = []
    packet_references: dict[str, set[str]] = defaultdict(set)
    packet_dir = work_dir / "phase_results"
    if packet_dir.is_dir():
        for packet_path in sorted(packet_dir.glob("*.json")):
            packet = _load_json_object(packet_path)
            packet_phase = str((packet or {}).get("phase") or packet_path.stem)
            for artifact in (packet or {}).get("artifacts") or []:
                if not isinstance(artifact, dict) or not artifact.get("path"):
                    continue
                artifact_path = str(artifact["path"])
                packet_references[artifact_path].add(packet_phase)
                if not (work_dir / artifact_path).is_file():
                    missing_references.append(artifact_path)
                if artifact_path not in by_path:
                    missing_references.append(f"unindexed:{artifact_path}")
    missing_references = sorted(set(missing_references))
    missing_file_count = sum(
        isinstance(item, dict)
        and bool(item.get("path"))
        and not (work_dir / str(item["path"])).is_file()
        for item in artifacts
    )
    phase_end_events = [
        event.get("event") == "phase.end" and isinstance(event.get("payload"), dict)
        for event in events
    ]
    failed_phase = any(
        event.get("event") == "phase.end"
        and (event.get("payload") or {}).get("success") is False
        for event in events
    )
    status = manifest.get("status")
    status = str(status) if status is not None else None
    status_consistent = (
        not failed_phase or status in {"failed", "partial_failure"}
        if any(phase_end_events)
        else None
    )
    phase_conflict_count = sum(len(phases) > 1 for phases in packet_references.values())
    structural_pass = bool(
        not invalid_entries
        and not missing_required
        and not duplicate_paths
        and not stale_entries
        and not missing_references
        and not (status == "completed" and missing_file_count)
    )
    completeness_pass = (
        structural_pass and status_consistent
        if status_consistent is not None
        else structural_pass
        if not structural_pass
        else None
    )
    return {
        "exists": True,
        "valid": not invalid_entries,
        "status": status,
        "missing_required_entries": missing_required,
        "duplicate_paths": duplicate_paths,
        "invalid_entries": invalid_entries,
        "stale_entries": sorted(set(stale_entries)),
        "missing_references": missing_references,
        "phase_conflict_count": phase_conflict_count,
        "missing_file_count": missing_file_count,
        "status_consistent": status_consistent,
        "artifact_count": len(artifacts),
        "completeness_pass": completeness_pass,
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

    if "min_source_inspection_coverage" in baseline:
        inspection = scorecard.get("source_inspection_quality", {})
        coverage_values = (
            inspection.get("file_coverage"),
            inspection.get("sheet_coverage"),
        )
        actual = (
            min(coverage_values)
            if all(
                isinstance(value, (int, float)) and not isinstance(value, bool)
                for value in coverage_values
            )
            else None
        )
        threshold = baseline["min_source_inspection_coverage"]
        checks.append(
            {
                "metric": "min_source_inspection_coverage",
                "value": actual,
                "threshold": f">= {threshold}",
                "pass": actual is not None and actual >= threshold,
            }
        )

    if "max_writer_raw_ques_all_leaks" in baseline:
        actual = scorecard.get("writer_leakage", {}).get("raw_ques_all_leaks")
        threshold = baseline["max_writer_raw_ques_all_leaks"]
        checks.append(
            {
                "metric": "max_writer_raw_ques_all_leaks",
                "value": actual,
                "threshold": f"<= {threshold}",
                "pass": actual is not None and actual <= threshold,
            }
        )

    if "max_writer_constraint_leaks" in baseline:
        actual = scorecard.get("writer_leakage", {}).get("constraint_leaks")
        threshold = baseline["max_writer_constraint_leaks"]
        checks.append(
            {
                "metric": "max_writer_constraint_leaks",
                "value": actual,
                "threshold": f"<= {threshold}",
                "pass": actual is not None and actual <= threshold,
            }
        )

    if "max_manifest_missing_references" in baseline:
        references = scorecard.get("manifest_quality", {}).get("missing_references")
        actual = len(references) if isinstance(references, list) else None
        threshold = baseline["max_manifest_missing_references"]
        checks.append(
            {
                "metric": "max_manifest_missing_references",
                "value": actual,
                "threshold": f"<= {threshold}",
                "pass": actual is not None and actual <= threshold,
            }
        )

    if "max_handoff_budget_violations" in baseline:
        actual = scorecard.get("handoff_quality", {}).get("budget_violation_count")
        threshold = baseline["max_handoff_budget_violations"]
        checks.append(
            {
                "metric": "max_handoff_budget_violations",
                "value": actual,
                "threshold": f"<= {threshold}",
                "pass": actual is not None and actual <= threshold,
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

    scorecard: dict[str, Any] = {
        "task_id": task_id,
        "agent_quality": compute_agent_quality(events),
        "llm_metrics": compute_llm_metrics(events),
        "figure_quality": compute_figure_quality(events, work_dir, work_dir / "res.md"),
        "paper_structure": compute_paper_structure(
            work_dir / "res.json", work_dir / "res.md", work_dir / "res.docx"
        ),
        "source_inspection_quality": compute_source_inspection_quality(
            events, work_dir
        ),
        "handoff_quality": compute_handoff_quality(events, work_dir),
        "writer_leakage": compute_writer_leakage(events),
        "manifest_quality": compute_manifest_quality(events, work_dir),
    }

    if baseline_path:
        baseline = load_baseline(baseline_path)
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
        "empty_sections",
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
            entry["improved"] = (
                new_val > old_val
                if key.endswith("_coverage")
                or key.endswith("_count")
                and "empty" not in key
                and "error" not in key
                else new_val < old_val
            )
        diffs.append(entry)

    return {
        "old_task_id": old_scorecard.get("task_id"),
        "new_task_id": new_scorecard.get("task_id"),
        "diffs": diffs,
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
