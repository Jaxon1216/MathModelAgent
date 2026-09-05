"""M2 ResultPackage 的落盘、读取和 Writer 渲染边界。"""

from app.results.package_store import (
    ResultPackagePersistenceError,
    load_result_package,
    persist_result_package,
    render_result_package_for_writer,
)

__all__ = [
    "ResultPackagePersistenceError",
    "load_result_package",
    "persist_result_package",
    "render_result_package_for_writer",
]
