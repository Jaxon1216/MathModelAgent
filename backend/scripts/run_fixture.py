"""运行仓库唯一问题 fixture 的可复现全链路 E2E。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

import redis.asyncio as aioredis

BACKEND_ROOT = Path(__file__).resolve().parent.parent
REPOSITORY_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

DEFAULT_SOURCE = "2024高教杯C题"
FIXTURES_DIR = BACKEND_ROOT / "fixtures" / "problems"
EXAMPLES_DIR = BACKEND_ROOT / "app" / "example" / "example"
M1_SOURCE_PATHS = (
    "backend/app",
    "backend/scripts/run_fixture.py",
    "backend/scripts/smoke_modeler.py",
    "backend/fixtures/modeler",
    "backend/fixtures/baseline/2024高教杯C题/expected.json",
    "backend/Makefile",
    "backend/pyproject.toml",
)


class FixtureRunError(RuntimeError):
    """Fixture runner 的稳定失败，保留已分配的 task id。"""

    def __init__(self, detail: str, *, task_id: str | None = None) -> None:
        self.detail = detail
        self.task_id = task_id
        super().__init__(detail)


@dataclass(frozen=True)
class RuntimeDependencies:
    """延迟加载的应用运行时依赖，便于本地单测替换。"""

    settings: Any
    problem_factory: Callable[..., Any]
    workflow_factory: Callable[[], Any]
    create_task_id: Callable[[], str]
    create_work_dir: Callable[[str], str]
    convert_docx: Callable[[str], None]
    comp_template: Any
    format_output: Any
    trace_emit: Callable[..., Awaitable[None]]


def load_fixture(source: str) -> tuple[dict[str, Any], Path]:
    """读取唯一允许的 E2E fixture。"""
    if source != DEFAULT_SOURCE:
        raise FixtureRunError(f"仅支持 fixture: {DEFAULT_SOURCE}")
    path = FIXTURES_DIR / f"{source}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureRunError(f"无法读取 fixture: {path}") from exc
    if payload.get("source") != source:
        raise FixtureRunError("fixture source 与文件名不一致")
    data_files = payload.get("data_files")
    if not isinstance(data_files, list) or not data_files:
        raise FixtureRunError("fixture data_files 不能为空")
    return payload, path


def sha256_file(path: Path) -> str:
    """计算文件 SHA-256。"""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_fixture_files(
    source: str,
    filenames: list[str],
    work_dir: Path,
) -> dict[str, str]:
    """复制声明文件并确认副本与来源 hash 一致。"""
    source_dir = EXAMPLES_DIR / source
    hashes: dict[str, str] = {}
    for filename in filenames:
        if Path(filename).name != filename:
            raise FixtureRunError(f"fixture 文件名不安全: {filename}")
        src = source_dir / filename
        dst = work_dir / filename
        if not src.is_file():
            raise FixtureRunError(f"fixture 文件不存在: {src}")
        source_hash = sha256_file(src)
        shutil.copy2(src, dst)
        if sha256_file(dst) != source_hash:
            raise FixtureRunError(f"fixture 文件复制校验失败: {filename}")
        hashes[filename] = source_hash
    return hashes


def ensure_openalex_email() -> bool:
    """缺少环境值时使用 Git 邮箱，仅写入当前进程。"""
    if os.environ.get("OPENALEX_EMAIL", "").strip():
        return True
    try:
        email = subprocess.check_output(
            ["git", "config", "user.email"],
            cwd=REPOSITORY_ROOT,
            text=True,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return False
    if not email:
        return False
    os.environ["OPENALEX_EMAIL"] = email
    return True


def git_snapshot() -> dict[str, Any]:
    """读取不含敏感信息的源码版本状态。"""
    return {
        "revision": _git_output(["rev-parse", "HEAD"]) or "unknown",
        "worktree_dirty": bool(_git_output(["status", "--porcelain"])),
        "m1_source_dirty": bool(
            _git_output(["status", "--porcelain", "--", *M1_SOURCE_PATHS])
        ),
    }


def config_snapshot(settings: Any) -> dict[str, Any]:
    """返回不含 API Key、URL 和邮箱值的四 Agent 配置快照。"""
    agents: dict[str, dict[str, Any]] = {}
    for name in ("COORDINATOR", "MODELER", "CODER", "WRITER"):
        api_type = getattr(settings, f"{name}_API_TYPE")
        base_url = getattr(settings, f"{name}_BASE_URL") or ""
        agents[name.lower()] = {
            "model": getattr(settings, f"{name}_MODEL"),
            "api_type": getattr(api_type, "value", api_type),
            "base_url_set": bool(base_url),
            "base_url_sha256": (
                hashlib.sha256(base_url.encode("utf-8")).hexdigest()[:16]
                if base_url
                else None
            ),
            "max_tokens": getattr(settings, f"{name}_MAX_TOKENS"),
            "context_window": getattr(settings, f"{name}_CONTEXT_WINDOW"),
        }
    return {
        "agents": agents,
        "modeler_max_repair_attempts": settings.MODELER_MAX_REPAIR_ATTEMPTS,
        "writer_max_tool_rounds": settings.WRITER_MAX_TOOL_ROUNDS,
        "writer_max_search_calls": settings.WRITER_MAX_SEARCH_CALLS,
        "openalex_timeout_seconds": settings.OPENALEX_TIMEOUT_SECONDS,
        "openalex_email_set": bool(settings.OPENALEX_EMAIL),
    }


def config_fingerprint(snapshot: dict[str, Any]) -> str:
    """计算稳定且脱敏的配置指纹。"""
    payload = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


async def preflight(settings: Any) -> dict[str, Any]:
    """检查一次完整 E2E 所需的本地依赖，不调用模型。"""
    missing: list[str] = []
    for name in ("COORDINATOR", "MODELER", "CODER", "WRITER"):
        if not str(getattr(settings, f"{name}_MODEL") or "").strip():
            missing.append(f"{name}_MODEL")
        if not str(getattr(settings, f"{name}_API_KEY") or "").strip():
            missing.append(f"{name}_API_KEY")
    if missing:
        raise FixtureRunError(f"缺少模型配置: {', '.join(missing)}")
    if not settings.OPENALEX_EMAIL:
        raise FixtureRunError("OPENALEX_EMAIL 未配置且 Git 邮箱不可用")
    if shutil.which("pandoc") is None:
        raise FixtureRunError("Pandoc 不可用")

    client = aioredis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except Exception as exc:
        raise FixtureRunError(f"Redis 不可用: {type(exc).__name__}") from exc
    finally:
        await client.aclose()

    snapshot = config_snapshot(settings)
    return {
        "config": snapshot,
        "config_fingerprint": config_fingerprint(snapshot),
        "git": git_snapshot(),
    }


async def run_fixture(
    source: str = DEFAULT_SOURCE,
    *,
    runtime: RuntimeDependencies | None = None,
    run_preflight: bool = True,
) -> dict[str, Any]:
    """复制 fixture 并运行完整 workflow 与 DOCX 导出。"""
    ensure_openalex_email()
    dependencies = runtime or _load_runtime()
    fixture, fixture_path = load_fixture(source)
    preflight_report = (
        await preflight(dependencies.settings)
        if run_preflight
        else {
            "config": config_snapshot(dependencies.settings),
            "config_fingerprint": config_fingerprint(
                config_snapshot(dependencies.settings)
            ),
            "git": git_snapshot(),
        }
    )

    task_id = dependencies.create_task_id()
    work_dir = Path(dependencies.create_work_dir(task_id))
    try:
        data_hashes = copy_fixture_files(
            source,
            [str(name) for name in fixture["data_files"]],
            work_dir,
        )
        fixture_hash = sha256_file(fixture_path)
        await dependencies.trace_emit(
            task_id,
            "fixture.run",
            source=source,
            fixture_sha256=fixture_hash,
            data_file_sha256=data_hashes,
            config=preflight_report["config"],
            config_fingerprint=preflight_report["config_fingerprint"],
            git=preflight_report["git"],
        )

        problem = dependencies.problem_factory(
            task_id=task_id,
            ques_all=fixture["ques_all"],
            comp_template=dependencies.comp_template,
            format_output=dependencies.format_output,
        )
        await dependencies.workflow_factory().execute(problem)
        dependencies.convert_docx(task_id)
        artifacts = _validate_final_artifacts(work_dir)
        return {
            "success": True,
            "task_id": task_id,
            "work_dir": str(work_dir),
            "artifacts": artifacts,
            "config_fingerprint": preflight_report["config_fingerprint"],
        }
    except FixtureRunError as exc:
        if exc.task_id is not None:
            raise
        raise FixtureRunError(exc.detail, task_id=task_id) from exc
    except Exception as exc:
        raise FixtureRunError(
            f"{type(exc).__name__}: {exc}",
            task_id=task_id,
        ) from exc


def _load_runtime() -> RuntimeDependencies:
    """在邮箱 fallback 完成后导入应用 settings 和 workflow。"""
    from app.config.setting import settings
    from app.core.workflow import MathModelWorkFlow
    from app.schemas.enums import CompTemplate, FormatOutPut
    from app.schemas.request import Problem
    from app.services.trace_recorder import trace_recorder
    from app.utils.common_utils import create_task_id, create_work_dir, md_2_docx

    return RuntimeDependencies(
        settings=settings,
        problem_factory=Problem,
        workflow_factory=MathModelWorkFlow,
        create_task_id=create_task_id,
        create_work_dir=create_work_dir,
        convert_docx=md_2_docx,
        comp_template=CompTemplate.CHINA,
        format_output=FormatOutPut.Markdown,
        trace_emit=trace_recorder.emit,
    )


def _validate_final_artifacts(work_dir: Path) -> dict[str, str]:
    """要求 Markdown 和 DOCX 都存在且非空。"""
    artifacts: dict[str, str] = {}
    for name in ("res.md", "res.docx"):
        path = work_dir / name
        if not path.is_file() or path.stat().st_size <= 0:
            raise FixtureRunError(f"缺少非空最终产物: {name}")
        artifacts[name] = sha256_file(path)
    return artifacts


def _git_output(args: list[str]) -> str:
    """执行只读 Git 命令，失败时返回空字符串。"""
    try:
        return subprocess.check_output(
            ["git", *args],
            cwd=REPOSITORY_ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def main() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="Run the repository E2E fixture")
    parser.add_argument("--source", default=DEFAULT_SOURCE)
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="只检查环境，不复制数据或调用模型",
    )
    args = parser.parse_args()

    ensure_openalex_email()
    if args.preflight_only:
        try:
            report = asyncio.run(preflight(_load_runtime().settings))
        except FixtureRunError as exc:
            print(
                json.dumps({"success": False, "error": exc.detail}, ensure_ascii=False)
            )
            return 1
        print(json.dumps({"success": True, **report}, ensure_ascii=False))
        return 0

    try:
        result = asyncio.run(run_fixture(args.source))
    except FixtureRunError as exc:
        print(
            json.dumps(
                {
                    "success": False,
                    "task_id": exc.task_id,
                    "error": exc.detail,
                },
                ensure_ascii=False,
            )
        )
        return 1

    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
