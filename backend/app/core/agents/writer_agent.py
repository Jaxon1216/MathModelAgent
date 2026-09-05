"""写作手 Agent 模块，负责基于已验证结果撰写学术论文。"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from app.config.setting import ApiType, settings
from app.core.agents.agent import Agent
from app.core.functions import writer_tools, writer_tools_anthropic
from app.core.llm.llm import LLM
from app.core.llm.types import ToolCall
from app.core.prompts import get_writer_prompt
from app.schemas.A2A import ReferenceEvidence, WriterResponse
from app.schemas.enums import CompTemplate, FormatOutPut
from app.schemas.response import SystemMessage, WriterMessage
from app.services.redis_manager import redis_manager
from app.services.trace_recorder import trace_recorder
from app.tools.openalex_scholar import OpenAlexScholar
from app.utils.log_util import logger

_REFERENCE_TOKEN_RE = re.compile(r"\[\[REF:(R\d+)\]\]")
_MODEL_INLINE_REFERENCE_RE = re.compile(r"\{\[\^\d+\]:.*?\}", re.DOTALL)
_MODEL_FOOTNOTE_DEFINITION_RE = re.compile(r"(?m)^\s*\[\^\d+\]:[^\n]*\n?")
_MODEL_FOOTNOTE_MARK_RE = re.compile(r"\[\^\d+\]")


class WriterAgent(Agent):
    """基于当前章节材料执行有界写作与文献检索。"""

    def __init__(
        self,
        task_id: str,
        model: LLM,
        comp_template: CompTemplate = CompTemplate.CHINA,
        format_output: FormatOutPut = FormatOutPut.Markdown,
        scholar: OpenAlexScholar | None = None,
        context_window: int = 128000,
        cancel_event: asyncio.Event | None = None,
        max_tool_rounds: int = settings.WRITER_MAX_TOOL_ROUNDS,
        max_search_calls: int = settings.WRITER_MAX_SEARCH_CALLS,
    ) -> None:
        super().__init__(task_id, model, context_window, cancel_event=cancel_event)
        self.format_out_put = format_output
        self.comp_template = comp_template
        self.scholar = scholar
        self.system_prompt = get_writer_prompt(format_output)
        self.available_images: list[str] = []
        self.max_tool_rounds = max_tool_rounds
        self.max_search_calls = max_search_calls
        self._references_by_key: dict[str, ReferenceEvidence] = {}
        self._reference_keys_by_source: dict[str, str] = {}

    async def run(  # type: ignore[reportIncompatibleMethodOverride]
        self,
        prompt: str,
        available_images: list[str] | None = None,
        sub_title: str | None = None,
    ) -> WriterResponse:
        """执行一个隔离章节的写作任务。"""
        section = sub_title or "unknown"
        logger.info(f"subtitle是:{sub_title}")
        self.reset_history(f"section:{section}")
        self.available_images = list(available_images or [])
        self._references_by_key = {}
        self._reference_keys_by_source = {}

        tools = (
            writer_tools_anthropic
            if self.model.api_type == ApiType.ANTHROPIC
            else writer_tools
        )
        await self.append_chat_history(
            {"role": "system", "content": self.system_prompt}
        )
        await self.append_chat_history(
            {"role": "user", "content": self._with_image_requirements(prompt)}
        )

        limitations: list[str] = []
        response_content = ""
        search_calls = 0
        fallback_reason = "tool_budget_disabled"

        for tool_round in range(1, self.max_tool_rounds + 1):
            try:
                response = await self._chat(
                    history=self.chat_history,
                    tools=tools,
                    tool_choice="auto",
                    agent_name=self.__class__.__name__,
                    sub_title=sub_title,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - Writer 可降级交付
                limitations.append(f"writer_request_failed: {type(exc).__name__}")
                fallback_reason = "writer_request_failed"
                break

            if not response.tool_calls:
                await self.record_response(response)
                response_content = (response.content or "").strip()
                if response_content:
                    break
                limitations.append("empty_writer_response")
                fallback_reason = "empty_writer_response"
                break

            await self.record_response(response, allow_compress=False)
            await trace_recorder.emit(
                self.task_id,
                "writer.tool_round",
                agent=self.__class__.__name__,
                phase=section,
                round=tool_round,
                tool_calls=len(response.tool_calls),
            )

            for tool_call in response.tool_calls:
                if (
                    tool_call.name == "search_papers"
                    and search_calls >= self.max_search_calls
                ):
                    tool_output = (
                        "文献检索调用预算已耗尽；请使用已返回文献或无引用完成正文。"
                    )
                    limitation = "search_budget_exhausted"
                else:
                    if tool_call.name == "search_papers":
                        search_calls += 1
                    tool_output, limitation = await self._handle_tool_call(
                        tool_call,
                        section=section,
                    )
                if limitation and limitation not in limitations:
                    limitations.append(limitation)
                await self.append_chat_history(
                    {
                        "role": "tool",
                        "content": tool_output,
                        "tool_call_id": tool_call.id,
                        "name": tool_call.name,
                    }
                )

            await self.compress_if_needed()
            fallback_reason = (
                "search_budget_exhausted"
                if search_calls >= self.max_search_calls
                else "tool_round_budget_exhausted"
            )
            if search_calls >= self.max_search_calls:
                break

        if not response_content:
            response_content = await self._generate_without_tools(
                section=section,
                reason=fallback_reason,
                limitations=limitations,
            )

        response_content, references, citation_limitations = self._validate_references(
            response_content
        )
        limitations.extend(
            item for item in citation_limitations if item not in limitations
        )

        if not response_content.strip():
            response_content = self._degraded_placeholder(section)
            limitations.append("empty_final_response")

        degraded = any(
            item.startswith(("final_generation_failed", "empty_final_response"))
            for item in limitations
        )
        status = "degraded" if degraded else "success"
        await trace_recorder.emit(
            self.task_id,
            "writer.section_end",
            agent=self.__class__.__name__,
            phase=section,
            status=status,
            content_chars=len(response_content),
            search_calls=search_calls,
            reference_count=len(references),
            limitations=limitations,
        )
        logger.info(f"{self.__class__.__name__}:完成:执行对话")
        return WriterResponse(
            status=status,
            response_content=response_content,
            footnotes=[],
            references=references,
            limitations=limitations,
        )

    def _with_image_requirements(self, prompt: str) -> str:
        """将当前章节经过筛选的图片清单附加到提示词。"""
        if not self.available_images:
            return prompt
        image_lines = "\n".join(
            f"- ![{image}]({image})" for image in self.available_images
        )
        return (
            f"{prompt}\n\n【必须插入的图片列表】\n"
            "以下图片是代码手生成的，你必须在论文相关段落后逐一插入：\n"
            f"{image_lines}\n"
            "图片使用原始文件名，每张图片后需给出充分的结果解读。\n"
        )

    async def _generate_without_tools(
        self,
        *,
        section: str,
        reason: str,
        limitations: list[str],
    ) -> str:
        """禁用工具执行一次最终正文生成。"""
        allowed = ", ".join(self._references_by_key) or "无"
        await trace_recorder.emit(
            self.task_id,
            "writer.fallback",
            agent=self.__class__.__name__,
            phase=section,
            reason=reason,
            allowed_references=list(self._references_by_key),
        )
        await self.append_chat_history(
            {
                "role": "user",
                "content": (
                    "文献检索阶段已经结束。现在直接输出完整章节正文，不再调用工具。"
                    f"仅可使用这些已验证引用标记：{allowed}。"
                    "若列表为‘无’，不要添加任何引用、DOI 或参考文献。"
                    "不要输出检索错误或过程说明。"
                ),
            }
        )
        try:
            response = await self._chat(
                history=self.chat_history,
                tools=None,
                tool_choice=None,
                agent_name=self.__class__.__name__,
                sub_title=section,
            )
            await self.record_response(response)
            return (response.content or "").strip()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 保证可交付占位
            limitations.append(f"final_generation_failed: {type(exc).__name__}")
            return self._degraded_placeholder(section)

    async def _handle_tool_call(
        self,
        tool_call: ToolCall,
        *,
        section: str,
    ) -> tuple[str, str | None]:
        """执行一个 Writer 工具调用并返回完整配对结果。"""
        if tool_call.name != "search_papers":
            return f"未知工具 '{tool_call.name}'，未执行。", "invalid_tool: unknown"

        try:
            arguments = json.loads(tool_call.arguments)
        except (TypeError, json.JSONDecodeError):
            return (
                "工具调用参数无效：arguments 必须是 JSON 对象。",
                "invalid_tool: json",
            )
        query = arguments.get("query") if isinstance(arguments, dict) else None
        if not isinstance(query, str) or not query.strip():
            return (
                "工具调用参数无效：search_papers 需要非空 query。",
                "invalid_tool: query",
            )

        await redis_manager.publish_message(
            self.task_id,
            SystemMessage(content="写作手调用search_papers工具"),
        )
        await redis_manager.publish_message(
            self.task_id,
            WriterMessage(content=query),
        )

        try:
            if self.scholar is None:
                raise RuntimeError("scholar 未初始化")
            papers = await self.scholar.search_papers(query)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 搜索失败交给 Writer 降级
            logger.error(f"搜索文献失败: {exc}")
            await trace_recorder.emit(
                self.task_id,
                "writer.search",
                agent=self.__class__.__name__,
                phase=section,
                success=False,
                error_type=type(exc).__name__,
            )
            return (
                "文献检索失败；请勿编造引用，可继续完成不含引用的正文。",
                f"search_failed: {type(exc).__name__}",
            )

        registered = self._register_references(papers)
        await trace_recorder.emit(
            self.task_id,
            "writer.search",
            agent=self.__class__.__name__,
            phase=section,
            success=bool(registered),
            result_count=len(registered),
        )
        if not registered:
            return (
                "未检索到可验证文献；请勿编造引用，可继续完成不含引用的正文。",
                "search_empty",
            )
        return self._format_references_for_model(registered), None

    def _register_references(
        self, papers: list[dict[str, Any]]
    ) -> list[tuple[ReferenceEvidence, dict[str, Any]]]:
        """注册带稳定来源的文献，并为本章节分配引用键。"""
        registered: list[tuple[ReferenceEvidence, dict[str, Any]]] = []
        for paper in papers:
            openalex_id = str(paper.get("openalex_id") or "").strip()
            title = str(paper.get("title") or "").strip()
            citation = str(paper.get("citation_format") or "").strip()
            if not openalex_id or not title or not citation:
                continue
            source_key = openalex_id
            key = self._reference_keys_by_source.get(source_key)
            if key is None:
                key = f"R{len(self._references_by_key) + 1}"
                authors = [
                    str(author.get("name") or "").strip()
                    for author in paper.get("authors", [])
                    if isinstance(author, dict) and author.get("name")
                ]
                reference = ReferenceEvidence(
                    key=key,
                    openalex_id=openalex_id,
                    doi=str(paper.get("doi") or "").strip() or None,
                    title=title,
                    authors=authors,
                    year=paper.get("publication_year"),
                    canonical_citation=citation,
                )
                self._references_by_key[key] = reference
                self._reference_keys_by_source[source_key] = key
            registered.append((self._references_by_key[key], paper))
        return registered

    @staticmethod
    def _format_references_for_model(
        registered: list[tuple[ReferenceEvidence, dict[str, Any]]],
    ) -> str:
        """把检索结果渲染为带白名单引用键的模型上下文。"""
        blocks = []
        for reference, paper in registered:
            abstract = str(paper.get("abstract") or "").strip()
            blocks.append(
                "\n".join(
                    [
                        f"引用标记: [[REF:{reference.key}]]",
                        f"标题: {reference.title}",
                        f"摘要: {abstract}",
                        f"规范引用: {reference.canonical_citation}",
                    ]
                )
            )
        return "\n\n".join(blocks)

    def _validate_references(
        self, content: str
    ) -> tuple[str, list[ReferenceEvidence], list[str]]:
        """移除模型自造引用，只保留本章节检索白名单中的标记。"""
        limitations: list[str] = []
        if (
            _MODEL_INLINE_REFERENCE_RE.search(content)
            or _MODEL_FOOTNOTE_DEFINITION_RE.search(content)
            or _MODEL_FOOTNOTE_MARK_RE.search(content)
        ):
            limitations.append("invalid_citation: model_authored_reference_removed")
        cleaned = _MODEL_INLINE_REFERENCE_RE.sub("", content)
        cleaned = _MODEL_FOOTNOTE_DEFINITION_RE.sub("", cleaned)
        cleaned = _MODEL_FOOTNOTE_MARK_RE.sub("", cleaned)

        used_keys: list[str] = []

        def validate_token(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in self._references_by_key:
                if "invalid_citation: unknown_reference_key" not in limitations:
                    limitations.append("invalid_citation: unknown_reference_key")
                return ""
            if key not in used_keys:
                used_keys.append(key)
            return match.group(0)

        cleaned = _REFERENCE_TOKEN_RE.sub(validate_token, cleaned).strip()
        references = [self._references_by_key[key] for key in used_keys]
        return cleaned, references, limitations

    @staticmethod
    def _degraded_placeholder(section: str) -> str:
        """返回不会被误判为正常正文的确定性占位。"""
        return f"本节未能生成完整正文（章节：{section}），相关限制已记录。"

    async def summarize(self) -> str:
        """总结当前章节对话内容，生成任务执行摘要。"""
        try:
            await self.append_chat_history(
                {"role": "user", "content": "请简单总结以上完成什么任务取得什么结果:"}
            )
            response = await self._chat(
                history=self.chat_history,
                agent_name=self.__class__.__name__,
            )
            await self.record_response(response)
            return response.content or ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 摘要不是主交付物
            logger.error(f"总结生成失败: {exc}")
            return "由于网络原因无法生成详细总结，但已完成主要任务处理。"
