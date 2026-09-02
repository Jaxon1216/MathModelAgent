"""Skills 加载器：为 CoderAgent 提供按需加载的领域知识。

实现渐进式披露机制：
- Layer 1: Metadata（启动时加载，~100 tokens/skill）
- Layer 2: SKILL body（按需加载，~1000+ tokens）

skill 文件格式：
    ---
    name: visualization
    description: 学术论文级 matplotlib 绘图规范。绘图前必须加载。
    ---
    # 可视化规范
    ...
"""

from pathlib import Path
from typing import Dict, List
import re
import yaml
from dataclasses import dataclass


@dataclass
class Skill:
    """技能数据类。

    Attributes:
        name: 技能名称（与 frontmatter 中 name 字段一致）。
        description: 一句话描述，用于工具列表展示。
        body: 完整技能内容（懒加载）。
        path: SKILL.md 文件路径。
        dir: 技能所在目录。
    """

    name: str
    description: str
    body: str
    path: Path
    dir: Path


class SkillLoader:
    """CoderAgent 技能加载器。

    特性：
    - 启动时仅扫描并缓存 frontmatter 元数据（轻量）
    - 调用 get_skill() 时按需读取完整 body（懒加载）
    - 已加载的 skill 结果缓存，同一进程内不重复 IO

    使用示例：
        >>> loader = SkillLoader()
        >>> # 获取所有技能名称和描述（用于工具描述字段）
        >>> print(loader.get_descriptions())
        >>> # 按需加载完整技能 body
        >>> skill = loader.get_skill("visualization")
        >>> print(skill.body)
    """

    def __init__(self, skills_dir: Path | None = None) -> None:
        """初始化技能加载器并扫描 catalog 目录。

        Args:
            skills_dir: 技能目录路径，默认为本模块同级 catalog/ 目录。
        """
        if skills_dir is None:
            skills_dir = Path(__file__).parent / "catalog"
        self.skills_dir = Path(skills_dir)

        # 完整技能缓存（按需填充）
        self._skills_cache: Dict[str, Skill] = {}
        # 轻量元数据缓存（启动时全量加载）
        self._metadata_cache: Dict[str, Dict] = {}

        self._scan_skills()

    # ---- 公共方法 ----

    def get_descriptions(self) -> str:
        """返回所有技能的名称和描述，用于嵌入工具 schema description 字段。

        Returns:
            格式化的技能列表字符串，例如：
            - eda: 数据驱动题 EDA 流程...
            - visualization: 学术论文级绘图规范...
        """
        if not self._metadata_cache:
            return "（暂无可用技能）"
        return "\n".join(
            f"- {name}: {meta['description']}"
            for name, meta in self._metadata_cache.items()
        )

    def get_description(self, name: str) -> str | None:
        """返回单个技能的 L1 描述，不触发 body 读取。"""
        metadata = self._metadata_cache.get(name)
        if metadata is None:
            return None
        description = metadata.get("description")
        return str(description) if description is not None else ""

    def get_skill(self, name: str) -> Skill | None:
        """按需加载完整技能。

        Args:
            name: 技能名称（与 SKILL.md frontmatter 中 name 字段一致）。

        Returns:
            Skill 对象，若技能不存在则返回 None。
        """
        if name in self._skills_cache:
            return self._skills_cache[name]

        if name not in self._metadata_cache:
            return None

        meta = self._metadata_cache[name]
        try:
            content = meta["path"].read_text(encoding="utf-8")
        except OSError:
            return None

        # 提取 frontmatter 与 body
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", content, re.DOTALL)
        if not match:
            return None

        _, body = match.groups()
        skill = Skill(
            name=name,
            description=meta["description"],
            body=body.strip(),
            path=meta["path"],
            dir=meta["dir"],
        )
        self._skills_cache[name] = skill
        return skill

    def list_skills(self) -> List[str]:
        """列出所有可用技能名称。

        Returns:
            技能名称列表。
        """
        return list(self._metadata_cache.keys())

    # ---- 私有方法 ----

    def _scan_skills(self) -> None:
        """扫描 skills_dir，加载所有 SKILL.md 的 frontmatter 元数据。"""
        if not self.skills_dir.exists():
            return

        for skill_file in sorted(self.skills_dir.glob("*.md")):
            meta = self._parse_frontmatter_only(skill_file)
            if meta is None:
                continue
            name = meta.get("name", skill_file.stem)
            self._metadata_cache[name] = {
                "name": name,
                "description": meta.get("description", ""),
                "path": skill_file,
                "dir": skill_file.parent,
            }

    def _parse_frontmatter_only(self, path: Path) -> Dict | None:
        """仅解析 YAML frontmatter，不读取 body（性能优化）。

        Args:
            path: SKILL.md 文件路径。

        Returns:
            解析后的元数据字典；若文件不合法则返回 None。
        """
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return None

        match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        if not match:
            return None

        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            return None

        if "name" not in meta or "description" not in meta:
            return None

        return meta
