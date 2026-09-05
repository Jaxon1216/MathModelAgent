"""Writer 可信引用与 OpenAlex 客户端测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.models.user_output import UserOutput
from app.schemas.A2A import ReferenceEvidence, WriterResponse
from app.services.redis_manager import redis_manager
from app.tools import openalex_scholar
from app.tools.openalex_scholar import OpenAlexScholar


def _reference(key: str = "R1") -> ReferenceEvidence:
    """构造同一篇规范化测试文献。"""
    return ReferenceEvidence(
        key=key,
        openalex_id="https://openalex.org/W1",
        doi="https://doi.org/10.1000/test",
        title="Verified paper",
        authors=["Ada Lovelace"],
        year=2026,
        canonical_citation="Ada Lovelace (2026). Verified paper. DOI: 10.1000/test",
    )


def test_user_output_numbers_verified_reference_once_across_sections(tmp_path: Path):
    """跨章节引用同一来源时复用编号且只生成一个脚注定义。"""
    output = UserOutput(str(tmp_path), ques_count=1)
    for section in output.seq:
        response = WriterResponse(response_content=f"## {section}")
        if section in {"firstPage", "ques1"}:
            response = WriterResponse(
                response_content=f"## {section}\n结论[[REF:R1]]",
                references=[_reference()],
            )
        output.set_res(section, response)

    markdown = output.get_result_to_save()

    assert markdown.count("[^1]") == 3
    assert "[^2]" not in markdown
    assert markdown.count("Ada Lovelace (2026). Verified paper") == 1


def test_user_output_removes_model_bibliography_and_keeps_global_one(tmp_path: Path):
    """章节自带参考文献块不能与系统统一文献表重复。"""
    output = UserOutput(str(tmp_path), ques_count=1)
    for section in output.seq:
        response = WriterResponse(response_content=f"## {section}")
        if section == "ques1":
            response = WriterResponse(
                response_content=(
                    "## ques1\n正文[[REF:R1]]\n\n"
                    "## 参考文献\n\n[1] 模型自行排版[[REF:R1]]"
                ),
                references=[_reference()],
            )
        output.set_res(section, response)

    markdown = output.get_result_to_save()

    assert markdown.count("参考文献") == 1
    assert "模型自行排版" not in markdown
    assert markdown.count("Ada Lovelace (2026). Verified paper") == 1


def test_openalex_uses_async_client_timeout_and_keeps_source_id(monkeypatch):
    """OpenAlex 请求必须异步执行、应用超时并保留稳定来源 ID。"""
    captured: dict = {}

    class _Response:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "results": [
                    {
                        "id": "https://openalex.org/W1",
                        "display_name": "Verified paper",
                        "authorships": [{"author": {"display_name": "Ada Lovelace"}}],
                        "cited_by_count": 3,
                        "doi": "https://doi.org/10.1000/test",
                        "publication_year": 2026,
                        "biblio": {},
                        "abstract_inverted_index": {"verified": [0]},
                    }
                ]
            }

    class _Client:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def get(self, url: str, *, params: dict, headers: dict):
            captured.update(url=url, params=params, headers=headers)
            return _Response()

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(openalex_scholar.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(redis_manager, "publish_message", noop)
    scholar = OpenAlexScholar("task", email="test@example.com", timeout_seconds=2.5)

    papers = asyncio.run(scholar.search_papers("topic"))

    assert captured["timeout"] == 2.5
    assert captured["params"]["search"] == "topic"
    assert papers[0]["openalex_id"] == "https://openalex.org/W1"
