"""Modeler smoke runner 的纯本地基础测试。"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config.setting import Settings
from scripts.smoke_modeler import (
    DEFAULT_FIXTURE,
    append_experiment_log,
    config_fingerprint,
    load_fixture,
)


def test_modeler_repair_attempts_setting_is_user_configurable(monkeypatch):
    """环境配置允许 0-3 次格式修复，默认保持一次。"""
    monkeypatch.delenv("MODELER_MAX_REPAIR_ATTEMPTS", raising=False)
    assert (
        Settings(_env_file=None).MODELER_MAX_REPAIR_ATTEMPTS  # type: ignore[call-arg]
        == 1
    )

    monkeypatch.setenv("MODELER_MAX_REPAIR_ATTEMPTS", "3")
    assert (
        Settings(_env_file=None).MODELER_MAX_REPAIR_ATTEMPTS  # type: ignore[call-arg]
        == 3
    )

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            MODELER_MAX_REPAIR_ATTEMPTS=4,
        )


def test_smoke_fixture_is_a_complete_domain_problem():
    """固定 smoke fixture 应覆盖三问、四张输入表和三个输出模板。"""
    problem = load_fixture(DEFAULT_FIXTURE)

    assert problem.question_set.question_count == 3
    assert set(problem.question_set.questions) == {"ques1", "ques2", "ques3"}
    assert len(problem.data_catalog.input_tables) == 4
    assert problem.data_catalog.output_templates == (
        "result1_1.xlsx",
        "result1_2.xlsx",
        "result2.xlsx",
    )


def test_config_fingerprint_is_stable_and_log_omits_raw_output(tmp_path: Path):
    """实验日志只记录配置指纹和有限证据，不保存模型正文。"""
    snapshot = {
        "api_type": "openai-chat",
        "model": "model",
        "base_url_set": True,
        "max_tokens": 4096,
        "prompt_contract": "m1",
    }
    assert config_fingerprint(snapshot) == config_fingerprint(dict(snapshot))

    log_path = tmp_path / "experiment-log.md"
    report = {
        "timestamp": "2026-09-04T00:00:00+00:00",
        "git_revision": "abc123",
        "worktree_dirty": False,
        "fixture": "fixtures/modeler/2024高教杯C题.json",
        "fixture_sha256": "fixture-hash",
        "config": snapshot,
        "config_fingerprint": config_fingerprint(snapshot),
        "all_pass": False,
        "runs": [
            {
                "run": 1,
                "success": False,
                "latency_ms": 12,
                "attempts": 1,
                "raw_output_passed": False,
                "failure_kind": "invalid_response",
                "failure_reason": "schema error | no raw model content",
            }
        ],
    }

    append_experiment_log(report, log_path)
    text = log_path.read_text(encoding="utf-8")

    assert "配置指纹" in text
    assert "worktree dirty：`false`" in text
    assert "invalid_response" in text
    assert "\\|" in text
    assert "raw model response" not in text
