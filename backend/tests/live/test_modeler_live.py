"""真实 Modeler smoke；默认测试套件不会执行。"""

import asyncio
import os

import pytest

from scripts.smoke_modeler import run_smoke


@pytest.mark.live_modeler
def test_three_concurrent_live_modeler_runs():
    """同一 fixture 和配置下三个独立 Modeler 必须全部成功。"""
    if os.getenv("RUN_LIVE_MODELER") != "1":
        pytest.skip("使用 make smoke-modeler 显式运行真实模型测试")

    report = asyncio.run(run_smoke(runs=3, append_log=True))

    assert report["all_pass"] is True, report
    assert len(report["runs"]) == 3
    assert all(run["success"] for run in report["runs"])
