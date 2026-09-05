"""SkillRegistry 的 L1/L2 渐进式加载测试。"""

from __future__ import annotations

from pathlib import Path

from app.core.skills.registry import SkillRegistry


def _write_skill(path: Path, *, description: str = "test description", body: str = ""):
    """写入一份最小 skill Markdown。"""
    path.write_text(
        "---\n"
        "name: test-skill\n"
        f"description: {description}\n"
        "---\n"
        f"{body or 'skill body'}\n",
        encoding="utf-8",
    )


def test_registry_scans_only_frontmatter(monkeypatch, tmp_path: Path):
    """L1 扫描不能使用 read_text 或主动读取正文。"""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir / "test.md", body="body that must remain unread")

    def fail_read_text(*args, **kwargs):
        raise AssertionError("L1 扫描不应调用 Path.read_text")

    monkeypatch.setattr(Path, "read_text", fail_read_text)
    registry = SkillRegistry(skills_dir)

    assert registry.get_descriptions() == "- test-skill: test description"
    descriptor = registry.get_descriptor("test-skill")
    assert descriptor is not None
    assert descriptor.version is None


def test_load_skill_reads_and_caches_l2_body(monkeypatch, tmp_path: Path):
    """L2 才读取正文，并将同一版本缓存到 registry 生命周期内。"""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_path = skills_dir / "test.md"
    _write_skill(skill_path, body="detailed instructions")
    calls: list[Path] = []
    original_read_bytes = Path.read_bytes

    def count_read_bytes(path: Path) -> bytes:
        calls.append(path)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", count_read_bytes)
    registry = SkillRegistry(skills_dir)

    first = registry.load_skill("test-skill")
    second = registry.load_skill("test-skill")

    assert first is not None
    assert first is second
    assert first.body == "detailed instructions"
    assert len(first.version) == 16
    assert calls == [skill_path]


def test_loaded_skill_version_changes_with_any_document_content(tmp_path: Path):
    """正文或 frontmatter 变化都必须生成新的正文内容版本。"""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_path = skills_dir / "test.md"

    _write_skill(skill_path, description="first description", body="first body")
    first = SkillRegistry(skills_dir).load_skill("test-skill")
    assert first is not None

    _write_skill(skill_path, description="first description", body="second body")
    body_changed = SkillRegistry(skills_dir).load_skill("test-skill")
    assert body_changed is not None

    _write_skill(skill_path, description="second description", body="second body")
    metadata_changed = SkillRegistry(skills_dir).load_skill("test-skill")
    assert metadata_changed is not None

    assert first.version != body_changed.version
    assert body_changed.version != metadata_changed.version


def test_registry_rejects_invalid_metadata_and_missing_skills(tmp_path: Path):
    """L1 只注册完整 frontmatter，L2 不返回未知或破损 skill。"""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "missing-description.md").write_text(
        "---\nname: incomplete\n---\nbody\n",
        encoding="utf-8",
    )
    (skills_dir / "malformed.md").write_text(
        "name: missing-delimiters\n",
        encoding="utf-8",
    )
    _write_skill(skills_dir / "valid.md")

    registry = SkillRegistry(skills_dir)

    assert registry.list_skills() == ["test-skill"]
    assert registry.load_skill("unknown") is None
    assert registry.load_skill("incomplete") is None
