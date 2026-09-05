"""M1.5 领域产物的安全路径解析与原子 JSON 存储。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import TypeVar

from pydantic import TypeAdapter, ValidationError

from app.domain.m15 import (
    M15_ARTIFACT_TYPES,
    M15ArtifactPath,
    VersionedArtifact,
)

ArtifactT = TypeVar("ArtifactT", bound=VersionedArtifact)
_M15_PATH_ADAPTER = TypeAdapter(M15ArtifactPath)


class ArtifactStorageError(RuntimeError):
    """M1.5 产物无法被安全、完整地持久化。"""


def write_utf8_atomic(
    work_dir: str | Path,
    relative_path: str,
    content: str,
) -> Path:
    """在工作目录内以 UTF-8 原子写入普通文本产物。

    Args:
        work_dir: 当前任务工作目录。
        relative_path: 受控 POSIX 相对路径。
        content: 要写入的完整文本。

    Returns:
        原子替换完成后的绝对路径。

    Raises:
        ArtifactStorageError: 路径不安全或写入失败。
    """
    root = Path(work_dir).resolve(strict=True)
    target = resolve_work_dir_path(root, relative_path)
    _ensure_safe_parent_directories(root, target.parent)
    _reject_unsafe_target(target, relative_path)

    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(
            descriptor,
            mode="w",
            encoding="utf-8",
            newline="\n",
        ) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

        _reject_symlink_components(root, target)
        _reject_unsafe_target(target, relative_path)
        os.replace(temporary_path, target)
        temporary_path = None
        _fsync_directory(target.parent)
    except (OSError, UnicodeError) as exc:
        raise ArtifactStorageError(
            f"UTF-8 原子写入失败: {relative_path}: {exc}"
        ) from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return target


def resolve_work_dir_path(
    work_dir: str | Path,
    relative_path: str,
    *,
    must_exist: bool = False,
) -> Path:
    """将受控相对路径解析到 work_dir 内，并拒绝符号链接。

    Args:
        work_dir: 当前任务工作目录。
        relative_path: 已使用 POSIX 分隔符的相对路径。
        must_exist: 是否要求目标已经存在。

    Returns:
        位于真实 work_dir 内的绝对路径。

    Raises:
        ArtifactStorageError: 工作目录、相对路径或路径组件不安全。
    """
    root_input = Path(work_dir)
    if not root_input.is_dir():
        raise ArtifactStorageError(f"工作目录不存在: {root_input}")

    try:
        normalized = _validate_general_relative_path(relative_path)
    except ValueError as exc:
        raise ArtifactStorageError(str(exc)) from exc

    root = root_input.resolve(strict=True)
    candidate = root.joinpath(*normalized.split("/"))
    _reject_symlink_components(root, candidate)

    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ArtifactStorageError(f"路径逃逸工作目录: {relative_path}") from exc

    if must_exist and not candidate.exists():
        raise ArtifactStorageError(f"产物不存在: {relative_path}")
    return candidate


class M15ArtifactStore:
    """在单个任务工作目录内原子读写 M1.5 JSON 产物。"""

    def __init__(self, work_dir: str | Path) -> None:
        """绑定已经存在的任务工作目录。

        Args:
            work_dir: 当前任务工作目录。

        Raises:
            ArtifactStorageError: 工作目录不存在。
        """
        root = Path(work_dir)
        if not root.is_dir():
            raise ArtifactStorageError(f"工作目录不存在: {root}")
        self._work_dir = root.resolve(strict=True)

    def write_json(self, artifact: VersionedArtifact) -> Path:
        """以 UTF-8 JSON 原子写入产物，并使用同一 schema 读取复验。

        Args:
            artifact: 已通过领域 schema 构造的 M1.5 产物。

        Returns:
            写入产物的绝对路径。

        Raises:
            ArtifactStorageError: schema、路径、写入或读取复验失败。
        """
        if not isinstance(artifact, M15_ARTIFACT_TYPES):
            raise ArtifactStorageError("只能写入已注册的 M1.5 具体领域产物")

        artifact_type = type(artifact)
        try:
            validated = artifact_type.model_validate_json(artifact.model_dump_json())
            relative_path = _M15_PATH_ADAPTER.validate_python(validated.artifact_path)
        except (ValidationError, ValueError) as exc:
            raise ArtifactStorageError(f"产物 schema 复验失败: {exc}") from exc

        target = resolve_work_dir_path(self._work_dir, relative_path)
        _ensure_safe_parent_directories(self._work_dir, target.parent)
        _reject_unsafe_target(target, relative_path)

        payload = json.dumps(
            validated.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        write_utf8_atomic(self._work_dir, relative_path, f"{payload}\n")

        loaded = self.read_json(
            relative_path,
            artifact_type,
            require_validated=False,
        )
        if loaded != validated:
            raise ArtifactStorageError(f"产物读取复验不一致: {relative_path}")
        return target

    def read_json(
        self,
        relative_path: str,
        artifact_type: type[ArtifactT],
        *,
        require_validated: bool = True,
    ) -> ArtifactT:
        """读取 JSON 并用调用方指定的同一领域 schema 复验。

        Args:
            relative_path: 相对于 work_dir 的 M1.5 JSON 路径。
            artifact_type: 预期的具体领域产物类型。
            require_validated: 是否拒绝非 validated 产物作为下游输入。

        Returns:
            经过 schema 和路径一致性校验的领域产物。

        Raises:
            ArtifactStorageError: 路径、UTF-8、JSON、schema 或状态无效。
        """
        if not issubclass(artifact_type, M15_ARTIFACT_TYPES):
            raise ArtifactStorageError("只能读取已注册的 M1.5 具体领域产物")

        try:
            normalized = _M15_PATH_ADAPTER.validate_python(relative_path)
        except ValidationError as exc:
            raise ArtifactStorageError(f"非法 M1.5 产物路径: {relative_path}") from exc

        path = resolve_work_dir_path(
            self._work_dir,
            normalized,
            must_exist=True,
        )
        _reject_unsafe_target(path, normalized)
        if not path.is_file():
            raise ArtifactStorageError(f"产物不是普通文件: {normalized}")

        try:
            raw_json = path.read_text(encoding="utf-8")
            artifact = artifact_type.model_validate_json(raw_json)
        except (OSError, UnicodeError, ValidationError) as exc:
            raise ArtifactStorageError(
                f"产物读取复验失败: {normalized}: {exc}"
            ) from exc

        if artifact.artifact_path != normalized:
            raise ArtifactStorageError(
                "产物内声明路径与读取路径不一致: "
                f"declared={artifact.artifact_path}, actual={normalized}"
            )
        if require_validated and artifact.validation_status != "validated":
            raise ArtifactStorageError(
                f"产物未通过校验，不能作为下游输入: {artifact.artifact_id}"
            )
        return artifact


def _validate_general_relative_path(relative_path: str) -> str:
    """存储层的独立防线，不依赖调用方已执行 Pydantic 校验。"""
    if not isinstance(relative_path, str):
        raise ValueError("相对路径必须为字符串")
    if relative_path != relative_path.strip() or not relative_path:
        raise ValueError("相对路径不能为空或包含首尾空白")
    if "\\" in relative_path or "\x00" in relative_path:
        raise ValueError("相对路径必须使用 POSIX 分隔符")
    parts = relative_path.split("/")
    if relative_path.startswith("/") or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"非法相对路径: {relative_path}")
    return relative_path


def _reject_symlink_components(root: Path, candidate: Path) -> None:
    """拒绝 root 以下任何已经存在的符号链接组件。"""
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ArtifactStorageError(f"路径不在工作目录内: {candidate}") from exc

    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ArtifactStorageError(f"路径不能包含符号链接: {current}")


def _ensure_safe_parent_directories(root: Path, parent: Path) -> None:
    """逐层创建目录，并在每一步拒绝符号链接和非目录节点。"""
    try:
        relative = parent.relative_to(root)
    except ValueError as exc:
        raise ArtifactStorageError(f"产物父目录不在工作目录内: {parent}") from exc

    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ArtifactStorageError(f"产物父目录不能是符号链接: {current}")
        try:
            current.mkdir()
        except FileExistsError:
            if not current.is_dir():
                raise ArtifactStorageError(f"产物父路径不是目录: {current}")


def _reject_unsafe_target(target: Path, relative_path: str) -> None:
    """目标只能不存在或是普通非符号链接文件。"""
    if target.is_symlink():
        raise ArtifactStorageError(f"拒绝覆盖符号链接: {relative_path}")
    if target.exists() and not target.is_file():
        raise ArtifactStorageError(f"产物目标不是普通文件: {relative_path}")


def _fsync_directory(directory: Path) -> None:
    """尽力将原子替换后的目录项同步到磁盘。"""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
