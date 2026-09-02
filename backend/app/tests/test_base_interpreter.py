"""代码解释器阶段产物快照测试。"""

from pathlib import Path

from app.tools.base_interpreter import BaseCodeInterpreter
from app.tools.notebook_serializer import NotebookSerializer


class _FakeInterpreter(BaseCodeInterpreter):
    """提供阶段产物测试所需的最小解释器实现。"""

    async def initialize(self, timeout: int = 3000) -> None:
        return None

    async def execute_code(self, code: str) -> tuple[str, bool, str]:
        return "", False, ""

    async def cleanup(self) -> None:
        return None

    async def get_created_images(self, section: str) -> list[str]:
        return []


def _make_interpreter(tmp_path: Path) -> _FakeInterpreter:
    """创建使用临时工作目录的测试解释器。"""
    return _FakeInterpreter(
        task_id="artifact-baseline-test",
        work_dir=str(tmp_path),
        notebook_serializer=NotebookSerializer(str(tmp_path)),
    )


def test_missing_phase_baseline_returns_no_work_dir_artifacts(tmp_path: Path):
    """未开始的 no-data EDA 或 Modeler phase 不应接管已有文件。"""
    (tmp_path / "preexisting.png").write_bytes(b"png")
    interpreter = _make_interpreter(tmp_path)

    assert interpreter.get_section_artifacts("eda") == []
    assert interpreter.get_section_artifacts("modeler") == []


def test_section_baseline_returns_only_real_new_artifacts(tmp_path: Path):
    """真实 Coder phase 只返回 add_section 之后写入的文件。"""
    (tmp_path / "preexisting.csv").write_text("value\n1\n", encoding="utf-8")
    interpreter = _make_interpreter(tmp_path)

    interpreter.add_section("ques1")
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "new_result.csv").write_text(
        "value\n2\n",
        encoding="utf-8",
    )

    assert interpreter.get_section_artifacts("ques1") == ["results/new_result.csv"]
