"""并发运行真实 Modeler smoke，并追加可复现实验证据。

用法:
    uv run python -m scripts.smoke_modeler
    uv run python -m scripts.smoke_modeler --runs 3 --no-log
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.agents.modeler import ModelerAgent
from app.config.setting import settings
from app.core.llm.llm import LLM
from app.domain.model_plan import ModelPlanValidationError, PlanValidation
from app.domain.problem import Problem
from app.orchestration.workflow import ModelerStageError, ModelerWorkflow
from app.runtime.llm.client import LLMClient, LegacyLLMClient
from app.runtime.tracing import LegacyStageTracer

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = BACKEND_ROOT.parent
DEFAULT_FIXTURE = BACKEND_ROOT / "fixtures" / "modeler" / "2024高教杯C题.json"
DEFAULT_EXPERIMENT_LOG = REPOSITORY_ROOT / "docs" / "enhance" / "experiment-log.md"


@dataclass(frozen=True)
class SmokeRunResult:
    """单次真实 Modeler 调用的有限证据。"""

    run: int
    task_id: str
    success: bool
    latency_ms: int
    attempts: int
    raw_output_passed: bool
    plan_sha256: str | None
    failure_kind: str | None
    failure_reason: str | None


class RecordingLLMClient:
    """记录原始响应次数，不改变底层 LLM 行为。"""

    def __init__(self, delegate: LLMClient) -> None:
        self._delegate = delegate
        self.responses: list[str] = []

    async def complete(self, messages: list[dict[str, str]]) -> str:
        """代理调用并仅在内存中保存原始文本。"""
        response = await self._delegate.complete(messages)
        self.responses.append(response)
        return response


def load_fixture(path: Path = DEFAULT_FIXTURE) -> Problem:
    """加载固定 Modeler 领域 fixture。"""
    return Problem.model_validate_json(path.read_text(encoding="utf-8"))


def model_config_snapshot() -> dict[str, Any]:
    """返回不含密钥的 Modeler 配置快照。"""
    api_type = settings.MODELER_API_TYPE
    base_url = settings.MODELER_BASE_URL or ""
    return {
        "api_type": getattr(api_type, "value", api_type),
        "model": settings.MODELER_MODEL,
        "base_url_set": bool(base_url),
        "base_url_sha256": (
            hashlib.sha256(base_url.encode("utf-8")).hexdigest()[:16]
            if base_url
            else None
        ),
        "max_tokens": settings.MODELER_MAX_TOKENS,
        "max_repair_attempts": settings.MODELER_MAX_REPAIR_ATTEMPTS,
        "prompt_contract": "m1",
    }


def config_fingerprint(snapshot: dict[str, Any]) -> str:
    """计算稳定配置指纹。"""
    payload = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


async def run_smoke(
    *,
    fixture_path: Path = DEFAULT_FIXTURE,
    runs: int = 3,
    append_log: bool = True,
    experiment_log: Path = DEFAULT_EXPERIMENT_LOG,
) -> dict[str, Any]:
    """并发执行多次独立真实 Modeler smoke。

    Args:
        fixture_path: 固定领域 fixture。
        runs: 独立运行次数。
        append_log: 是否追加 Markdown 实验日志。
        experiment_log: 实验日志路径。

    Returns:
        不含模型原始正文和密钥的结构化报告。

    Raises:
        ValueError: runs 小于 1。
    """
    if runs < 1:
        raise ValueError("runs 必须大于等于 1")

    fixture_bytes = fixture_path.read_bytes()
    base_problem = Problem.model_validate_json(fixture_bytes)
    snapshot = model_config_snapshot()
    fingerprint = config_fingerprint(snapshot)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")

    results = await asyncio.gather(
        *[
            _run_once(
                run=index,
                task_id=f"modeler-smoke-{stamp}-{index}",
                base_problem=base_problem,
            )
            for index in range(1, runs + 1)
        ]
    )
    report = {
        "timestamp": datetime.now(UTC).isoformat(),
        "git_revision": _git_revision(),
        "worktree_dirty": _git_worktree_dirty(),
        "source_dirty": _git_source_dirty(),
        "fixture": str(fixture_path.relative_to(BACKEND_ROOT)),
        "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
        "config": snapshot,
        "config_fingerprint": fingerprint,
        "runs": [asdict(result) for result in results],
        "all_pass": all(result.success for result in results),
    }
    if append_log:
        append_experiment_log(report, experiment_log)
    return report


async def _run_once(
    *,
    run: int,
    task_id: str,
    base_problem: Problem,
) -> SmokeRunResult:
    """执行一次隔离的真实 Modeler 调用。"""
    problem = base_problem.model_copy(update={"task_id": task_id})
    legacy_llm = LLM(
        api_type=settings.MODELER_API_TYPE,
        api_key=settings.MODELER_API_KEY,
        model=settings.MODELER_MODEL,
        base_url=settings.MODELER_BASE_URL,
        task_id=task_id,
        max_tokens=settings.MODELER_MAX_TOKENS,
    )
    client = RecordingLLMClient(LegacyLLMClient(legacy_llm))
    workflow = ModelerWorkflow(
        ModelerAgent(
            client,
            max_repair_attempts=settings.MODELER_MAX_REPAIR_ATTEMPTS,
        ),
        tracer=LegacyStageTracer(),
    )
    started_at = time.monotonic()

    try:
        plan = await workflow.create_plan(problem)
        raw_output_passed = _first_response_passed(problem, client.responses)
        plan_json = plan.model_dump_json()
        return SmokeRunResult(
            run=run,
            task_id=task_id,
            success=True,
            latency_ms=int((time.monotonic() - started_at) * 1000),
            attempts=len(client.responses),
            raw_output_passed=raw_output_passed,
            plan_sha256=hashlib.sha256(plan_json.encode("utf-8")).hexdigest(),
            failure_kind=None,
            failure_reason=None,
        )
    except ModelerStageError as exc:
        return SmokeRunResult(
            run=run,
            task_id=task_id,
            success=False,
            latency_ms=int((time.monotonic() - started_at) * 1000),
            attempts=len(client.responses),
            raw_output_passed=_first_response_passed(problem, client.responses),
            plan_sha256=None,
            failure_kind=exc.failure_kind,
            failure_reason=exc.detail[:500],
        )
    except Exception as exc:
        return SmokeRunResult(
            run=run,
            task_id=task_id,
            success=False,
            latency_ms=int((time.monotonic() - started_at) * 1000),
            attempts=len(client.responses),
            raw_output_passed=_first_response_passed(problem, client.responses),
            plan_sha256=None,
            failure_kind="unexpected",
            failure_reason=f"{type(exc).__name__}: {str(exc)[:400]}",
        )


def _first_response_passed(problem: Problem, responses: list[str]) -> bool:
    """判断未经修复的第一份原始响应是否直接通过。"""
    if not responses:
        return False
    try:
        PlanValidation.for_problem(problem).parse_json(responses[0])
    except ModelPlanValidationError:
        return False
    return True


def append_experiment_log(report: dict[str, Any], path: Path) -> None:
    """将有限 smoke 证据追加到 Markdown 日志。"""
    lines = [
        "",
        f"## {report['timestamp']} - Modeler 真实 smoke",
        "",
        f"- git revision：`{report['git_revision']}`",
        f"- worktree dirty：`{str(report['worktree_dirty']).lower()}`",
        f"- M1 source dirty：`{str(report['source_dirty']).lower()}`",
        f"- fixture：`{report['fixture']}`，SHA256 `{report['fixture_sha256']}`",
        (
            f"- 模型：`{report['config'].get('model')}`，API 类型："
            f"`{report['config'].get('api_type')}`，配置指纹："
            f"`{report['config_fingerprint']}`"
        ),
        f"- 结果：`all_pass={str(report['all_pass']).lower()}`",
        "",
        "| run | success | latency_ms | attempts | raw_output_passed | failure |",
        "|---:|---|---:|---:|---|---|",
    ]
    for result in report["runs"]:
        failure = result["failure_kind"] or ""
        if result["failure_reason"]:
            failure = f"{failure}: {result['failure_reason']}"
        failure = failure.replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {result['run']} | {str(result['success']).lower()} | "
            f"{result['latency_ms']} | {result['attempts']} | "
            f"{str(result['raw_output_passed']).lower()} | {failure} |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write("\n".join(lines) + "\n")


def _git_revision() -> str:
    """读取当前仓库 revision；失败时返回 unknown。"""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=REPOSITORY_ROOT,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _git_worktree_dirty() -> bool | None:
    """返回工作树是否有未提交变化；读取失败时返回 None。"""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        return bool(result.stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        return None


def _git_source_dirty() -> bool | None:
    """仅检查会影响 M1 smoke 行为的代码、配置和 fixture。"""
    try:
        result = subprocess.run(
            [
                "git",
                "status",
                "--porcelain",
                "--",
                "backend/app",
                "backend/scripts/smoke_modeler.py",
                "backend/fixtures/modeler",
                "backend/Makefile",
                "backend/pyproject.toml",
            ],
            cwd=REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        return bool(result.stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        return None


def main() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="Run concurrent live Modeler smoke")
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--no-log", action="store_true")
    args = parser.parse_args()

    report = asyncio.run(
        run_smoke(
            fixture_path=args.fixture,
            runs=args.runs,
            append_log=not args.no_log,
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["all_pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
