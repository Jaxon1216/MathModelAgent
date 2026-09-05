"""M2 ResultPackage 的安全落盘与 Writer 材料渲染。"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Iterable

from app.domain.result_package import (
    ArtifactKind,
    FailureEvidence,
    FailureKind,
    ResultArtifact,
    ResultMetric,
    ResultPackage,
    ResultPackageStatus,
)

RESULT_PACKAGES_DIRECTORY = "result_packages"
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}


class ResultPackagePersistenceError(RuntimeError):
    """ResultPackage 文件无法安全、完整落盘。"""


def persist_result_package(
    work_dir: str | Path,
    *,
    task_id: str,
    phase: str,
    status: ResultPackageStatus,
    executed_code: Iterable[str],
    stdout: str,
    artifact_paths: Iterable[str],
    metric_statements: Iterable[str],
    limitations: Iterable[str],
    summary: str | None,
    failure_kind: FailureKind | None = None,
    failure_message: str | None = None,
) -> ResultPackage:
    """写入代码、stdout 和 JSON package，并返回通过 schema 校验的终态对象。

    Args:
        work_dir: 当前任务工作目录。
        task_id: 任务标识。
        phase: 当前 Coder phase。
        status: 成功或 partial 终态。
        executed_code: 当前 phase 实际执行过的代码片段。
        stdout: 当前 phase 的成功 stdout 快照。
        artifact_paths: 已验证的阶段文件路径。
        metric_statements: 从 stdout 提取的有限指标。
        limitations: 已知限制。
        summary: 模型完成摘要。
        failure_kind: partial 状态的稳定失败分类。
        failure_message: partial 状态的失败信息。

    Returns:
        已写入并读取复验的 ResultPackage。

    Raises:
        ResultPackagePersistenceError: 路径、文件或 JSON 持久化失败。
    """
    root = _resolve_root(work_dir)
    _validate_phase(phase)
    _create_package_directory(root)
    package_prefix = f"{RESULT_PACKAGES_DIRECTORY}/{phase}"
    code_path = f"{package_prefix}.py"
    stdout_path = f"{package_prefix}.stdout.txt"
    package_path = f"{package_prefix}.json"

    code_text = _render_code_snapshot(executed_code)
    _write_text_atomic(root, code_path, code_text)
    _write_text_atomic(root, stdout_path, stdout)

    code_ref = _file_reference(root, code_path, "code")
    stdout_ref = _file_reference(root, stdout_path, "stdout")
    artifacts = tuple(
        reference
        for path in dict.fromkeys(artifact_paths)
        if (reference := _file_reference_or_none(root, path)) is not None
    )
    figures = tuple(
        artifact for artifact in artifacts if artifact.kind == "image"
    )
    metrics = _build_metrics(stdout, stdout_path, metric_statements)
    failure = _build_failure(status, failure_kind, failure_message)
    package = ResultPackage(
        task_id=task_id,
        phase=phase,
        status=status,
        created_at=datetime.now(UTC).isoformat(),
        package_path=package_path,
        code=code_ref,
        stdout=stdout_ref,
        artifacts=artifacts,
        figures=figures,
        metrics=metrics,
        limitations=tuple(_bounded_unique(limitations, max_items=20, max_chars=300)),
        failure=failure,
        summary=(summary or "").strip()[:2000],
    )
    _write_text_atomic(
        root,
        package_path,
        json.dumps(package.model_dump(mode="json"), ensure_ascii=False, indent=2)
        + "\n",
    )
    return load_result_package(root, package_path)


def load_result_package(
    work_dir: str | Path,
    package_path: str,
) -> ResultPackage:
    """读取、校验并验证 package 自身声明路径。"""
    root = _resolve_root(work_dir)
    path = _safe_path(root, package_path, must_exist=True)
    try:
        package = ResultPackage.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ResultPackagePersistenceError(
            f"ResultPackage 无法读取或校验: {package_path}"
        ) from exc
    if package.package_path != package_path:
        raise ResultPackagePersistenceError(
            "ResultPackage 声明路径与读取路径不一致: "
            f"declared={package.package_path}, actual={package_path}"
        )
    return package


def render_result_package_for_writer(package: ResultPackage) -> str:
    """将终态 package 渲染为 Writer 可消费的有限、可追溯材料。"""
    lines = [
        f"阶段：{package.phase}",
        f"状态：{package.status}",
        f"代码快照：{package.code.path}",
        f"执行输出：{package.stdout.path}",
    ]
    if package.metrics:
        lines.append("已验证指标：")
        lines.extend(
            (
                f"- {metric.statement}"
                f"（来源 {metric.source_path}:L{metric.source_line}）"
            )
            for metric in package.metrics
        )
    if package.figures:
        lines.append("可用图表：")
        lines.extend(f"- {figure.path}" for figure in package.figures)
    other_artifacts = [
        artifact.path
        for artifact in package.artifacts
        if artifact.kind != "image"
    ]
    if other_artifacts:
        lines.append("其他可用产物：")
        lines.extend(f"- {path}" for path in other_artifacts)
    if package.limitations:
        lines.append("限制：" + "；".join(package.limitations))
    if package.failure is not None:
        lines.append(f"partial 原因：{package.failure.kind}")
        lines.append(f"失败证据：{package.failure.message}")
        lines.append("不得将本阶段描述为完全收敛，只能使用上述已验证材料。")
    if package.summary:
        lines.append(f"阶段摘要：{package.summary}")
    return "\n".join(lines)


def _resolve_root(work_dir: str | Path) -> Path:
    """解析并验证任务工作目录。"""
    root = Path(work_dir)
    if not root.is_dir():
        raise ResultPackagePersistenceError(f"任务工作目录不存在: {root}")
    try:
        return root.resolve(strict=True)
    except OSError as exc:
        raise ResultPackagePersistenceError(f"任务工作目录无法解析: {root}") from exc


def _validate_phase(phase: str) -> None:
    """限制 package 文件名可安全派生。"""
    if not phase or not phase.replace("_", "").replace("-", "").isalnum():
        raise ResultPackagePersistenceError(f"非法 ResultPackage phase: {phase}")


def _create_package_directory(root: Path) -> Path:
    """创建 package 目录，并拒绝符号链接写入。"""
    directory = root / RESULT_PACKAGES_DIRECTORY
    try:
        directory.mkdir(exist_ok=True)
    except OSError as exc:
        raise ResultPackagePersistenceError("无法创建 ResultPackage 目录") from exc
    if directory.is_symlink() or not directory.is_dir():
        raise ResultPackagePersistenceError("ResultPackage 目录必须是普通目录")
    return directory


def _safe_path(root: Path, relative_path: str, *, must_exist: bool = False) -> Path:
    """解析受控相对路径，不允许逃逸或符号链接组件。"""
    try:
        candidate_path = PurePosixPath(relative_path)
    except TypeError as exc:
        raise ResultPackagePersistenceError("ResultPackage 路径必须是字符串") from exc
    if (
        not relative_path
        or "\\" in relative_path
        or candidate_path.is_absolute()
        or ".." in candidate_path.parts
        or "." in candidate_path.parts
    ):
        raise ResultPackagePersistenceError(
            f"非法 ResultPackage 相对路径: {relative_path}"
        )
    candidate = root.joinpath(*candidate_path.parts)
    current = root
    for part in candidate_path.parts:
        current = current / part
        if current.is_symlink():
            raise ResultPackagePersistenceError(
                f"ResultPackage 路径不能包含符号链接: {relative_path}"
            )
    try:
        candidate.resolve(strict=False).relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ResultPackagePersistenceError(
            f"ResultPackage 路径逃逸工作目录: {relative_path}"
        ) from exc
    if must_exist and (not candidate.is_file() or candidate.is_symlink()):
        raise ResultPackagePersistenceError(
            f"ResultPackage 文件不存在或不安全: {relative_path}"
        )
    return candidate


def _write_text_atomic(root: Path, relative_path: str, content: str) -> None:
    """原子写入一个 UTF-8 ResultPackage 文件。"""
    target = _safe_path(root, relative_path)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if target.is_symlink():
            raise ResultPackagePersistenceError(
                f"ResultPackage 目标不能是符号链接: {relative_path}"
            )
        os.replace(temporary, target)
    except OSError as exc:
        raise ResultPackagePersistenceError(
            f"ResultPackage 写入失败: {relative_path}"
        ) from exc
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink(missing_ok=True)


def _render_code_snapshot(executed_code: Iterable[str]) -> str:
    """将本 phase 实际执行的代码保存为稳定快照。"""
    blocks = [
        f"# execute_code #{index}\n{code.rstrip()}"
        for index, code in enumerate(executed_code, start=1)
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _file_reference(
    root: Path,
    relative_path: str,
    kind: ArtifactKind,
) -> ResultArtifact:
    """构造存在文件的带 fingerprint 引用。"""
    path = _safe_path(root, relative_path, must_exist=True)
    try:
        return ResultArtifact(
            path=relative_path,
            kind=kind,
            bytes=path.stat().st_size,
            sha256=_sha256(path),
        )
    except OSError as exc:
        raise ResultPackagePersistenceError(
            f"ResultPackage 文件无法读取: {relative_path}"
        ) from exc


def _file_reference_or_none(root: Path, relative_path: str) -> ResultArtifact | None:
    """跳过不安全或不可读的 Coder 候选产物。"""
    try:
        path = _safe_path(root, relative_path, must_exist=True)
        return ResultArtifact(
            path=relative_path,
            kind=_artifact_kind(relative_path),
            bytes=path.stat().st_size,
            sha256=_sha256(path),
        )
    except (OSError, ResultPackagePersistenceError):
        return None


def _artifact_kind(relative_path: str) -> ArtifactKind:
    """按文件后缀确定 Writer 可用的产物类型。"""
    suffix = Path(relative_path).suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    if suffix == ".csv":
        return "csv"
    if suffix in {".xlsx", ".xls"}:
        return "spreadsheet"
    if suffix == ".npy":
        return "array"
    return "file"


def _sha256(path: Path) -> str:
    """计算文件 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_metrics(
    stdout: str,
    stdout_path: str,
    metric_statements: Iterable[str],
) -> tuple[ResultMetric, ...]:
    """为每条有限 stdout 指标提供稳定文件行定位。"""
    normalized_lines = [" ".join(line.split()) for line in stdout.splitlines()]
    metrics: list[ResultMetric] = []
    for statement in metric_statements:
        normalized = " ".join(statement.split())
        if not normalized or normalized in {metric.statement for metric in metrics}:
            continue
        try:
            source_line = normalized_lines.index(normalized) + 1
        except ValueError:
            continue
        metrics.append(
            ResultMetric(
                statement=normalized,
                source_path=stdout_path,
                source_line=source_line,
            )
        )
    return tuple(metrics)


def _build_failure(
    status: ResultPackageStatus,
    kind: FailureKind | None,
    message: str | None,
) -> FailureEvidence | None:
    """根据 Coder 终态建立 partial 的明确失败证据。"""
    if status == "success":
        return None
    return FailureEvidence(
        kind=kind or "execution_error",
        message=(message or "阶段未完全收敛。").strip()[:2000],
    )


def _bounded_unique(
    values: Iterable[str],
    *,
    max_items: int,
    max_chars: int,
) -> list[str]:
    """稳定去重并限制 package 中的自由文本。"""
    result: list[str] = []
    for value in values:
        normalized = " ".join(str(value).split())[:max_chars]
        if normalized and normalized not in result:
            result.append(normalized)
        if len(result) >= max_items:
            break
    return result
