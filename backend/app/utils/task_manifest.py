"""任务产物 manifest：在不移动现有文件的前提下建立可验证索引。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.utils.data_contract import is_result_template

MANIFEST_FILENAME = "task_manifest.json"
MANIFEST_VERSION = 1
SUPPORTED_INPUT_SUFFIXES = (".csv", ".xls", ".xlsx")
INDEXED_ARTIFACT_SUFFIXES = (".png", ".jpg", ".jpeg", ".csv", ".npy")
PAPER_FILENAMES = ("res.json", "res.md", "res.docx")
MISSING_OUTPUT_FILENAMES = PAPER_FILENAMES

_ARTIFACT_PRIORITY = {
    "generic": 10,
    "notebook": 20,
    "paper": 30,
    "phase_result": 40,
    "cleaned_csv": 50,
    "contract": 60,
    "inspection": 60,
    "input": 70,
}
_MAX_FAILURE_REASON_CHARS = 2000


def _now() -> str:
    """返回 UTC ISO 时间。"""
    return datetime.now(UTC).isoformat()


def _stable_unique(values: Iterable[str]) -> list[str]:
    """保持首次出现顺序去重。"""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def normalize_manifest_path(path: str | Path) -> str:
    """规范化并校验 manifest 中的相对路径。"""
    value = str(path).replace("\\", "/").strip()
    candidate = Path(value)
    if not value or candidate.is_absolute():
        raise ValueError(f"manifest 路径必须是非空相对路径: {path}")
    normalized = candidate.as_posix()
    if normalized == "." or normalized.startswith("../") or normalized == "..":
        raise ValueError(f"manifest 路径越界: {path}")
    return normalized


def sha256_file(path: str | Path) -> str | None:
    """计算文件 SHA-256；文件不可读时返回 ``None``。"""
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def file_fingerprint(path: str | Path) -> dict[str, Any]:
    """返回文件存在性、字节数和 SHA-256。"""
    file_path = Path(path)
    if not file_path.is_file():
        return {
            "exists": False,
            "bytes": None,
            "sha256": None,
            "status": "missing",
        }
    try:
        size = file_path.stat().st_size
    except OSError:
        return {
            "exists": True,
            "bytes": None,
            "sha256": None,
            "status": "unreadable",
        }
    digest = sha256_file(file_path)
    return {
        "exists": True,
        "bytes": size,
        "sha256": digest,
        "status": "available" if digest is not None else "unreadable",
    }


def artifact_kind(path: str | Path) -> str:
    """按路径返回 manifest 的稳定产物类型。"""
    relpath = normalize_manifest_path(path)
    lower = relpath.lower()
    name = Path(relpath).name.lower()
    if name in {"source_inspection.json"}:
        return "source_inspection"
    if name in {"data_contract.json"}:
        return "data_contract"
    if relpath.startswith("phase_results/") and lower.endswith(".json"):
        return "phase_result"
    if relpath.startswith("cleaned/") and lower.endswith(".csv"):
        return "cleaned_csv"
    if name == "notebook.ipynb" or lower.endswith(".ipynb"):
        return "notebook"
    if name == "res.json":
        return "result_json"
    if name == "res.md":
        return "result_markdown"
    if name == "res.docx":
        return "result_docx"
    if lower.endswith(".png"):
        return "png"
    if lower.endswith((".jpg", ".jpeg")):
        return "image"
    if lower.endswith(".csv"):
        return "csv"
    if lower.endswith(".npy"):
        return "npy"
    if lower.endswith((".xlsx", ".xls")):
        return "spreadsheet"
    return "file"


def _phase_from_packet_path(path: str) -> str:
    """从 phase_results/{phase}.json 读取稳定 phase 名。"""
    return Path(path).stem


def _safe_json_object(path: Path) -> dict[str, Any] | None:
    """读取一个 JSON 对象；非法文件只影响该条元数据。"""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _phase_artifacts_from_manifest(
    manifest: Mapping[str, Any] | None,
) -> dict[str, list[str]]:
    """从上一版 manifest 恢复 phase 引用，避免终态刷新丢失归属。"""
    if not isinstance(manifest, Mapping):
        return {}

    phase_artifacts: dict[str, list[str]] = {}
    for artifact in manifest.get("artifacts") or []:
        if not isinstance(artifact, Mapping):
            continue
        phase = str(artifact.get("phase") or "").strip()
        path = artifact.get("path")
        if (
            not phase
            or phase in {"multiple", "unassigned"}
            or not isinstance(path, str)
            or not path.strip()
        ):
            continue
        phase_artifacts.setdefault(phase, []).append(path)

    for conflict in manifest.get("phase_conflicts") or []:
        if not isinstance(conflict, Mapping) or not isinstance(
            conflict.get("path"), str
        ):
            continue
        for phase in conflict.get("phases") or []:
            phase_name = str(phase).strip()
            if phase_name and phase_name not in {"multiple", "unassigned"}:
                phase_artifacts.setdefault(phase_name, []).append(
                    conflict["path"]
                )
    return phase_artifacts


def _artifact_entry(
    root: Path,
    relpath: str,
    *,
    kind: str,
    phase: str,
    status: str | None = None,
) -> dict[str, Any]:
    """构造单个索引条目。"""
    normalized = normalize_manifest_path(relpath)
    entry = {
        "path": normalized,
        "kind": kind,
        "phase": phase,
        **file_fingerprint(root / Path(normalized)),
    }
    if status is not None:
        entry["status"] = status
    return entry


def _add_candidate(
    candidates: dict[str, tuple[int, dict[str, Any]]],
    *,
    root: Path,
    relpath: str,
    kind: str,
    phase: str,
    category: str,
    status: str | None = None,
) -> None:
    """按路径和优先级加入候选，避免目录扫描产生重复条目。"""
    normalized = normalize_manifest_path(relpath)
    entry = _artifact_entry(
        root,
        normalized,
        kind=kind,
        phase=phase,
        status=status,
    )
    priority = _ARTIFACT_PRIORITY[category]
    previous = candidates.get(normalized)
    if previous is None:
        candidates[normalized] = (priority, entry)
        return

    previous_priority, previous_entry = previous
    replace = priority > previous_priority or (
        not previous_entry["exists"] and entry["exists"]
    )
    if replace:
        candidates[normalized] = (priority, entry)


def _phase_artifact_references(
    root: Path,
    phase_artifacts: Mapping[str, Iterable[str]] | None = None,
) -> tuple[dict[str, list[str]], dict[str, str]]:
    """读取 packet 和外部 phase 索引，返回产物到 phase 的引用和 packet 状态。"""
    references: dict[str, list[str]] = {}
    packet_statuses: dict[str, str] = {}
    packet_dir = root / "phase_results"

    if packet_dir.is_dir():
        for packet_path in sorted(packet_dir.glob("*.json")):
            relpath = packet_path.relative_to(root).as_posix()
            phase = _phase_from_packet_path(relpath)
            packet = _safe_json_object(packet_path)
            if packet is not None:
                packet_statuses[phase] = str(packet.get("status") or "available")
                for artifact in packet.get("artifacts") or []:
                    if not isinstance(artifact, Mapping) or not artifact.get("path"):
                        continue
                    try:
                        artifact_path = normalize_manifest_path(str(artifact["path"]))
                    except ValueError:
                        continue
                    references.setdefault(artifact_path, []).append(phase)

    # packet 可能因 6000 字符预算省略路径；workflow 提供的完整路径只补充
    # phase 引用，不改变 packet 内容或其自身的截断语义。
    for phase, artifact_paths in (phase_artifacts or {}).items():
        phase_name = str(phase).strip()
        if not phase_name or isinstance(artifact_paths, (str, bytes, bytearray)):
            continue
        for artifact_path in artifact_paths:
            try:
                normalized = normalize_manifest_path(str(artifact_path))
            except ValueError:
                continue
            references.setdefault(normalized, []).append(phase_name)
    return references, packet_statuses


def _phase_for_artifact(
    relpath: str,
    references: Mapping[str, list[str]],
) -> tuple[str, list[str]]:
    """根据 packet 引用推断产物 phase，并保留跨 phase 冲突信息。"""
    phases = _stable_unique(references.get(relpath) or [])
    if len(phases) == 1:
        return phases[0], phases
    if len(phases) > 1:
        return "multiple", phases
    return "unassigned", phases


def _discover_artifacts(
    root: Path,
    phase_artifacts: Mapping[str, Iterable[str]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """发现任务目录中的输入、交接和最终产物。"""
    candidates: dict[str, tuple[int, dict[str, Any]]] = {}
    source_files: list[str] = []
    result_templates: list[str] = []

    if root.is_dir():
        for path in sorted(root.iterdir()):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_INPUT_SUFFIXES:
                continue
            relpath = path.relative_to(root).as_posix()
            if is_result_template(path.name):
                result_templates.append(relpath)
                _add_candidate(
                    candidates,
                    root=root,
                    relpath=relpath,
                    kind="result_template",
                    phase="input",
                    category="input",
                    status="skipped",
                )
            else:
                source_files.append(relpath)
                _add_candidate(
                    candidates,
                    root=root,
                    relpath=relpath,
                    kind="source_data",
                    phase="input",
                    category="input",
                )

    source_data_exists = bool(source_files)
    inspection_path = root / "source_inspection.json"
    if source_data_exists or inspection_path.is_file():
        _add_candidate(
            candidates,
            root=root,
            relpath="source_inspection.json",
            kind="source_inspection",
            phase="eda",
            category="inspection",
        )

    contract_path = root / "data_contract.json"
    if source_data_exists or contract_path.is_file():
        _add_candidate(
            candidates,
            root=root,
            relpath="data_contract.json",
            kind="data_contract",
            phase="eda",
            category="contract",
        )

    cleaned_dir = root / "cleaned"
    if cleaned_dir.is_dir():
        for path in sorted(cleaned_dir.rglob("*.csv")):
            if path.is_file():
                _add_candidate(
                    candidates,
                    root=root,
                    relpath=path.relative_to(root).as_posix(),
                    kind="cleaned_csv",
                    phase="eda",
                    category="cleaned_csv",
                )

    references, packet_statuses = _phase_artifact_references(root, phase_artifacts)
    phase_dir = root / "phase_results"
    if phase_dir.is_dir():
        for path in sorted(phase_dir.glob("*.json")):
            if path.is_file():
                relpath = path.relative_to(root).as_posix()
                phase = _phase_from_packet_path(relpath)
                _add_candidate(
                    candidates,
                    root=root,
                    relpath=relpath,
                    kind="phase_result",
                    phase=phase,
                    category="phase_result",
                    status=packet_statuses.get(phase),
                )

    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relpath = path.relative_to(root).as_posix()
            if relpath == MANIFEST_FILENAME or relpath.startswith("phase_results/"):
                continue
            lower = relpath.lower()
            if lower.endswith(".ipynb"):
                _add_candidate(
                    candidates,
                    root=root,
                    relpath=relpath,
                    kind="notebook",
                    phase="shared",
                    category="notebook",
                )
            elif lower.endswith(INDEXED_ARTIFACT_SUFFIXES):
                phase, _ = _phase_for_artifact(relpath, references)
                _add_candidate(
                    candidates,
                    root=root,
                    relpath=relpath,
                    kind=artifact_kind(relpath),
                    phase=phase,
                    category="generic",
                )

    for paper_name in MISSING_OUTPUT_FILENAMES:
        paper_path = root / paper_name
        if paper_path.is_file() or root.is_dir():
            _add_candidate(
                candidates,
                root=root,
                relpath=paper_name,
                kind=artifact_kind(paper_name),
                phase="writer",
                category="paper",
            )

    if root.is_dir():
        for paper_path in sorted(root.glob("res.*")):
            if paper_path.is_file() and paper_path.name not in PAPER_FILENAMES:
                _add_candidate(
                    candidates,
                    root=root,
                    relpath=paper_path.relative_to(root).as_posix(),
                    kind="result",
                    phase="writer",
                    category="paper",
                )

    artifacts = [
        entry for _, entry in sorted(candidates.values(), key=lambda item: item[1]["path"])
    ]
    conflicts = [
        {"path": path, "phases": _stable_unique(phases)}
        for path, phases in sorted(references.items())
        if len(_stable_unique(phases)) > 1
    ]
    data_status = (
        "source_data"
        if source_data_exists
        else "not_applicable"
        if result_templates
        else "no_source_data"
    )
    return artifacts, {
        "data_status": data_status,
        "source_files": source_files,
        "result_templates": result_templates,
        "phase_conflicts": conflicts,
    }


def build_task_manifest(
    work_dir: str | Path,
    *,
    task_id: str,
    status: str = "running",
    current_phase: str | None = None,
    completed_phases: Iterable[str] | None = None,
    failed_phase: str | None = None,
    failure_reason: str | None = None,
    generated_at: str | None = None,
    existing_manifest: Mapping[str, Any] | None = None,
    phase_artifacts: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, Any]:
    """构造幂等任务 manifest。

    Args:
        work_dir: 任务根目录。
        task_id: 任务 ID。
        status: 任务状态，如 ``running``、``completed`` 或
            ``partial_failure``。
        current_phase: 当前正在执行的 phase。
        completed_phases: 本次刷新确认完成的 phase。
        failed_phase: 失败 phase。
        failure_reason: 失败原因摘要。
        generated_at: 可选固定时间，便于纯函数测试和重放。
        existing_manifest: 上一次 manifest，用于保留已完成 phase。
        phase_artifacts: workflow 内存中的完整 phase 到相对产物路径映射；
            仅用于补充 manifest 的 phase 归属，不写入 packet。

    Returns:
        可直接 JSON 序列化的 manifest 字典。
    """
    root = Path(work_dir)
    now = generated_at or _now()
    previous = dict(existing_manifest or {})
    created_at = str(previous.get("created_at") or now)
    completed = _stable_unique(
        [
            *(str(phase) for phase in previous.get("completed_phases") or []),
            *(str(phase) for phase in completed_phases or []),
        ]
    )
    reason = (failure_reason or "").strip()
    if len(reason) > _MAX_FAILURE_REASON_CHARS:
        reason = reason[:_MAX_FAILURE_REASON_CHARS] + "…"

    phase_reference_index = (
        phase_artifacts
        if phase_artifacts is not None
        else _phase_artifacts_from_manifest(previous)
    )
    artifacts, discovery = _discover_artifacts(root, phase_reference_index)
    failure = None
    if failed_phase or reason:
        failure = {
            "phase": failed_phase or current_phase or "unavailable",
            "reason": reason or "unavailable",
        }

    return {
        "version": MANIFEST_VERSION,
        "task_id": task_id,
        "status": status,
        "created_at": created_at,
        "updated_at": now,
        "generated_at": now,
        "current_phase": current_phase,
        "completed_phases": completed,
        "failed_phase": failed_phase,
        "failure_reason": reason or None,
        "failure": failure,
        "data_status": discovery["data_status"],
        "source_files": discovery["source_files"],
        "result_templates": discovery["result_templates"],
        "phase_conflicts": discovery["phase_conflicts"],
        "artifacts": artifacts,
        "artifact_count": len(artifacts),
    }


def save_task_manifest(
    work_dir: str | Path,
    manifest: Mapping[str, Any],
) -> str:
    """原子写入任务根目录的 manifest，并返回相对路径。"""
    root = Path(work_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / MANIFEST_FILENAME
    temporary = root / f".{MANIFEST_FILENAME}.tmp"
    temporary.write_text(
        json.dumps(dict(manifest), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return MANIFEST_FILENAME


def load_task_manifest(work_dir: str | Path) -> dict[str, Any] | None:
    """读取 manifest；缺失或非法 JSON 返回 ``None``。"""
    path = Path(work_dir) / MANIFEST_FILENAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def update_task_manifest(
    work_dir: str | Path,
    *,
    task_id: str,
    status: str = "running",
    current_phase: str | None = None,
    completed_phases: Iterable[str] | None = None,
    failed_phase: str | None = None,
    failure_reason: str | None = None,
    generated_at: str | None = None,
    phase_artifacts: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, Any]:
    """读取旧状态、重建产物索引并幂等保存 manifest。"""
    previous = load_task_manifest(work_dir)
    manifest = build_task_manifest(
        work_dir,
        task_id=task_id,
        status=status,
        current_phase=current_phase,
        completed_phases=completed_phases,
        failed_phase=failed_phase,
        failure_reason=failure_reason,
        generated_at=generated_at,
        existing_manifest=previous,
        phase_artifacts=phase_artifacts,
    )
    save_task_manifest(work_dir, manifest)
    return manifest


# 兼容调用方更短的命名，主实现仍使用带 task 前缀的名称。
build_manifest = build_task_manifest
save_manifest = save_task_manifest
load_manifest = load_task_manifest
update_manifest = update_task_manifest
