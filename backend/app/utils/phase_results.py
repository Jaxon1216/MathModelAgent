"""阶段结果包：把 Coder 的可验证事实交接给 Writer。

结果包只索引已经落盘且存在的文件，并限制 stdout、摘要和指标文本的长度。
它不复制 Notebook、完整代码或任何 Agent 对话历史。
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from app.utils.problem_context import (
    RAW_PROBLEM_OVERLAP_THRESHOLD,
    redact_raw_problem_echoes,
    redact_execution_constraints,
    truncate_handoff_text,
)
from app.utils.source_inspection import redact_source_inspection_echoes

PHASE_RESULTS_DIR = "phase_results"
PHASE_RESULT_VERSION = 1
PHASE_RESULT_BUDGET = 6000
WRITER_MATERIAL_BUDGET = 12000
DATA_INVENTORY_BUDGET = 12000
IMAGE_LIST_BUDGET = 12000
WRITER_PROMPT_BUDGET = 12000
_PHASE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_FACT_MARKERS = (
    "metric",
    "accuracy",
    "precision",
    "recall",
    "f1",
    "rmse",
    "mae",
    "mse",
    "r2",
    "auc",
    "score",
    "loss",
    "p-value",
    "rows",
    "columns",
    "指标",
    "结果",
    "结论",
    "警告",
    "限制",
    "未完成",
    "unavailable",
)
_TRUNCATION_SUFFIX = "\n...[内容已截断]"
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg")
_ARTIFACT_SUFFIXES = _IMAGE_SUFFIXES + (
    ".csv",
    ".npy",
    ".json",
    ".xlsx",
    ".xls",
    ".pdf",
)
_LOW_PRIORITY_MARKERS = (
    "stdout",
    "解释器输出",
    "解释文本",
    "解释：",
    "说明文本",
    "清洗摘要",
    "summary",
    "explanation",
    "reasoning",
)
_ECHO_UNIT_BOUNDARY_RE = re.compile(
    r"(?<=[。！？!?；;，,\n])|(?<=[.])(?=\s|$)"
)
_CONTEXT_FIELD_PREFIX_RE = re.compile(r"^[^:：\n]{1,64}[:：]\s*")


def _is_low_priority_line(line: str) -> bool:
    """判断一行是否是可在预算压力下丢弃的 stdout/解释文本。"""
    lowered = line.casefold()
    return any(marker.casefold() in lowered for marker in _LOW_PRIORITY_MARKERS)


def _line_priority(line: str) -> int:
    """返回 Writer 交接行的保留优先级，数值越小越重要。"""
    lowered = line.casefold()
    if _is_low_priority_line(line):
        return 99
    if (
        "path" in lowered
        or "artifact" in lowered
        or "产物" in line
        or "cleaned/" in lowered
        or "results/" in lowered
        or any(
            re.search(rf"\{suffix}(?:[\s:;,)\]]|$)", lowered)
            for suffix in _ARTIFACT_SUFFIXES
        )
    ):
        return 0
    if any(marker in lowered for marker in ("status", "evidence", "状态", "证据")):
        return 1
    if any(marker in lowered for marker in _FACT_MARKERS):
        return 2
    if any(marker in lowered for marker in ("limitation", "warning", "限制", "警告")):
        return 3
    return 4


def _fit_priority_lines(lines: Sequence[str], budget: int) -> str:
    """按优先级保留完整行，并在末行必要时做字符级兜底。"""
    if budget <= 0:
        return ""

    candidates = [
        (index, line.strip())
        for index, line in enumerate(lines)
        if line and line.strip() and not _is_low_priority_line(line)
    ]
    selected: list[tuple[int, str]] = []
    used = 0
    for priority in range(5):
        for index, line in candidates:
            if _line_priority(line) != priority:
                continue
            separator = 1 if selected else 0
            remaining = budget - used - separator
            if remaining <= 0:
                continue
            if len(line) <= remaining:
                selected.append((index, line))
                used += separator + len(line)
            elif priority <= 3 and remaining > 0:
                # 路径、状态、指标和限制行应尽量保留；极端长值只能截断。
                selected.append((index, line[:remaining].rstrip()))
                used = budget
                break
        if used >= budget:
            break

    selected.sort(key=lambda item: item[0])
    return "\n".join(line for _, line in selected)


def bound_writer_material(text: str, budget: int = WRITER_MATERIAL_BUDGET) -> str:
    """限制 Writer 中间材料，优先保留可验证字段并丢弃解释性文本。"""
    safe_text = str(text or "")
    if len(safe_text) <= budget and not any(
        _is_low_priority_line(line) for line in safe_text.splitlines()
    ):
        return safe_text
    return _fit_priority_lines(safe_text.splitlines(), budget)


def bound_data_inventory(
    text: str, budget: int = DATA_INVENTORY_BUDGET
) -> str:
    """限制数据清单，优先保留真实路径、状态、指标、警告和限制。"""
    lines = str(text or "").splitlines()
    filtered: list[str] = []
    in_explanation = False
    for line in lines:
        if "清洗摘要" in line:
            in_explanation = True
            continue
        if in_explanation:
            continue
        filtered.append(line)
    return bound_writer_material("\n".join(filtered), budget)


def _normalize_image_paths(images: Sequence[Any] | None) -> list[str]:
    """去重并保留图片后缀路径，保持解释器返回顺序。"""
    if not isinstance(images, Sequence) or isinstance(
        images, (str, bytes, bytearray)
    ):
        return []
    paths: list[str] = []
    for image in images:
        if not isinstance(image, str):
            continue
        path = image.strip()
        if not path or not path.casefold().split("?", 1)[0].endswith(_IMAGE_SUFFIXES):
            continue
        if path not in paths:
            paths.append(path)
    return paths


def render_image_list_for_writer(
    images: Sequence[Any] | None,
    budget: int = IMAGE_LIST_BUDGET,
) -> str:
    """渲染有限图片清单，优先逐条保留真实图片路径。"""
    paths = _normalize_image_paths(images)
    if not paths or budget <= 0:
        return ""

    header = (
        "【必须插入的图片列表】\n"
        "以下图片是代码手生成的，请在相关段落后逐一插入；"
        "每张图片后配 3 行分析："
    )
    lines: list[str] = []
    used = len(header)
    for path in paths:
        line = f"- ![{path}]({path})"
        separator = 1 if lines else 1
        if used + separator + len(line) > budget:
            break
        lines.append(line)
        used += separator + len(line)
    if not lines:
        # 保留路径本身，即使图片路径异常长，也不让渲染结果越过预算。
        remaining = max(0, budget - len(header) - 1)
        if remaining:
            lines.append(f"- {paths[0][:remaining]}")
    return f"{header}\n" + "\n".join(lines)


def bound_writer_user_prompt(
    prompt: str,
    images: Sequence[Any] | None = None,
    budget: int = WRITER_PROMPT_BUDGET,
) -> str:
    """合并 Writer user prompt 和图片清单，并执行最后总预算兜底。"""
    if budget <= 0:
        return ""

    image_material = render_image_list_for_writer(images, budget)
    prompt_text = str(prompt or "").strip()
    if not image_material:
        return bound_writer_material(prompt_text, budget)
    if len(prompt_text) + 2 + len(image_material) <= budget:
        return f"{prompt_text}\n\n{image_material}".strip()

    # 图片路径比解释性上下文更难恢复，先为图片清单保留最大可用空间。
    remaining = max(0, budget - len(image_material) - 2)
    bounded_prompt = bound_writer_material(prompt_text, remaining)
    if bounded_prompt:
        return f"{bounded_prompt}\n\n{image_material}".strip()
    return image_material[:budget]


def phase_result_relpath(phase: str) -> str:
    """返回阶段结果包相对路径。"""
    if not _PHASE_RE.fullmatch(phase):
        raise ValueError(f"非法 phase 名称: {phase}")
    return f"{PHASE_RESULTS_DIR}/{phase}.json"


def _file_metadata(work_dir: str | Path, relpath: str) -> dict[str, Any] | None:
    """读取真实产物的有限元数据；不存在的路径不进入 packet。"""
    root = Path(work_dir).resolve()
    path = (root / relpath).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    if not path.is_file():
        return None
    return {
        "path": path.relative_to(root).as_posix(),
        "kind": _artifact_kind(path.name),
        "bytes": path.stat().st_size,
        "exists": True,
    }


def _artifact_kind(filename: str) -> str:
    """按扩展名返回交接产物类型。"""
    lower = filename.lower()
    for suffix, kind in (
        (".png", "image"),
        (".jpg", "image"),
        (".jpeg", "image"),
        (".csv", "csv"),
        (".npy", "npy"),
        (".json", "json"),
        (".xlsx", "spreadsheet"),
        (".xls", "spreadsheet"),
        (".pdf", "pdf"),
    ):
        if lower.endswith(suffix):
            return kind
    return "file"


def _fact_lines(text: str, constraints: Sequence[Any] | None) -> list[str]:
    """从 stdout/收工摘要中保留少量可引用事实行。"""
    safe_text = redact_execution_constraints(text, constraints)
    lines: list[str] = []
    for line in safe_text.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        lowered = candidate.lower()
        if any(marker in lowered for marker in _FACT_MARKERS):
            if candidate not in lines:
                lines.append(candidate)
    return lines[:40]


def _echo_match_key(text: str) -> str:
    """生成忽略空白、标点和大小写差异的题面匹配键。"""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)


def _public_context_units(public_context: str) -> set[str]:
    """提取公共题面的规范化句/边界片段。"""
    units: set[str] = set()
    for line in str(public_context or "").splitlines():
        content = _CONTEXT_FIELD_PREFIX_RE.sub("", line.strip())
        for unit in _ECHO_UNIT_BOUNDARY_RE.split(content):
            key = _echo_match_key(unit)
            if key:
                units.add(key)
    return units


def _redact_public_context_echoes(text: str, public_context: str | None) -> str:
    """删除确定的题面回声，同时保留短、混合或无明确边界的材料。"""
    context_units = _public_context_units(public_context or "")
    if not text or not context_units:
        return text

    kept_units: list[str] = []
    for unit in _ECHO_UNIT_BOUNDARY_RE.split(text):
        key = _echo_match_key(unit)
        if not key:
            kept_units.append(unit)
            continue
        is_long_fragment = len(key) >= RAW_PROBLEM_OVERLAP_THRESHOLD and any(
            key in context for context in context_units
        )
        if not is_long_fragment:
            kept_units.append(unit)
    return "".join(kept_units)


def _redact_phase_text(
    text: str,
    *,
    constraints: Sequence[Any] | None,
    public_context: str | None = None,
    raw_problem: str | None = None,
    inspection_text: str | None = None,
) -> str:
    """按统一顺序清理探查、约束、题面上下文和 raw problem 回声。"""
    cleaned = redact_source_inspection_echoes(text, inspection_text)
    cleaned = redact_execution_constraints(cleaned, constraints)
    cleaned = _redact_public_context_echoes(cleaned, public_context)
    return redact_raw_problem_echoes(cleaned, raw_problem)


def _serialize_packet(packet: dict[str, Any]) -> str:
    """使用保存阶段结果包的紧凑格式序列化 packet。"""
    return json.dumps(packet, ensure_ascii=False, separators=(",", ":"))


def _packet_json_size(packet: dict[str, Any]) -> int:
    """返回 packet 的稳定 JSON 字符数。"""
    return len(_serialize_packet(packet))


def _clip_text(value: Any, max_chars: int) -> str:
    """截断文本并保证结果本身不超过 ``max_chars``。"""
    if max_chars <= 0:
        return ""
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    clipped = truncate_handoff_text(text, max_chars)
    return clipped[:max_chars]


def _normalize_artifacts(value: Any) -> list[dict[str, Any]]:
    """按图片优先、组内路径排序产物元数据，不为输入路径补造文件。"""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []

    by_path: dict[str, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            continue
        path = item.get("path")
        if not isinstance(path, str) or not path or path in by_path:
            continue
        normalized: dict[str, Any] = {"path": path}
        kind = item.get("kind")
        if isinstance(kind, str) and kind:
            normalized["kind"] = kind
        exists = item.get("exists")
        if isinstance(exists, bool):
            normalized["exists"] = exists
        by_path[path] = normalized
    return sorted(
        by_path.values(),
        key=lambda item: (
            item.get("kind") != "image",
            str(item["path"]),
        ),
    )


def _normalize_images(value: Any, artifacts: Sequence[Mapping[str, Any]]) -> list[str]:
    """仅保留同时出现在 image artifact 中的真实图片路径。"""
    image_paths = {
        str(item["path"])
        for item in artifacts
        if item.get("kind") == "image" and item.get("path")
    }
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return sorted(
        {path for path in value if isinstance(path, str) and path in image_paths}
    )


def _text_items(value: Any, *, max_items: int, item_chars: int) -> list[str]:
    """把列表型交接内容压缩为有限数量的文本项。"""
    if (
        max_items <= 0
        or not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
    ):
        return []
    items: list[str] = []
    for item in value:
        text = _clip_text(item, item_chars)
        if text and text not in items:
            items.append(text)
        if len(items) >= max_items:
            break
    return items


def _truncation_metadata(
    *,
    budget: int,
    artifact_total: int,
    artifact_included: int,
    image_total: int,
    image_included: int,
    compact: bool = False,
) -> dict[str, int]:
    """生成可审计的产物截断计数。"""
    if compact:
        return {
            "budget": budget,
            "omitted_artifact_count": artifact_total - artifact_included,
            "omitted_image_count": image_total - image_included,
        }
    return {
        "budget": budget,
        "original_artifact_count": artifact_total,
        "included_artifact_count": artifact_included,
        "omitted_artifact_count": artifact_total - artifact_included,
        "original_image_count": image_total,
        "included_image_count": image_included,
        "omitted_image_count": image_total - image_included,
    }


def _packet_candidate(
    packet: Mapping[str, Any],
    *,
    artifacts: Sequence[Mapping[str, Any]],
    images: Sequence[str],
    budget: int,
    profile: Mapping[str, Any],
    artifact_total: int,
    image_total: int,
) -> dict[str, Any]:
    """按压缩 profile 生成一个候选 packet。"""
    candidate: dict[str, Any] = {
        "version": packet.get("version", PHASE_RESULT_VERSION),
        "task_id": _clip_text(packet.get("task_id", ""), 512),
        "phase": _clip_text(packet.get("phase", ""), profile["phase_chars"]),
        "status": _clip_text(packet.get("status", "failed"), profile["status_chars"]),
        "evidence_status": _clip_text(
            packet.get("evidence_status", "missing"),
            profile["evidence_chars"],
        ),
    }
    if profile["keep_metadata"]:
        for key, max_chars in (
            ("started_at", 128),
            ("ended_at", 128),
            ("model_reference", 512),
        ):
            if key in packet:
                candidate[key] = _clip_text(packet.get(key), max_chars)

    if profile["error_chars"] > 0:
        candidate["error_summary"] = _clip_text(
            packet.get("error_summary", "unavailable"),
            profile["error_chars"],
        )
    if profile["summary_chars"] > 0:
        candidate["summary"] = _clip_text(
            packet.get("summary", "unavailable"),
            profile["summary_chars"],
        )
    if profile["stdout_chars"] > 0:
        candidate["stdout"] = _clip_text(
            packet.get("stdout", "unavailable"),
            profile["stdout_chars"],
        )

    candidate["metrics"] = _text_items(
        packet.get("metrics"),
        max_items=profile["metric_items"],
        item_chars=profile["item_chars"],
    )
    limitations = _text_items(
        packet.get("limitations"),
        max_items=profile["limitation_items"],
        item_chars=profile["item_chars"],
    )
    candidate["limitations"] = limitations or ["unavailable"]
    candidate["artifacts"] = [dict(item) for item in artifacts]
    candidate["images"] = list(images)
    candidate["truncated"] = True
    candidate["truncation"] = _truncation_metadata(
        budget=budget,
        artifact_total=artifact_total,
        artifact_included=len(artifacts),
        image_total=image_total,
        image_included=len(images),
        compact=profile.get("compact_truncation", False),
    )
    return candidate


def _bound_packet(packet: dict[str, Any], budget: int) -> dict[str, Any]:
    """按字符预算压缩 packet，保持状态、路径、限制和截断元信息。"""
    if budget <= 0:
        raise ValueError("phase result budget must be positive")
    effective_budget = min(budget, PHASE_RESULT_BUDGET)

    bounded = dict(packet)
    artifacts = _normalize_artifacts(bounded.get("artifacts"))
    images = _normalize_images(bounded.get("images"), artifacts)
    bounded["artifacts"] = artifacts
    bounded["images"] = images
    bounded.setdefault("truncated", False)
    if _packet_json_size(bounded) <= effective_budget:
        return bounded

    artifact_total = len(artifacts)
    image_total = len(images)
    profiles = (
        {
            "keep_metadata": True,
            "error_chars": 400,
            "summary_chars": 600,
            "stdout_chars": 800,
            "metric_items": 8,
            "limitation_items": 12,
            "item_chars": 240,
            "phase_chars": 256,
            "status_chars": 64,
            "evidence_chars": 64,
        },
        {
            "keep_metadata": True,
            "error_chars": 160,
            "summary_chars": 300,
            "stdout_chars": 240,
            "metric_items": 4,
            "limitation_items": 8,
            "item_chars": 120,
            "phase_chars": 128,
            "status_chars": 32,
            "evidence_chars": 32,
        },
        {
            "keep_metadata": True,
            "error_chars": 0,
            "summary_chars": 0,
            "stdout_chars": 0,
            "metric_items": 0,
            "limitation_items": 4,
            "item_chars": 80,
            "phase_chars": 96,
            "status_chars": 24,
            "evidence_chars": 24,
        },
        {
            "keep_metadata": True,
            "error_chars": 0,
            "summary_chars": 0,
            "stdout_chars": 0,
            "metric_items": 0,
            "limitation_items": 1,
            "item_chars": 48,
            "phase_chars": 64,
            "status_chars": 16,
            "evidence_chars": 16,
            "compact_truncation": False,
        },
        {
            "keep_metadata": True,
            "error_chars": 0,
            "summary_chars": 0,
            "stdout_chars": 0,
            "metric_items": 0,
            "limitation_items": 1,
            "item_chars": 48,
            "phase_chars": 64,
            "status_chars": 16,
            "evidence_chars": 16,
            "compact_truncation": True,
        },
    )

    best: dict[str, Any] | None = None
    best_score = (-1, 0)
    for profile_index, profile in enumerate(profiles):
        low = 0
        high = artifact_total
        profile_best: dict[str, Any] | None = None
        while low <= high:
            included = (low + high) // 2
            included_artifacts = artifacts[:included]
            included_paths = {
                str(item["path"]) for item in included_artifacts if item.get("path")
            }
            included_images = [path for path in images if path in included_paths]
            candidate = _packet_candidate(
                bounded,
                artifacts=included_artifacts,
                images=included_images,
                budget=effective_budget,
                profile=profile,
                artifact_total=artifact_total,
                image_total=image_total,
            )
            if _packet_json_size(candidate) <= effective_budget:
                profile_best = candidate
                low = included + 1
            else:
                high = included - 1

        if profile_best is not None:
            included_count = len(profile_best["artifacts"])
            score = (included_count, -profile_index)
            if score > best_score:
                best = profile_best
                best_score = score

    if best is None:
        raise ValueError(
            f"phase result budget {effective_budget} is too small for required fields"
        )
    return best


def build_phase_result(
    work_dir: str | Path,
    *,
    task_id: str,
    phase: str,
    status: str,
    started_at: str,
    ended_at: str,
    interpreter: Any | None = None,
    coder_summary: str = "",
    error: str = "",
    model_reference: str | None = None,
    constraints: Sequence[Any] | None = None,
    public_context: str | None = None,
    raw_problem: str | None = None,
    inspection_text: str | None = None,
    budget: int = PHASE_RESULT_BUDGET,
) -> dict[str, Any]:
    """构造一个只含有限事实和真实产物路径的阶段结果包。

    ``public_context`` 和 ``raw_problem`` 仅用于本函数内部识别题面回声，
    不会写入返回的 packet 或下游交接文本。
    """
    stdout = ""
    artifact_paths: list[str] = []
    if interpreter is not None:
        try:
            stdout = interpreter.get_code_output(phase)
        except (KeyError, AttributeError):
            stdout = ""
        try:
            artifact_paths = list(interpreter.get_section_artifacts(phase))
        except AttributeError:
            artifact_paths = []

    artifacts = [
        metadata
        for path in sorted(set(artifact_paths))
        if (metadata := _file_metadata(work_dir, path)) is not None
    ]
    images = [artifact["path"] for artifact in artifacts if artifact["kind"] == "image"]
    safe_stdout = _redact_phase_text(
        stdout,
        constraints=constraints,
        public_context=public_context,
        raw_problem=raw_problem,
        inspection_text=inspection_text,
    )
    safe_summary = _redact_phase_text(
        coder_summary,
        constraints=constraints,
        public_context=public_context,
        raw_problem=raw_problem,
        inspection_text=inspection_text,
    )
    safe_error = _redact_phase_text(
        error,
        constraints=constraints,
        public_context=public_context,
        raw_problem=raw_problem,
        inspection_text=inspection_text,
    )
    metrics = _fact_lines(f"{safe_stdout}\n{safe_summary}", constraints)
    limitations: list[str] = []
    if safe_error:
        limitations.append(f"execution_error: {truncate_handoff_text(safe_error, 800)}")
    if not safe_stdout.strip():
        limitations.append("interpreter_stdout: unavailable")
    if not metrics:
        limitations.append("metrics: unavailable")
    if not artifacts:
        limitations.append("artifacts: unavailable")

    evidence_status = "available"
    if status != "success" or not safe_stdout.strip() or not metrics:
        evidence_status = "ready_with_limitations" if status == "success" else "missing"

    packet = {
        "version": PHASE_RESULT_VERSION,
        "task_id": task_id,
        "phase": phase,
        "status": status,
        "evidence_status": evidence_status,
        "started_at": started_at,
        "ended_at": ended_at,
        "model_reference": model_reference or f"modeler_solution:{phase}",
        "error_summary": safe_error or "unavailable",
        "summary": truncate_handoff_text(safe_summary, 1800)
        if safe_summary
        else "unavailable",
        "stdout": truncate_handoff_text(safe_stdout, 2400)
        if safe_stdout
        else "unavailable",
        "metrics": metrics,
        "limitations": limitations or ["unavailable"],
        "artifacts": artifacts,
        "images": images,
    }
    return _bound_packet(packet, budget)


def save_phase_result(
    work_dir: str | Path,
    packet: dict[str, Any],
    *,
    budget: int = PHASE_RESULT_BUDGET,
) -> str:
    """保存 bounded phase result packet 并返回相对路径。"""
    relpath = phase_result_relpath(str(packet.get("phase") or "unknown"))
    bounded = _bound_packet(packet, budget)
    path = Path(work_dir) / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_serialize_packet(bounded), encoding="utf-8")
    return relpath


def load_phase_result(work_dir: str | Path, phase: str) -> dict[str, Any] | None:
    """读取阶段结果包；缺失或非法 JSON 返回 None。"""
    path = Path(work_dir) / phase_result_relpath(phase)
    try:
        packet = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return packet if isinstance(packet, dict) else None


def validate_phase_result(
    work_dir: str | Path,
    packet: dict[str, Any] | None,
) -> list[str]:
    """校验 packet 基本字段和其引用的真实文件。"""
    if not isinstance(packet, dict):
        return ["packet unavailable"]
    errors: list[str] = []
    for field in ("task_id", "phase", "status", "evidence_status"):
        if not packet.get(field):
            errors.append(f"missing field: {field}")
    for artifact in packet.get("artifacts") or []:
        if not isinstance(artifact, dict) or not artifact.get("path"):
            errors.append("artifact path unavailable")
            continue
        if _file_metadata(work_dir, str(artifact["path"])) is None:
            errors.append(f"missing artifact: {artifact['path']}")
    return errors


def render_phase_result_for_writer(
    packet: dict[str, Any] | None,
    *,
    budget: int = WRITER_MATERIAL_BUDGET,
    constraints: Sequence[Any] | None = None,
    raw_problem: str | None = None,
) -> str:
    """渲染 Writer 单章材料，优先状态、路径、指标和限制。"""
    if not packet:
        return (
            "阶段结果包：unavailable\n"
            "证据状态：missing。不得写入未经验证的精确结果或图表结论。"
        )

    status = str(packet.get("status") or "unavailable")
    evidence_status = str(packet.get("evidence_status") or "missing")
    phase_failed = status not in {"success", "not_applicable"}
    evidence_missing = evidence_status == "missing"
    lines = [
        f"阶段结果包 phase={packet.get('phase', 'unavailable')}",
        f"状态：{status}",
        f"证据状态：{evidence_status}",
        f"模型引用：{_redact_phase_text(str(packet.get('model_reference') or 'unavailable'), constraints=constraints, raw_problem=raw_problem)}",
    ]
    if phase_failed:
        error_summary = _redact_phase_text(
            str(packet.get("error_summary") or "unavailable"),
            constraints=constraints,
            raw_problem=raw_problem,
        )
        lines.append(f"失败原因：{error_summary}")
        lines.append(
            "写作规则：本阶段失败，不能把该问题写成已完成，"
            "不能引用部分输出作为精确结果，也不得编造精确数字或图表结论。"
        )

    artifacts = packet.get("artifacts") or []
    if artifacts:
        lines.append("真实产物：")
        lines.extend(
            f"- {item.get('path', 'unavailable')} ({item.get('kind', 'file')})"
            for item in artifacts
        )
    else:
        lines.append("真实产物：unavailable")
    summary = _redact_phase_text(
        str(packet.get("summary") or "").strip(),
        constraints=constraints,
        raw_problem=raw_problem,
    )
    if summary and summary != "unavailable" and not phase_failed:
        lines.append("收工摘要：")
        lines.append(truncate_handoff_text(summary, 1800))
    metrics = packet.get("metrics") or []
    lines.append("指标与事实：")
    if metrics and not phase_failed:
        lines.extend(
            f"- {_redact_phase_text(str(metric), constraints=constraints, raw_problem=raw_problem)}"
            for metric in metrics
        )
    else:
        lines.append("- unavailable")
    limitations = packet.get("limitations") or []
    lines.append("限制：")
    lines.extend(
        f"- {_redact_phase_text(str(limitation), constraints=constraints, raw_problem=raw_problem)}"
        for limitation in limitations
    )
    stdout = str(packet.get("stdout") or "").strip()
    if stdout and stdout != "unavailable" and not phase_failed:
        stdout = _redact_phase_text(
            stdout,
            constraints=constraints,
            raw_problem=raw_problem,
        )
        lines.append("有限 stdout：")
        lines.append(stdout)
    if evidence_missing and not phase_failed:
        lines.append("写作规则：证据缺失，只能明确标记限制，不得编造精确数字。")
    return bound_writer_material("\n".join(lines), budget)
