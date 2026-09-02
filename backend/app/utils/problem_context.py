"""题面公共内容与执行约束的确定性分流工具。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence


_CONSTRAINT_MARKERS = (
    "不要使用",
    "禁止使用",
    "不得使用",
    "不能使用",
    "不可使用",
    "请勿使用",
    "禁止采用",
    "不得采用",
    "不能采用",
    "不可采用",
    "请勿采用",
    "必须使用",
    "必须调用",
    "只能使用",
    "请使用",
    "不要调用",
    "禁止调用",
    "不得调用",
    "执行约束",
    "输出格式",
    "运行策略",
    "请将结果保存为",
    "请把结果保存为",
    "将结果保存为",
    "结果保存为",
    "保存结果为",
    "保存结果至",
    "结果保存至",
    "请将结果保存到",
    "请把结果保存到",
    "将结果保存到",
    "请将结果写入",
    "请把结果写入",
    "将结果写入",
    "输出保存为",
    "do not use",
    "do not adopt",
    "must not use",
    "must not adopt",
    "must use",
    "must call",
    "please save the result as",
    "please save the results as",
    "save the result as",
    "save the results as",
    "save output as",
    "save the output as",
    "save the result to",
    "save the results to",
    "write the result to",
    "write the results to",
    "store the result in",
    "store the results in",
    "execution constraint",
    "output format",
)
_CONSTRAINT_MARKER_RE = re.compile(
    "|".join(re.escape(marker) for marker in _CONSTRAINT_MARKERS),
    re.IGNORECASE,
)
_PROCESS_ECHO_MARKERS = (
    "按要求",
    "按照要求",
    "根据要求",
    "遵循要求",
    "按题目要求",
    "按照题目要求",
    "根据题目要求",
    "用户要求",
    "为满足要求",
    "满足要求",
    "按约束",
    "按照约束",
    "根据约束",
    "遵循约束",
    "满足限制",
    "在限制下",
    "as requested",
    "as required",
    "according to the requirement",
    "to meet the requirement",
    "to satisfy the requirement",
    "to comply with the constraint",
)
_PROCESS_ECHO_MARKER_RE = re.compile(
    "|".join(re.escape(marker) for marker in _PROCESS_ECHO_MARKERS),
    re.IGNORECASE,
)
_RAW_PROBLEM_MARKERS = (
    "原始题面",
    "题面原文",
    "原始问题",
    "原始任务",
    "题目原文",
    "用户题面",
    "用户输入",
    "original problem",
    "original prompt",
    "original task",
    "problem statement",
    "task statement",
    "user prompt",
    "user input",
)
_RAW_PROBLEM_MARKER_RE = re.compile(
    "|".join(re.escape(marker) for marker in _RAW_PROBLEM_MARKERS),
    re.IGNORECASE,
)
_CLAUSE_BOUNDARY_RE = re.compile(r"(?<=[。！？!?；;\n])|(?<=[.])(?=\s|$)")
_EMBEDDED_CONSTRAINT_CLAUSE_BOUNDARY_RE = re.compile(
    r"(?<=[。！？!?\n])|(?<=[.])(?=\s|$)"
)
_EMBEDDED_CONSTRAINT_BOUNDARY_RE = re.compile(
    r"[，,；;。！？!?\n]|[.](?=\s|$)"
)
_WHITESPACE_RE = re.compile(r"[ \t]+")
_TRUNCATION_SUFFIX = "\n...[内容已截断]"
_REWRITE_MARKERS = (
    "改用",
    "改为",
    "替代",
    "转而",
    "instead",
    "rather than",
    "in place of",
    "to comply",
    "in compliance",
    "to satisfy",
    "due to the restriction",
    "due to the requirement",
)
_RESTRICTION_MARKERS = (
    "限制",
    "约束",
    "要求",
    "禁令",
    "restriction",
    "constraint",
    "requirement",
    "prohibition",
    "forbidden",
)
RAW_PROBLEM_OVERLAP_THRESHOLD = 32
_RAW_ECHO_BOUNDARY_RE = re.compile(
    r"[。！？!?；;，,：:\n\r]|[.](?=\s|$)"
)


def normalize_constraints(value: object) -> list[str]:
    """把多种兼容输入形式归一化为去重后的约束文本列表。

    Args:
        value: 约束字符串、递归字符串序列，或带有 text/description 字段的映射。
            不支持的 malformed 值会被忽略，不会被隐式转换成字符串。

    Returns:
        按原顺序去重且去除空白项的约束列表。
    """
    if value is None:
        return []

    if isinstance(value, str):
        candidates: list[object] = [value]
    elif isinstance(value, Mapping):
        if any(key in value for key in ("text", "description", "rule")):
            candidates = [
                value.get("text") or value.get("description") or value.get("rule")
            ]
        elif any(key in value for key in ("items", "constraints", "rules")):
            nested = (
                value.get("items") or value.get("constraints") or value.get("rules")
            )
            candidates = [nested]
        else:
            return []
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        candidates = list(value)
    elif isinstance(value, (set, frozenset)):
        candidates = list(value)
    else:
        return []

    normalized: list[str] = []
    for candidate in candidates:
        if isinstance(candidate, (Mapping, Sequence, set, frozenset)) and not isinstance(
            candidate, (str, bytes, bytearray)
        ):
            nested = normalize_constraints(candidate)
        elif isinstance(candidate, str):
            text = candidate.strip().strip(" \t，,；;。！？!?")
            nested = [text] if text else []
        else:
            # 数字、布尔值、对象实例等 malformed 值不能冒充约束文本。
            nested = []

        for text in nested:
            if text not in normalized:
                normalized.append(text)
    return normalized


def _contains_constraint_marker(text: str) -> bool:
    """判断文本片段是否明显是执行约束。"""
    return _CONSTRAINT_MARKER_RE.search(text) is not None


def _contains_process_echo_marker(text: str) -> bool:
    """判断文本片段是否明显是过程性回声或原始题面标签。"""
    return (
        _PROCESS_ECHO_MARKER_RE.search(text) is not None
        or _RAW_PROBLEM_MARKER_RE.search(text) is not None
    )


def _constraint_marker_start(text: str) -> int | None:
    """返回文本中第一个执行约束 marker 的起始位置。"""
    match = _CONSTRAINT_MARKER_RE.search(text)
    return match.start() if match else None


def _split_clauses(text: str) -> list[str]:
    """按中文和英文常见句末边界拆分文本。"""
    return [part.strip() for part in _CLAUSE_BOUNDARY_RE.split(text) if part.strip()]


def _normalize_public_fragment(text: str) -> str:
    """清理移除约束后相邻的标点，避免公共文本出现孤立分隔符。"""
    cleaned = text.strip()
    cleaned = re.sub(r"^[，,；;。！？!?]+", "", cleaned)
    cleaned = re.sub(r"[，,；;。！？!?]+$", "", cleaned)
    cleaned = re.sub(r"([；;])\s*[，,]+", r"\1", cleaned)
    cleaned = re.sub(r"([，,])\s*[；;]+", r"\1", cleaned)
    cleaned = re.sub(r"([。！？!?])\s*[，,；;]+", r"\1", cleaned)
    cleaned = re.sub(r"([，,；;])\s*\1+", r"\1", cleaned)
    return cleaned.strip()


def _split_embedded_constraint(clause: str) -> tuple[str, list[str]] | None:
    """按 marker 到最近安全边界拆出多个执行约束。

    marker 后的逗号、分号、句末或下一个 marker 都可以作为边界。
    没有边界时，剩余文本整体视为执行片段；这会牺牲少量无法确认的
    公共内容，但不会把过程指令泄漏到 Writer。
    """
    marker_matches = list(_CONSTRAINT_MARKER_RE.finditer(clause))
    if not marker_matches:
        return None

    constraint_spans: list[tuple[int, int, str]] = []
    for index, match in enumerate(marker_matches):
        next_marker_start = (
            marker_matches[index + 1].start()
            if index + 1 < len(marker_matches)
            else len(clause)
        )
        boundary = _EMBEDDED_CONSTRAINT_BOUNDARY_RE.search(
            clause,
            match.end(),
            next_marker_start,
        )
        end = boundary.start() if boundary is not None else next_marker_start
        constraint = clause[match.start() : end].strip(" \t，,；;。！？!?")
        if not constraint:
            return None
        constraint_spans.append((match.start(), end, constraint))

    public_text = clause
    for start, end, _ in reversed(constraint_spans):
        public_text = public_text[:start] + public_text[end:]

    return _normalize_public_fragment(public_text), [
        constraint for _, _, constraint in constraint_spans
    ]


def extract_embedded_constraints(text: str) -> tuple[str, list[str]]:
    """从旧题面字段中提取明显的过程约束。

    Args:
        text: 可能混合公共题面和过程指令的文本。

    Returns:
        `(公共文本, 提取出的约束)`。无法确定边界的 marker 后片段进入约束，
        以避免执行内容泄漏到公共文本。
    """
    if not text:
        return "", []

    public_clauses: list[str] = []
    constraints: list[str] = []
    embedded_clauses = [
        part.strip()
        for part in _EMBEDDED_CONSTRAINT_CLAUSE_BOUNDARY_RE.split(text)
        if part.strip()
    ]
    for clause in embedded_clauses:
        embedded = _split_embedded_constraint(clause)
        if embedded is not None:
            public_fragment, embedded_constraints = embedded
            if public_fragment:
                public_clauses.append(public_fragment)
            constraints.extend(normalize_constraints(embedded_constraints))
            continue

        marker_start = _constraint_marker_start(clause)
        if marker_start is None:
            public_clauses.append(clause)
            continue

        # 兜底时只保留 marker 前的公共前缀，marker 后整体隔离到约束通道。
        public_prefix = clause[:marker_start].strip()
        if public_prefix:
            public_clauses.append(public_prefix)
        constraints.extend(normalize_constraints(clause[marker_start:]))

    public_text = "".join(public_clauses).strip()
    return public_text, normalize_constraints(constraints)


def _remove_constraint_text(text: str, constraints: Sequence[str]) -> str:
    """移除明确约束文本，同时尽量保留同一段中的公共题面。"""
    cleaned = text
    for constraint in constraints:
        if constraint:
            cleaned = re.sub(re.escape(constraint), "", cleaned, flags=re.IGNORECASE)
    return cleaned


def _match_key(text: str) -> str:
    """生成用于中英文标点、大小写和空白差异的匹配键。"""
    return re.sub(r"[\W_]+", "", text.casefold(), flags=re.UNICODE)


def _constraint_anchors(constraints: Sequence[str]) -> list[str]:
    """提取约束中可能出现在改写句里的方法/工具锚点。"""
    anchors: list[str] = []
    for constraint in constraints:
        text = constraint
        for marker in _CONSTRAINT_MARKERS:
            text = re.sub(re.escape(marker), "", text, flags=re.IGNORECASE)
        key = _match_key(text)
        if len(key) >= 2 and key not in anchors:
            anchors.append(key)
    return anchors


def _is_constraint_echo(
    clause: str,
    constraints: Sequence[str],
    *,
    include_rewrites: bool = True,
) -> bool:
    """判断一句材料是否是约束本身或对约束的执行性改写。

    Writer 不应该通过自然语言重述执行禁令。这里使用结构化约束的锚点配合
    中英文约束/改写标记做确定性过滤；题面公共内容仍由
    :func:`render_public_problem_context` 从结构化字段生成，不依赖本函数猜测。
    """
    lowered = clause.casefold()
    normalized_clause = _match_key(clause)
    normalized_constraints = {_match_key(item) for item in constraints if item}
    if normalized_constraints and any(
        item and item in normalized_clause for item in normalized_constraints
    ):
        return True
    if _contains_constraint_marker(clause):
        return True
    if _contains_process_echo_marker(clause):
        return True
    if not include_rewrites:
        return False

    has_rewrite = any(marker.casefold() in lowered for marker in _REWRITE_MARKERS)
    has_restriction = any(
        marker.casefold() in lowered for marker in _RESTRICTION_MARKERS
    )
    if has_rewrite and has_restriction:
        return True
    anchors = _constraint_anchors(constraints)
    return has_rewrite and any(anchor in normalized_clause for anchor in anchors)


def find_constraint_echoes(
    text: str,
    constraints: Sequence[object] | None = None,
) -> list[str]:
    """返回材料中可确定识别的约束/约束改写句，供 trace/eval 使用。"""
    if not text:
        return []
    normalized = normalize_constraints(constraints)
    return [
        clause
        for clause in _split_clauses(text)
        if _is_constraint_echo(clause, normalized)
    ]


def redact_execution_constraints(
    text: str, constraints: Sequence[object] | None = None
) -> str:
    """从下游材料中删除执行约束及其明显的过程性回声。

    Args:
        text: 将要传递给 Writer 的材料。
        constraints: Coordinator 已识别的约束列表。

    Returns:
        不含已知约束和明显约束句的文本。
    """
    if not text:
        return ""

    known_constraints = normalize_constraints(constraints)
    cleaned = _remove_constraint_text(text, known_constraints)

    # 对未知 marker 也做同样的保守处理：marker 后有明确标点时保留可验证的
    # 公共前后缀；没有可靠边界时由 _split_embedded_constraint 丢弃不确定尾部。
    # 原始题面标签和“按要求改用”等过程回声无法安全切分时则整句丢弃。
    public_clauses: list[str] = []
    for clause in _split_clauses(cleaned):
        if _contains_constraint_marker(clause):
            embedded = _split_embedded_constraint(clause)
            public_fragment = embedded[0] if embedded is not None else ""
            if public_fragment and not _is_constraint_echo(
                public_fragment, known_constraints
            ):
                public_clauses.append(public_fragment)
            continue
        if not _is_constraint_echo(clause, known_constraints):
            public_clauses.append(clause)
    cleaned = "".join(public_clauses).strip()

    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned)
    cleaned = re.sub(r"([。！？!?；;])\s*([。！？!?；;])+", r"\1", cleaned)
    cleaned = re.sub(r"([；;])\s*[，,]+", r"\1", cleaned)
    cleaned = re.sub(r"([，,])\s*[；;]+", r"\1", cleaned)
    return cleaned.strip(" \t，,；;")


def _normalized_comparison_chars(text: str) -> tuple[str, list[tuple[int, int]]]:
    """返回规范化字符和每个字符对应的原文范围。"""
    chars: list[str] = []
    spans: list[tuple[int, int]] = []
    for index, char in enumerate(str(text or "")):
        normalized = unicodedata.normalize("NFKC", char).casefold()
        for candidate in normalized:
            if not candidate.isalnum():
                continue
            chars.append(candidate)
            spans.append((index, index + 1))
    return "".join(chars), spans


def _contains_normalized_overlap(
    candidate: str,
    reference_windows: set[str],
    threshold: int = RAW_PROBLEM_OVERLAP_THRESHOLD,
) -> bool:
    """判断候选片段是否包含达到阈值的规范化连续重合。"""
    if len(candidate) < threshold:
        return False
    return any(
        candidate[index : index + threshold] in reference_windows
        for index in range(len(candidate) - threshold + 1)
    )


def _iter_bounded_text_spans(text: str) -> list[tuple[int, int]]:
    """返回由明确标点或换行界定的原文片段范围。"""
    spans: list[tuple[int, int]] = []
    start = 0
    for match in _RAW_ECHO_BOUNDARY_RE.finditer(text):
        if start < match.start():
            spans.append((start, match.start()))
        start = match.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def _remove_normalized_occurrences(text: str, reference_key: str) -> str:
    """删除完整 raw problem 的规范化重合，保留旁边的合法结果。"""
    candidate_key, spans = _normalized_comparison_chars(text)
    if not reference_key or not candidate_key or len(reference_key) > len(candidate_key):
        return text

    removals: list[tuple[int, int]] = []
    search_start = 0
    while True:
        match_start = candidate_key.find(reference_key, search_start)
        if match_start < 0:
            break
        match_end = match_start + len(reference_key)
        removals.append((spans[match_start][0], spans[match_end - 1][1]))
        search_start = match_end
    for start, end in reversed(removals):
        text = text[:start] + text[end:]
    return text


def redact_raw_problem_echoes(
    text: str,
    raw_problem: str | None,
    *,
    threshold: int = RAW_PROBLEM_OVERLAP_THRESHOLD,
) -> str:
    """过滤 raw problem 回声，同时保留短片段、指标和合法结果。

    完整 raw problem 即使出现在带有结果的同一行中也会被移除；除此之外，
    只有被明确标点或换行界定、且与 raw problem 的规范化连续重合达到阈值
    的片段才会被移除。没有清晰边界的混合片段保持原样，避免误删结果。

    Args:
        text: 待交接或落盘的文本。
        raw_problem: 仅用于内存中的比较基准，不会写入返回值。
        threshold: 规范化连续重合的字符阈值。

    Returns:
        过滤后的文本。
    """
    if not text or not raw_problem or threshold <= 0:
        return text

    reference_key, _ = _normalized_comparison_chars(raw_problem)
    if not reference_key:
        return text

    cleaned = _remove_normalized_occurrences(text, reference_key)
    reference_windows = {
        reference_key[index : index + threshold]
        for index in range(len(reference_key) - threshold + 1)
    }
    if not reference_windows:
        return cleaned

    removals: list[tuple[int, int]] = []
    for start, end in _iter_bounded_text_spans(cleaned):
        segment = cleaned[start:end]
        segment_key, _ = _normalized_comparison_chars(segment)
        if (
            len(segment_key) >= threshold
            and segment_key in reference_key
            and _contains_normalized_overlap(
                segment_key,
                reference_windows,
                threshold,
            )
        ):
            removals.append((start, end))
    for start, end in reversed(removals):
        cleaned = cleaned[:start] + cleaned[end:]
    return cleaned


def split_public_questions(
    payload: Mapping[str, object],
) -> tuple[dict[str, object], list[str]]:
    """分离公共题面字段和执行约束，兼容旧的扁平 Coordinator JSON。

    Args:
        payload: Coordinator 返回的扁平 JSON，或包含 `questions` 的包装对象。

    Returns:
        `(公共题面字典, 执行约束列表)`。
    """
    source = dict(payload)
    nested_questions = source.pop("questions", None)
    if isinstance(nested_questions, Mapping):
        public_questions = {
            key: value
            for key, value in nested_questions.items()
            if isinstance(key, str)
        }
        public_questions.update(
            {key: value for key, value in source.items() if key not in {"constraints"}}
        )
    else:
        public_questions = source

    explicit_constraints = normalize_constraints(source.get("constraints"))
    constraints = normalize_constraints(public_questions.pop("constraints", None))
    constraints.extend(explicit_constraints)

    for key, value in list(public_questions.items()):
        if not isinstance(value, str) or key == "ques_count":
            continue
        public_value, embedded = extract_embedded_constraints(value)
        public_questions[key] = public_value
        constraints.extend(embedded)

    return public_questions, normalize_constraints(constraints)


def normalize_coordinator_payload(payload: Mapping[str, object]) -> dict[str, object]:
    """规范化 Coordinator 输出，生成 A2A schema 可直接消费的字典。

    Args:
        payload: 已解析的 Coordinator JSON 对象。

    Returns:
        包含 `questions`、`ques_count` 和独立 `constraints` 的字典。

    Raises:
        ValueError: payload 不是对象或缺少合法的问题数量。
    """
    if not isinstance(payload, Mapping):
        raise ValueError("Coordinator 输出必须是 JSON 对象")

    public_questions, constraints = split_public_questions(payload)
    raw_count = public_questions.get("ques_count", payload.get("ques_count"))
    if raw_count is None or isinstance(raw_count, bool):
        raise ValueError("Coordinator 输出缺少合法的 ques_count")
    if not isinstance(raw_count, (str, int, float)):
        raise ValueError("Coordinator 输出缺少合法的 ques_count")
    try:
        ques_count = int(raw_count)
    except (TypeError, ValueError) as exc:
        raise ValueError("Coordinator 输出缺少合法的 ques_count") from exc

    return {
        "questions": public_questions,
        "ques_count": ques_count,
        "constraints": constraints,
    }


def render_public_problem_context(questions: Mapping[str, object]) -> str:
    """将公共题面渲染为 Writer 可读、且不含约束的文本。"""
    public_questions, _ = split_public_questions(questions)
    ordered_keys: list[str] = []
    for key in ("title", "background"):
        if key in public_questions:
            ordered_keys.append(key)
    ordered_keys.extend(
        sorted(
            (
                key
                for key in public_questions
                if key.startswith("ques") and key != "ques_count"
            ),
            key=lambda key: ((0, int(key[4:])) if key[4:].isdigit() else (1, key)),
        )
    )
    ordered_keys.extend(
        key
        for key in public_questions
        if key not in ordered_keys and key != "ques_count"
    )

    lines = []
    for key in ordered_keys:
        value = public_questions[key]
        if value not in (None, ""):
            lines.append(f"{key}: {value}")
    return "\n".join(lines)


def truncate_handoff_text(text: str, max_chars: int) -> str:
    """按字符上限截断交接文本，并保留明确的截断标记。"""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    keep_chars = max(0, max_chars - len(_TRUNCATION_SUFFIX))
    return text[:keep_chars].rstrip() + _TRUNCATION_SUFFIX


def render_execution_constraints(
    constraints: Sequence[object] | None, max_chars: int = 2000
) -> str:
    """渲染仅供 Modeler/Coder 执行的约束段落。"""
    normalized = normalize_constraints(constraints)
    if not normalized:
        return ""
    text = "执行约束（仅供建模与代码执行，不得写入论文）：\n" + "\n".join(
        f"- {constraint}" for constraint in normalized
    )
    return truncate_handoff_text(text, max_chars)
