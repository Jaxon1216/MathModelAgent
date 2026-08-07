"""pytest 全局配置与题目 fixture 加载。"""

import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "problems"


@pytest.fixture(scope="session")
def anyio_backend():
    """pytest-asyncio 使用 asyncio 后端。"""
    return "asyncio"


def load_problem_fixture(source: str) -> dict[str, Any]:
    """加载预解析的例题 fixture。

    Args:
        source: 例题来源目录名，如 "2025五一杯C题"。

    Returns:
        dict，包含 source、ques_all、data_files、expected_ques_count 等字段。

    Raises:
        FileNotFoundError: fixture 文件不存在。
    """
    fixture_path = FIXTURES_DIR / f"{source}.json"
    if not fixture_path.exists():
        available = [p.stem for p in FIXTURES_DIR.glob("*.json")]
        raise FileNotFoundError(
            f"Fixture 不存在: {source}。可用: {', '.join(available)}"
        )
    return json.loads(fixture_path.read_text(encoding="utf-8"))


def list_problem_fixtures() -> list[str]:
    """列出所有可用的例题 fixture 名称。"""
    return sorted([p.stem for p in FIXTURES_DIR.glob("*.json")])
