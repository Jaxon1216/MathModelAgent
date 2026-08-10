"""任务日志预览：trace 时间线、摘要统计、error log 与 messages 概览。

用法:
    uv run python scripts/preview_logs.py --list
    uv run python scripts/preview_logs.py --latest
    uv run python scripts/preview_logs.py --task-id {task_id}
    uv run python scripts/preview_logs.py --task-id {task_id} --verbose
    uv run python scripts/preview_logs.py --task-id {task_id} --summary --errors
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

# 允许从 backend/ 直接运行 scripts
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.trace_recorder import TraceRecorder  # noqa: E402

TRACES_DIR = PROJECT_ROOT / "logs" / "traces"
MESSAGES_DIR = PROJECT_ROOT / "logs" / "messages"
LOGS_DIR = PROJECT_ROOT / "logs"
WORK_DIR_ROOT = PROJECT_ROOT / "project" / "work_dir"

# compact 模式默认隐藏的高频低信息事件
NOISY_EVENTS = frozenset({"react.turn", "execute.start"})

_recorder = TraceRecorder()


def list_task_ids() -> list[str]:
    """列出 traces 目录下所有 task_id（按修改时间倒序）。"""
    if not TRACES_DIR.exists():
        return []
    files = sorted(TRACES_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.stem for p in files]


def load_trace(task_id: str) -> list[dict[str, Any]]:
    """读取 JSONL trace。"""
    path = TRACES_DIR / f"{task_id}.jsonl"
    if not path.exists():
        print(f"[preview] trace 不存在: {path}", file=sys.stderr)
        return []
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def format_ts(ts: str) -> str:
    """ISO 时间戳转为本地简短显示。"""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%H:%M:%S")
    except ValueError:
        return ts[:19] if len(ts) >= 19 else ts


def format_event_line(ev: dict[str, Any]) -> str:
    """单行 trace 摘要（复用 TraceRecorder 格式化逻辑）。"""
    event = ev.get("event", "")
    phase = ev.get("phase")
    payload = ev.get("payload") or {}
    if event == "llm.response" and ev.get("agent") and "agent" not in payload:
        payload = {**payload, "agent": ev["agent"]}
    summary = _recorder._format_content(event, phase, payload)
    ts = format_ts(ev.get("ts", ""))
    return f"{ts}  {summary}"


def filter_events(
    events: list[dict[str, Any]],
    *,
    verbose: bool,
    event_filter: set[str] | None,
    phase_filter: str | None,
    last_n: int | None,
) -> list[dict[str, Any]]:
    """按模式与过滤器裁剪事件列表。"""
    filtered: list[dict[str, Any]] = []
    for ev in events:
        event = ev.get("event", "")
        phase = ev.get("phase") or ""

        if not verbose and event in NOISY_EVENTS:
            continue
        if event_filter and event not in event_filter:
            continue
        if phase_filter and phase != phase_filter:
            continue
        filtered.append(ev)

    if last_n is not None and last_n > 0:
        filtered = filtered[-last_n:]
    return filtered


def print_timeline(events: list[dict[str, Any]]) -> None:
    """打印 trace 时间线。"""
    if not events:
        print("(无匹配事件)")
        return
    for ev in events:
        print(format_event_line(ev))


def print_summary(task_id: str, events: list[dict[str, Any]]) -> None:
    """打印 trace 汇总面板。"""
    if not events:
        return

    print("\n── 摘要 ──")
    print(f"task_id:    {task_id}")
    print(f"events:     {len(events)}")
    print(f"时间范围:   {format_ts(events[0].get('ts', ''))} → {format_ts(events[-1].get('ts', ''))}")

    by_event = Counter(ev.get("event", "") for ev in events)
    print(f"事件分布:   {dict(by_event.most_common(8))}")

    # phase 进度
    phases: list[str] = []
    for ev in events:
        if ev.get("event") == "phase.end":
            ph = ev.get("phase") or ev.get("payload", {}).get("phase", "")
            ok = ev.get("payload", {}).get("success", True)
            dur = ev.get("payload", {}).get("duration_ms")
            dur_s = f"{dur // 1000}s" if dur else "?"
            phases.append(f"{ph}({'ok' if ok else 'FAIL'},{dur_s})")
    if phases:
        print(f"已完成阶段: {' → '.join(phases)}")
    else:
        last_phase = next(
            (ev.get("phase") for ev in reversed(events) if ev.get("phase")), None
        )
        print(f"当前阶段:   {last_phase or '(未知)'}")

    # 执行错误
    exec_ok = exec_err = 0
    for ev in events:
        if ev.get("event") == "execute.done":
            if ev.get("payload", {}).get("error"):
                exec_err += 1
            else:
                exec_ok += 1
    total_exec = exec_ok + exec_err
    err_rate = f"{exec_err / total_exec:.1%}" if total_exec else "n/a"
    print(f"代码执行:   {exec_ok} ok / {exec_err} err ({err_rate})")

    # LLM token
    llm_events = [ev for ev in events if ev.get("event") == "llm.response"]
    if llm_events:
        total_tok = sum(ev.get("payload", {}).get("total_tokens", 0) for ev in llm_events)
        avg_prompt = sum(ev.get("payload", {}).get("prompt_tokens", 0) for ev in llm_events) / len(
            llm_events
        )
        print(f"LLM 调用:   {len(llm_events)} 次, {total_tok:,} tokens, avg prompt {avg_prompt:,.0f}")

    # subtask.summary
    for ev in events:
        if ev.get("event") == "subtask.summary":
            ph = ev.get("phase", "")
            p = ev.get("payload", {})
            print(
                f"  {ph}: turns={p.get('turns', 0)}, png={p.get('png_count', 0)}, "
                f"retries={p.get('retries', 0)}"
            )

    # work_dir 产物
    work_dir = WORK_DIR_ROOT / task_id
    if work_dir.is_dir():
        pngs = list(work_dir.glob("*.png"))
        has_res = (work_dir / "res.md").exists()
        has_docx = (work_dir / "res.docx").exists()
        print(
            f"work_dir:   {len(pngs)} png, res.md={'✓' if has_res else '✗'}, "
            f"res.docx={'✓' if has_docx else '✗'}"
        )


def find_error_lines(task_id: str, limit: int = 30) -> list[str]:
    """从 logs/*_error.log 中提取与 task_id 相关的 ERROR 行。"""
    if not LOGS_DIR.exists():
        return []

    lines: list[str] = []
    log_files = sorted(LOGS_DIR.glob("*_error.log"), key=lambda p: p.stat().st_mtime, reverse=True)

    for log_path in log_files:
        try:
            content = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in content.splitlines():
            if task_id not in line:
                continue
            if "| ERROR" in line or "任务执行失败" in line or "超过最大" in line:
                lines.append(line.strip())
        if lines:
            break

    return lines[-limit:]


def print_errors(task_id: str, limit: int) -> None:
    """打印 error log 摘要。"""
    lines = find_error_lines(task_id, limit=limit)
    print("\n── Error Log ──")
    if not lines:
        print("(无 ERROR 或未在 error log 中找到该 task_id)")
        return
    for line in lines:
        # 截断过长行
        if len(line) > 200:
            line = line[:200] + "..."
        print(line)


def print_messages_summary(task_id: str, last_n: int) -> None:
    """打印 messages.json 概览。"""
    path = MESSAGES_DIR / f"{task_id}.json"
    print("\n── Messages ──")
    if not path.exists():
        print(f"(不存在: {path})")
        return

    try:
        messages: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"(读取失败: {exc})")
        return

    by_type = Counter(m.get("msg_type", "unknown") for m in messages)
    print(f"总数: {len(messages)}, 类型: {dict(by_type)}")

    # 最近若干条 system / error 类消息
    interesting = [
        m
        for m in messages
        if m.get("msg_type") in ("system", "error")
        or m.get("type") in ("error", "warning", "success")
    ]
    tail = interesting[-last_n:] if interesting else messages[-last_n:]
    print(f"最近 {len(tail)} 条:")
    for m in tail:
        content = (m.get("content") or "")[:120]
        msg_type = m.get("msg_type", "")
        level = m.get("type", "")
        label = level or msg_type
        print(f"  [{label}] {content}")


def parse_event_filter(raw: str | None) -> set[str] | None:
    """解析逗号分隔的 event 过滤器。"""
    if not raw:
        return None
    return {e.strip() for e in raw.split(",") if e.strip()}


def resolve_task_id(args: argparse.Namespace) -> str | None:
    """解析 task_id：--task-id / --latest / --list。"""
    if args.list:
        tasks = list_task_ids()
        if not tasks:
            print("(无 trace 文件)")
            return None
        print("可用 task_id（新 → 旧）:")
        for tid in tasks:
            trace_path = TRACES_DIR / f"{tid}.jsonl"
            mtime = datetime.fromtimestamp(trace_path.stat().st_mtime).strftime(
                "%Y-%m-%d %H:%M"
            )
            lines = sum(1 for _ in trace_path.open(encoding="utf-8"))
            print(f"  {tid}  ({lines} events, {mtime})")
        return None

    if args.latest:
        tasks = list_task_ids()
        if not tasks:
            print("[preview] 无 trace 文件", file=sys.stderr)
            sys.exit(1)
        return tasks[0]

    return args.task_id


def main() -> None:
    parser = argparse.ArgumentParser(
        description="预览任务 logs：trace 时间线 + 摘要 + error/messages"
    )
    parser.add_argument("--task-id", help="任务 ID")
    parser.add_argument("--latest", action="store_true", help="使用最新 trace")
    parser.add_argument("--list", action="store_true", help="列出所有 task_id")
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="显示 react.turn / execute.start 等高频事件"
    )
    parser.add_argument(
        "--summary", "-s", action="store_true", help="打印摘要统计（可与时间线同显）"
    )
    parser.add_argument("--errors", action="store_true", help="打印 error log 中该 task 的 ERROR")
    parser.add_argument("--messages", action="store_true", help="打印 messages.json 概览")
    parser.add_argument("--phase", help="只显示指定 phase，如 eda / ques1")
    parser.add_argument(
        "--event",
        help="只显示指定 event（逗号分隔），如 llm.response,execute.done",
    )
    parser.add_argument("--last", type=int, help="只显示最后 N 条匹配事件")
    parser.add_argument(
        "--error-limit", type=int, default=20, help="error log 最多显示行数（默认 20）"
    )
    parser.add_argument(
        "--message-last", type=int, default=8, help="messages 最近条数（默认 8）"
    )
    args = parser.parse_args()

    task_id = resolve_task_id(args)
    if task_id is None:
        return

    events = load_trace(task_id)
    if not events:
        sys.exit(1)

    event_filter = parse_event_filter(args.event)
    filtered = filter_events(
        events,
        verbose=args.verbose,
        event_filter=event_filter,
        phase_filter=args.phase,
        last_n=args.last,
    )

    show_summary = args.summary or not (args.event or args.phase or args.last)
    show_timeline = True

    if show_summary:
        print_summary(task_id, events)

    if show_timeline:
        print("\n── Trace 时间线 ──")
        print_timeline(filtered)

    if args.errors:
        print_errors(task_id, limit=args.error_limit)

    if args.messages:
        print_messages_summary(task_id, last_n=args.message_last)


if __name__ == "__main__":
    main()
