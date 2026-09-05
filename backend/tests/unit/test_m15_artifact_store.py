"""M1.5 原子 JSON 产物存储测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.data.artifact_store import (
    ArtifactStorageError,
    M15ArtifactStore,
    resolve_work_dir_path,
    write_utf8_atomic,
)
from app.domain.m15 import FileFact, TaskFacts, VersionedArtifact

SOURCE_SHA = "a" * 64


def _task_facts(
    *,
    artifact_path: str = "m15/task_facts.json",
    validation_status: str = "validated",
) -> TaskFacts:
    """构造可落盘的 TaskFacts。"""
    return TaskFacts.model_validate(
        {
            "schema_version": "m1.5",
            "artifact_id": "task-facts:store",
            "source_artifact_ids": (),
            "validation_status": validation_status,
            "artifact_path": artifact_path,
            "task_id": "store-test",
            "problem_text": "分析中文附件。",
            "attachments": (
                FileFact(
                    path="附件.csv",
                    sha256=SOURCE_SHA,
                ),
            ),
            "output_templates": (),
        }
    )


def test_store_atomically_writes_utf8_and_reads_with_same_schema(tmp_path: Path):
    """原子写入保留 UTF-8，且读取后得到完全相同的只读模型。"""
    store = M15ArtifactStore(tmp_path)
    artifact = _task_facts()

    path = store.write_json(artifact)
    restored = store.read_json(artifact.artifact_path, TaskFacts)

    assert restored == artifact
    raw = path.read_bytes()
    assert "分析中文附件。".encode() in raw
    assert json.loads(raw.decode("utf-8"))["schema_version"] == "m1.5"
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))


def test_atomic_write_cleans_temporary_file_on_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """原子替换前取消时传播 CancelledError 并清理同目录临时文件。"""

    def cancel_replace(source, target):
        raise asyncio.CancelledError

    monkeypatch.setattr("app.data.artifact_store.os.replace", cancel_replace)

    with pytest.raises(asyncio.CancelledError):
        write_utf8_atomic(tmp_path, "cleaned/table.csv", "id\n1\n")

    target = tmp_path / "cleaned" / "table.csv"
    assert not target.exists()
    assert not list(target.parent.glob(f".{target.name}.*.tmp"))


def test_store_rejects_failed_artifact_as_downstream_input(tmp_path: Path):
    """失败证据可以落盘，但默认读取不能把它伪装成可消费产物。"""
    store = M15ArtifactStore(tmp_path)
    artifact = _task_facts(validation_status="failed")
    store.write_json(artifact)

    with pytest.raises(ArtifactStorageError, match="不能作为下游输入"):
        store.read_json(artifact.artifact_path, TaskFacts)

    assert (
        store.read_json(
            artifact.artifact_path,
            TaskFacts,
            require_validated=False,
        )
        == artifact
    )


@pytest.mark.parametrize(
    "relative_path",
    [
        "../outside.json",
        "m15/../../outside.json",
        "/tmp/outside.json",
        r"m15\outside.json",
    ],
)
def test_safe_path_resolution_rejects_escape(
    tmp_path: Path,
    relative_path: str,
):
    """存储层独立拒绝路径逃逸，不依赖领域模型先行校验。"""
    with pytest.raises(ArtifactStorageError):
        resolve_work_dir_path(tmp_path, relative_path)


def test_store_rejects_symlinked_parent_directory(tmp_path: Path):
    """m15 父目录是符号链接时不能向工作目录外写入。"""
    outside = tmp_path.parent / f"{tmp_path.name}-outside-parent"
    outside.mkdir()
    (tmp_path / "m15").symlink_to(outside, target_is_directory=True)
    store = M15ArtifactStore(tmp_path)

    with pytest.raises(ArtifactStorageError, match="符号链接"):
        store.write_json(_task_facts())

    assert not (outside / "task_facts.json").exists()


def test_store_rejects_symlink_target_overwrite(tmp_path: Path):
    """既有 JSON 目标是符号链接时不能覆盖或跟随它。"""
    outside = tmp_path.parent / f"{tmp_path.name}-outside-file.json"
    outside.write_text("outside", encoding="utf-8")
    artifact_dir = tmp_path / "m15"
    artifact_dir.mkdir()
    (artifact_dir / "task_facts.json").symlink_to(outside)
    store = M15ArtifactStore(tmp_path)

    with pytest.raises(ArtifactStorageError, match="符号链接"):
        store.write_json(_task_facts())

    assert outside.read_text(encoding="utf-8") == "outside"


def test_store_revalidates_json_schema_on_read(tmp_path: Path):
    """磁盘内容被替换为未知字段或错误类型后必须读取失败。"""
    artifact = _task_facts()
    store = M15ArtifactStore(tmp_path)
    path = store.write_json(artifact)
    payload = artifact.model_dump(mode="json")
    payload["unexpected"] = True
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ArtifactStorageError, match="读取复验失败"):
        store.read_json(artifact.artifact_path, TaskFacts)


def test_store_rejects_declared_path_mismatch(tmp_path: Path):
    """复制到另一相对路径的 JSON 不能凭内部旧路径通过复验。"""
    artifact = _task_facts()
    store = M15ArtifactStore(tmp_path)
    original = store.write_json(artifact)
    copied = tmp_path / "m15" / "copied.json"
    copied.write_bytes(original.read_bytes())

    with pytest.raises(ArtifactStorageError, match="声明路径与读取路径不一致"):
        store.read_json("m15/copied.json", TaskFacts)


def test_store_rejects_shared_metadata_without_concrete_artifact(
    tmp_path: Path,
):
    """共享元数据本身不是可持久化的阶段产物。"""
    metadata = VersionedArtifact(
        schema_version="m1.5",
        artifact_id="metadata:only",
        source_artifact_ids=(),
        validation_status="validated",
        artifact_path="m15/metadata.json",
    )
    store = M15ArtifactStore(tmp_path)

    with pytest.raises(ArtifactStorageError, match="具体领域产物"):
        store.write_json(metadata)
