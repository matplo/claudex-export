"""Read rollout records without executing tools or opening referenced resources."""

from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
import json
from pathlib import Path
import shlex
from typing import Any


class SessionError(ValueError):
    pass


@dataclass
class Entry:
    kind: str
    text: str = ""
    timestamp: str = ""
    phase: str = ""
    name: str = ""
    result: str | None = None
    images: list[str] = field(default_factory=list)
    id: str = ""
    call_id: str = ""
    turn: str = ""
    origin: str = "native"
    position: int = 0
    is_prompt: bool = True


@dataclass
class Session:
    source: Path
    id: str = ""
    title: str = ""
    timestamp: str = ""
    cwd: str = ""
    entries: list[Entry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    provider: str = "codex"
    duration_seconds: float | None = None
    identifiers: list[str] = field(default_factory=list, repr=False)


def pretty(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def canonical(value: str) -> str:
    try:
        return json.dumps(json.loads(value), sort_keys=True, ensure_ascii=False)
    except (ValueError, TypeError):
        return value.strip()


def content_parts(content: Any) -> tuple[str, list[str]]:
    if isinstance(content, str):
        return content, []
    texts, images = [], []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        kind = str(part.get("type", "")).lower()
        if kind in {"text", "input_text", "output_text"}:
            texts.append(str(part.get("text", "")))
        elif kind in {"image", "input_image", "image_url"}:
            ref = part.get("image_url", part.get("url", part.get("path", "")))
            if isinstance(ref, dict):
                ref = ref.get("url", "")
            images.append(str(ref))
        else:
            texts.append(f"[Unsupported content: {kind or 'unknown'}]")
    return "\n\n".join(texts), images


def iter_records(path: Path, warnings: list[str], require_payload: bool = True):
    """Stream JSONL, or accept a JSON record/array with the same schema."""
    if path.suffix.lower() not in {".json", ".jsonl"}:
        raise SessionError("Input must be a .jsonl or .json session file.")
    with path.open(encoding="utf-8-sig") as stream:
        if path.suffix.lower() == ".json":
            try:
                data = json.load(stream)
            except ValueError as exc:
                raise SessionError(f"Invalid JSON: {exc}") from exc
            records = data if isinstance(data, list) else [data]
            if not records or any(not isinstance(r, dict) or "type" not in r or (require_payload and "payload" not in r) for r in records):
                raise SessionError("Unsupported JSON structure: expected a Codex record with type and payload, or an array of those records.")
            yield from records
            return
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                suffix = " (possibly an incomplete final write)" if not line.endswith("\n") else ""
                warnings.append(f"Skipped malformed JSON at line {number}{suffix}.")
                continue
            if not isinstance(record, dict) or "type" not in record or (require_payload and "payload" not in record):
                warnings.append(f"Skipped unsupported record at line {number}.")
                continue
            yield record


def message_entry(payload: dict, **kwargs) -> Entry | None:
    role = payload.get("role")
    if role not in {"user", "assistant"}:
        return None
    text, images = content_parts(payload.get("content", []))
    if not text and not images:
        return None
    return Entry(kind=role, text=text, images=images, phase=str(payload.get("phase") or ""), **kwargs)


def event_entry(item: dict, **kwargs) -> Entry | None:
    kind = item.get("type")
    if kind in {"UserMessage", "AgentMessage"}:
        return message_entry({**item, "role": "user" if kind == "UserMessage" else "assistant"}, **kwargs)
    if kind == "CommandExecution":
        command = item.get("command", "")
        command = shlex.join(map(str, command)) if isinstance(command, list) else str(command)
        result = item.get("formatted_output") or item.get("aggregated_output") or "\n".join(str(item.get(k) or "") for k in ("stdout", "stderr"))
        if item.get("exit_code") is not None:
            result += f"\nExit code: {item['exit_code']}"
        return Entry(kind="tool", name="Command execution", text=command, result=result, **kwargs)
    if kind == "McpToolCall":
        return Entry(kind="tool", name=f"{item.get('server', '')}.{item.get('tool', '')}".strip("."), text=pretty(item.get("arguments", {})), result=pretty(item.get("result")), **kwargs)
    if kind == "FileChange":
        return Entry(kind="tool", name="File changes", text=pretty(item.get("changes", {})), result=pretty({k: item[k] for k in ("status", "stdout", "stderr") if k in item}), **kwargs)
    if kind == "ImageView":
        return Entry(kind="tool", name="Image view", images=[str(item.get("path", ""))], **kwargs)
    if kind == "Extension":
        return Entry(kind="tool", name=str(item.get("kind") or "Extension"), text=pretty(item.get("action", item.get("query", ""))), result=pretty(item.get("results", [])), **kwargs)
    if kind == "Plan":
        return Entry(kind="assistant", text=str(item.get("text", "")), phase="plan", **kwargs)
    if kind == "ContextCompaction":
        return Entry(kind="marker", text="Context compacted", **kwargs)
    return None


def signatures(entry: Entry) -> list[tuple]:
    """Cross-stream matching only; equal messages in one stream stay distinct."""
    keys = []
    for identity in (entry.id, entry.call_id):
        if identity:
            keys.append(("id", identity))
    if entry.kind in {"user", "assistant", "marker"}:
        keys.append(("content", entry.turn, entry.kind, entry.text.strip(), tuple(entry.images)))
    elif entry.kind == "tool":
        keys.append(("tool", entry.turn, entry.name, canonical(entry.text)))
        if entry.origin == "native":
            try:
                args = json.loads(entry.text)
                command = args.get("cmd", args.get("command")) if isinstance(args, dict) else None
                if command is not None:
                    command = shlex.join(map(str, command)) if isinstance(command, list) else str(command)
                    keys.append(("command", entry.turn, command))
            except ValueError:
                pass
        elif entry.name == "Command execution":
            keys.append(("command", entry.turn, entry.text))
    return keys


def deduplicate(entries: list[Entry]) -> list[Entry]:
    candidates = defaultdict(deque)
    for index, entry in enumerate(entries):
        if entry.origin == "native":
            for key in signatures(entry):
                candidates[key].append(index)
    used, seen_ids, output = set(), set(), []
    for entry in entries:
        if entry.id:
            identity = (entry.origin, entry.kind, entry.id)
            if identity in seen_ids:
                continue
            seen_ids.add(identity)
        if entry.origin == "event":
            matched = False
            for key in signatures(entry):
                queue = candidates[key]
                while queue and queue[0] in used:
                    queue.popleft()
                if queue:
                    index = queue.popleft()
                    used.add(index)
                    # Completion events sometimes have the only recorded result.
                    if entries[index].result is None and entry.result is not None:
                        entries[index].result = entry.result
                    matched = True
                    break
            if matched:
                continue
        output.append(entry)
    return output


def fallback_title(session: Session) -> str:
    for entry in user_prompts(session):
        if entry.text.strip():
            return " ".join(entry.text.split())[:100]
    return session.id or session.source.stem


def read_codex_session(path: Path) -> Session:
    session = Session(source=path, id=path.stem)
    turn = ""
    pending: dict[str, list[Entry]] = defaultdict(list)
    outputs: dict[str, Entry] = {}
    ignored = Counter()
    count = 0
    ignored_top = {"world_state", "token_usage_record"}
    ignored_events = {"token_count", "task_started", "task_complete", "thread_settings_applied", "agent_reasoning", "agent_reasoning_raw_content", "reasoning_content_delta", "agent_message_delta", "user_message_delta"}
    for position, record in enumerate(iter_records(path, session.warnings)):
        count += 1
        top = record.get("type")
        p = record.get("payload")
        if not isinstance(p, dict):
            session.warnings.append(f"Skipped record {position + 1}: payload must be an object.")
            continue
        timestamp = str(record.get("timestamp") or "")
        kind = p.get("type")
        if top == "session_meta":
            session.id = str(p.get("id") or p.get("session_id") or session.id)
            session.timestamp = str(p.get("timestamp") or timestamp)
            session.cwd = str(p.get("cwd") or "")
            session.title = str(p.get("thread_name") or p.get("title") or "")
            continue
        if top == "turn_context" or (top == "event_msg" and kind == "task_started"):
            turn = str(p.get("turn_id") or turn)
            continue
        common = dict(timestamp=timestamp, turn=str(p.get("turn_id") or turn), position=position)
        entry = None
        if top == "response_item":
            common.update(id=str(p.get("id") or ""), call_id=str(p.get("call_id") or ""))
            if kind == "message":
                entry = message_entry(p, **common)
            elif kind in {"function_call", "custom_tool_call", "tool_search_call", "web_search_call"}:
                entry = Entry(kind="tool", name=str(p.get("name") or "Tool"), text=pretty(p.get("arguments", p.get("input", ""))), **common)
                if kind == "web_search_call":
                    entry.name, entry.text = "web.search", pretty(p.get("action", {}))
                elif kind == "tool_search_call":
                    entry.name = "tool_search"
                if entry.call_id:
                    pending[entry.call_id].append(entry)
            elif kind in {"function_call_output", "custom_tool_call_output", "tool_search_output"}:
                call_id = common["call_id"]
                result = pretty(p.get("output", p.get("tools", "")))
                if call_id:
                    outputs[call_id] = Entry(kind="tool", name="Unmatched tool result", result=result, **common)
                else:
                    entry = Entry(kind="tool", name="Unmatched tool result", result=result, **common)
            elif kind not in {"reasoning", "compaction", "ghost_snapshot"}:
                ignored[f"response_item/{kind}"] += 1
        elif top == "event_msg":
            if kind == "item_completed":
                item = p.get("item", {})
                if isinstance(item, dict):
                    entry = event_entry(item, origin="event", id=str(item.get("id") or ""), call_id=str(item.get("call_id") or ""), **common)
                    if entry is None and item.get("type") != "Reasoning":
                        ignored[f"item_completed/{item.get('type')}"] += 1
            elif kind in {"user_message", "agent_message"}:
                entry = Entry(kind="user" if kind == "user_message" else "assistant", text=str(p.get("message", "")), images=[str(x) for x in (p.get("images") or [])], origin="event", **common)
            elif kind in {"exec_command_end", "patch_apply_end", "view_image_tool_call", "web_search_end"}:
                item_type = {"exec_command_end": "CommandExecution", "patch_apply_end": "FileChange", "view_image_tool_call": "ImageView", "web_search_end": "Extension"}[kind]
                entry = event_entry({**p, "type": item_type, "kind": "web.search"}, origin="event", call_id=str(p.get("call_id") or ""), **common)
            elif kind == "context_compacted":
                entry = Entry(kind="marker", text="Context compacted", origin="event", **common)
            elif kind == "thread_rolled_back":
                entry = Entry(kind="marker", text=f"Session rolled back by {p.get('num_turns', 'unknown')} turn(s); earlier recorded messages are retained in this export.", **common)
            elif kind == "turn_aborted":
                entry = Entry(kind="marker", text="Turn interrupted" + (f": {p['reason']}" if p.get("reason") else ""), **common)
            elif kind not in ignored_events:
                ignored[f"event_msg/{kind}"] += 1
        elif top == "compacted":
            # Replacement history is a summary, not a new copy of the conversation.
            entry = Entry(kind="marker", text="Context compacted", **common)
        elif top not in ignored_top:
            ignored[str(top)] += 1
        if entry:
            session.entries.append(entry)
    if not count:
        raise SessionError("No valid Codex session records found.")
    for call_id, output in outputs.items():
        if call_id in pending:
            for entry in pending[call_id]:
                entry.result = output.result
        else:
            session.entries.append(output)
    session.entries.sort(key=lambda entry: entry.position)
    session.entries = deduplicate(session.entries)
    session.title = session.title or fallback_title(session)
    for kind, number in sorted(ignored.items()):
        session.warnings.append(f"Skipped {number} unsupported {kind} record(s).")
    return session


def read_session(path: Path, provider: str = "auto") -> Session:
    path = Path(path)
    if provider not in {"auto", "codex", "claude"}:
        raise SessionError(f"Unknown session source: {provider}")
    if provider == "auto":
        for record in iter_records(path, [], require_payload=False):
            if "payload" in record:
                provider = "codex"
                break
            if "sessionId" in record or (record.get("type") in {"user", "assistant"} and isinstance(record.get("message"), dict)):
                provider = "claude"
                break
        else:
            # Preserve Codex's diagnostics for empty or future rollout records.
            provider = "codex"
    if provider == "claude":
        from .claude import read_claude_session
        session = read_claude_session(path)
    else:
        session = read_codex_session(path)
    from .privacy import collect_metadata
    collect_metadata(session)
    return session


def user_prompts(session: Session) -> list[Entry]:
    """Actual user inputs for previews, excluding injected context and results."""
    return [entry for entry in session.entries if entry.kind == "user" and entry.is_prompt
            and not entry.text.lstrip().startswith(("# AGENTS.md instructions", "<environment_context>", "<INSTRUCTIONS>", "<user_shell_command>", "<local-command-caveat>", "<local-command-stdout>", "[Request interrupted"))]
