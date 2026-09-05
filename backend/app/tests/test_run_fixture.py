"""唯一 fixture E2E runner 的本地测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_fixture as runner


def _settings(**overrides):
    """构造不含真实密钥的 runner 配置。"""
    values = {
        "REDIS_URL": "redis://localhost:6379/0",
        "MODELER_MAX_REPAIR_ATTEMPTS": 3,
        "WRITER_MAX_TOOL_ROUNDS": 2,
        "WRITER_MAX_SEARCH_CALLS": 4,
        "OPENALEX_EMAIL": "test@example.com",
        "OPENALEX_TIMEOUT_SECONDS": 15.0,
    }
    for name in ("COORDINATOR", "MODELER", "CODER", "WRITER"):
        values[f"{name}_MODEL"] = f"{name.lower()}-model"
        values[f"{name}_API_KEY"] = "secret"
        values[f"{name}_API_TYPE"] = "openai-chat"
        values[f"{name}_BASE_URL"] = "https://example.invalid/v1"
        values[f"{name}_MAX_TOKENS"] = None
        values[f"{name}_CONTEXT_WINDOW"] = 128000
    values.update(overrides)
    return SimpleNamespace(**values)


def _fixture_tree(tmp_path: Path) -> tuple[Path, Path]:
    """创建最小 fixture 与来源文件树。"""
    fixtures = tmp_path / "fixtures"
    examples = tmp_path / "examples"
    source_dir = examples / runner.DEFAULT_SOURCE
    fixtures.mkdir()
    source_dir.mkdir(parents=True)
    (source_dir / "input.csv").write_text("id,value\n1,a\n", encoding="utf-8")
    (source_dir / "result.xlsx").write_bytes(b"template")
    payload = {
        "source": runner.DEFAULT_SOURCE,
        "ques_all": "问题 1：测试。",
        "data_files": ["input.csv", "result.xlsx"],
        "comp_template": "CHINA",
    }
    (fixtures / f"{runner.DEFAULT_SOURCE}.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    return fixtures, examples


def test_load_fixture_rejects_any_second_source():
    """runner 不能悄悄扩展为多 fixture 入口。"""
    with pytest.raises(runner.FixtureRunError, match="仅支持"):
        runner.load_fixture("other")


def test_copy_fixture_files_preserves_hash_and_rejects_unsafe_name(
    tmp_path: Path,
    monkeypatch,
):
    """复制文件必须逐个校验 hash，且文件名不能跨目录。"""
    _, examples = _fixture_tree(tmp_path)
    monkeypatch.setattr(runner, "EXAMPLES_DIR", examples)
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    hashes = runner.copy_fixture_files(
        runner.DEFAULT_SOURCE,
        ["input.csv", "result.xlsx"],
        work_dir,
    )

    assert set(hashes) == {"input.csv", "result.xlsx"}
    assert runner.sha256_file(work_dir / "input.csv") == hashes["input.csv"]
    with pytest.raises(runner.FixtureRunError, match="不安全"):
        runner.copy_fixture_files(runner.DEFAULT_SOURCE, ["../input.csv"], work_dir)


def test_config_snapshot_omits_keys_urls_and_email():
    """配置证据只记录模型和脱敏摘要。"""
    snapshot = runner.config_snapshot(_settings())
    text = json.dumps(snapshot)

    assert "secret" not in text
    assert "test@example.com" not in text
    assert "https://example.invalid" not in text
    assert snapshot["openalex_email_set"] is True
    assert runner.config_fingerprint(snapshot) == runner.config_fingerprint(
        dict(snapshot)
    )


def test_git_email_fallback_is_process_only(monkeypatch):
    """Git 邮箱只进入当前环境，不出现在返回值。"""
    monkeypatch.delenv("OPENALEX_EMAIL", raising=False)
    monkeypatch.setattr(
        runner.subprocess,
        "check_output",
        lambda *args, **kwargs: "git@example.com\n",
    )

    assert runner.ensure_openalex_email() is True
    assert runner.os.environ["OPENALEX_EMAIL"] == "git@example.com"


def test_preflight_rejects_missing_model_before_external_checks():
    """缺模型配置时不应连接 Redis 或调用 Pandoc。"""
    settings = _settings(CODER_MODEL=None)

    with pytest.raises(runner.FixtureRunError, match="CODER_MODEL"):
        asyncio.run(runner.preflight(settings))


def test_preflight_rejects_missing_pandoc(monkeypatch):
    """Pandoc 缺失必须在真实 workflow 前失败。"""
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)

    with pytest.raises(runner.FixtureRunError, match="Pandoc"):
        asyncio.run(runner.preflight(_settings()))


def test_preflight_rejects_unavailable_redis(monkeypatch):
    """Redis 不可用必须在真实 workflow 前失败。"""

    class FakeRedis:
        async def ping(self):
            raise ConnectionError("offline")

        async def aclose(self):
            return None

    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/pandoc")
    monkeypatch.setattr(
        runner.aioredis.Redis,
        "from_url",
        lambda *args, **kwargs: FakeRedis(),
    )

    with pytest.raises(runner.FixtureRunError, match="Redis 不可用"):
        asyncio.run(runner.preflight(_settings()))


def test_run_fixture_executes_workflow_and_validates_artifacts(
    tmp_path: Path,
    monkeypatch,
):
    """成功路径必须复制输入、运行 workflow 并检查 Markdown/DOCX。"""
    fixtures, examples = _fixture_tree(tmp_path)
    monkeypatch.setattr(runner, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(runner, "EXAMPLES_DIR", examples)
    monkeypatch.setenv("OPENALEX_EMAIL", "test@example.com")
    work_dir = tmp_path / "work" / "task-1"
    trace_events: list[tuple[str, str, dict]] = []

    class FakeWorkflow:
        async def execute(self, problem):
            assert problem["task_id"] == "task-1"
            (work_dir / "res.md").write_text("# result", encoding="utf-8")

    async def emit(task_id: str, event: str, **payload):
        trace_events.append((task_id, event, payload))

    def convert_docx(task_id: str) -> None:
        del task_id
        (work_dir / "res.docx").write_bytes(b"docx")

    runtime = runner.RuntimeDependencies(
        settings=_settings(),
        problem_factory=lambda **kwargs: kwargs,
        workflow_factory=FakeWorkflow,
        create_task_id=lambda: "task-1",
        create_work_dir=lambda task_id: str(work_dir),
        convert_docx=convert_docx,
        comp_template="CHINA",
        format_output="Markdown",
        trace_emit=emit,
    )
    work_dir.mkdir(parents=True)

    result = asyncio.run(
        runner.run_fixture(
            runner.DEFAULT_SOURCE,
            runtime=runtime,
            run_preflight=False,
        )
    )

    assert result["success"] is True
    assert result["task_id"] == "task-1"
    assert set(result["artifacts"]) == {"res.md", "res.docx"}
    assert (work_dir / "input.csv").is_file()
    assert trace_events[0][1] == "fixture.run"
    assert "secret" not in json.dumps(trace_events)


def test_run_fixture_preserves_task_id_on_workflow_failure(
    tmp_path: Path,
    monkeypatch,
):
    """workflow 异常必须带 task id 向 CLI 传播。"""
    fixtures, examples = _fixture_tree(tmp_path)
    monkeypatch.setattr(runner, "FIXTURES_DIR", fixtures)
    monkeypatch.setattr(runner, "EXAMPLES_DIR", examples)
    monkeypatch.setenv("OPENALEX_EMAIL", "test@example.com")
    work_dir = tmp_path / "work" / "task-failed"
    work_dir.mkdir(parents=True)

    class FailingWorkflow:
        async def execute(self, problem):
            del problem
            raise RuntimeError("workflow failed")

    async def emit(*args, **kwargs):
        return None

    runtime = runner.RuntimeDependencies(
        settings=_settings(),
        problem_factory=lambda **kwargs: kwargs,
        workflow_factory=FailingWorkflow,
        create_task_id=lambda: "task-failed",
        create_work_dir=lambda task_id: str(work_dir),
        convert_docx=lambda task_id: None,
        comp_template="CHINA",
        format_output="Markdown",
        trace_emit=emit,
    )

    with pytest.raises(runner.FixtureRunError) as exc_info:
        asyncio.run(
            runner.run_fixture(
                runner.DEFAULT_SOURCE,
                runtime=runtime,
                run_preflight=False,
            )
        )

    assert exc_info.value.task_id == "task-failed"
    assert "workflow failed" in exc_info.value.detail
