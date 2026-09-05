"""Coder 的渐进式技能注册表。

L1 仅读取并索引 skill frontmatter；正文与内容版本只会在 L2 ``load_skill()``
显式调用时读取，避免将无关知识提前带入模型上下文。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from pathlib import Path

import yaml


@dataclass(frozen=True)
class SkillDescriptor:
    """L1 skill 描述符。

    Attributes:
        name: 稳定 skill 名称。
        description: 面向工具 schema 的简短能力描述。
        path: 受控 catalog Markdown 路径。
        directory: skill 所在目录。
        version: L2 加载正文后得到的内容版本；L1 扫描阶段不会读取正文计算它。
    """

    name: str
    description: str
    path: Path
    directory: Path
    version: str | None = None


@dataclass(frozen=True)
class LoadedSkill:
    """L2 显式加载的 skill 正文与内容身份。"""

    descriptor: SkillDescriptor
    body: str

    @property
    def name(self) -> str:
        """返回稳定 skill 名称。"""
        return self.descriptor.name

    @property
    def description(self) -> str:
        """返回简短能力描述。"""
        return self.descriptor.description

    @property
    def version(self) -> str:
        """返回与正文绑定的内容版本。"""
        assert self.descriptor.version is not None
        return self.descriptor.version


class SkillRegistry:
    """维护受控 skill 的 L1 索引，并在需要时执行 L2 正文加载。"""

    _VERSION_LENGTH = 16

    def __init__(self, skills_dir: Path | None = None) -> None:
        """初始化注册表并只扫描 skill frontmatter。

        Args:
            skills_dir: skill Markdown 目录，默认使用同级 ``catalog``。
        """
        self.skills_dir = skills_dir or Path(__file__).parent / "catalog"
        self._descriptors: dict[str, SkillDescriptor] = {}
        self._loaded_cache: dict[str, LoadedSkill] = {}
        self._scan_skills()

    def get_descriptions(self) -> str:
        """返回稳定排序的 L1 名称与简短描述。"""
        if not self._descriptors:
            return "（暂无可用技能）"
        return "\n".join(
            f"- {descriptor.name}: {descriptor.description}"
            for descriptor in self.descriptors()
        )

    def descriptors(self) -> tuple[SkillDescriptor, ...]:
        """返回所有 L1 描述符，不读取 skill 正文。"""
        return tuple(
            self._descriptors[name] for name in sorted(self._descriptors)
        )

    def get_descriptor(self, name: str) -> SkillDescriptor | None:
        """按名称获取 L1 描述符。"""
        return self._descriptors.get(name)

    def list_skills(self) -> list[str]:
        """列出稳定排序的已注册 skill 名称。"""
        return list(sorted(self._descriptors))

    def load_skill(self, name: str) -> LoadedSkill | None:
        """显式读取、验证并缓存一个 skill 正文。

        Args:
            name: L1 已注册的 skill 名称。

        Returns:
            与正文内容版本绑定的 L2 skill；名称不存在、文件不可读或元数据不一致时返回
            ``None``。
        """
        if name in self._loaded_cache:
            return self._loaded_cache[name]

        descriptor = self._descriptors.get(name)
        if descriptor is None:
            return None

        try:
            content = descriptor.path.read_bytes()
        except OSError:
            return None

        parsed = _parse_skill_document(content)
        if parsed is None:
            return None
        metadata, body = parsed
        if (
            metadata.get("name") != descriptor.name
            or metadata.get("description") != descriptor.description
        ):
            return None

        version = hashlib.sha256(content).hexdigest()[: self._VERSION_LENGTH]
        loaded = LoadedSkill(
            descriptor=replace(descriptor, version=version),
            body=body.strip(),
        )
        self._loaded_cache[name] = loaded
        return loaded

    def _scan_skills(self) -> None:
        """索引目录中的合法 frontmatter，不读取任一正文。"""
        if not self.skills_dir.is_dir():
            return

        for skill_file in sorted(self.skills_dir.glob("*.md")):
            metadata = _read_frontmatter(skill_file)
            if metadata is None:
                continue
            name = metadata.get("name")
            description = metadata.get("description")
            if (
                not isinstance(name, str)
                or not name.strip()
                or not isinstance(description, str)
                or not description.strip()
                or name in self._descriptors
            ):
                continue
            self._descriptors[name] = SkillDescriptor(
                name=name,
                description=description,
                path=skill_file,
                directory=skill_file.parent,
            )


def _read_frontmatter(path: Path) -> dict[str, object] | None:
    """从文件开头逐行读取 YAML frontmatter，遇到 closing delimiter 立即停止。"""
    try:
        with path.open("rb") as stream:
            if stream.readline().strip() != b"---":
                return None

            lines: list[bytes] = []
            for line in stream:
                if line.strip() == b"---":
                    break
                lines.append(line)
            else:
                return None
    except OSError:
        return None

    try:
        metadata = yaml.safe_load(b"".join(lines).decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError):
        return None
    return metadata if isinstance(metadata, dict) else None


def _parse_skill_document(content: bytes) -> tuple[dict[str, object], str] | None:
    """解析 L2 完整 skill 内容，并返回元数据与正文。"""
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError:
        return None

    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", decoded, re.DOTALL)
    if match is None:
        return None
    try:
        metadata = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(metadata, dict):
        return None
    return metadata, match.group(2)
