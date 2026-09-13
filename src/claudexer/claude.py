"""Normalize Claude Code's local JSONL records into the shared transcript."""

from collections import Counter
from pathlib import Path

from .session import Entry, Session, SessionError, fallback_title, iter_records, pretty


def parts(content) -> tuple[str, list[str]]:
    if isinstance(content, str):
        return content, []
    text, images = [], []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text.append(str(block.get("text", "")))
        elif kind == "image":
            source = block.get("source", {})
            if isinstance(source, dict):
                images.append(f"data:{source.get('media_type', '')};base64,{source.get('data', '')}" if source.get("type") == "base64" else str(source.get("url", "")))
        elif kind not in {"thinking", "redacted_thinking", "tool_use", "tool_result"}:
            text.append(f"[Unsupported content: {kind or 'unknown'}]")
    return "\n\n".join(text), images


def read_claude_session(path: Path) -> Session:
    session = Session(source=path, id=path.stem, provider="claude")
    seen, calls, results = set(), {}, {}
    ignored = Counter()
    metadata = {"attachment", "file-history-snapshot", "file-history-delta", "queue-operation", "mode", "permission-mode", "bridge-session", "agent-name", "last-prompt", "atis-latch", "pr-link", "cost-state", "progress"}
    count, custom_title = 0, ""
    for position, record in enumerate(iter_records(path, session.warnings, require_payload=False)):
        if "payload" in record:
            raise SessionError("This file contains Codex records. Omit --source claude to auto-detect its format.")
        count += 1
        kind = record.get("type")
        session.id = str(record.get("sessionId") or session.id)
        session.cwd = str(record.get("cwd") or session.cwd)
        timestamp = str(record.get("timestamp") or "")
        if timestamp and not session.timestamp:
            session.timestamp = timestamp
        if kind in {"ai-title", "custom-title", "summary"}:
            title = record.get("customTitle") or record.get("aiTitle") or record.get("summary")
            if title:
                session.title = str(title)
                if kind == "custom-title":
                    custom_title = str(title)
            continue
        identity = str(record.get("uuid") or "")
        if identity:
            if identity in seen:
                continue
            seen.add(identity)
        common = dict(timestamp=timestamp, position=position, id=identity)
        if kind == "system":
            if record.get("subtype") == "compact_boundary":
                session.entries.append(Entry(kind="marker", text="Context compacted", **common))
            continue
        if kind == "continued-in":
            session.entries.append(Entry(kind="marker", text=f"Conversation continued in session {record.get('continuedInSessionId', 'unknown')} (separate export).", **common))
            continue
        if kind in metadata:
            continue
        if kind not in {"user", "assistant"}:
            ignored[str(kind)] += 1
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            session.warnings.append(f"Skipped Claude record {position + 1}: message must be an object.")
            continue
        if record.get("isMeta") or record.get("isCompactSummary"):
            continue
        content = message.get("content", [])
        blocks = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
        # A Claude message ID may span multiple records/blocks. Only record UUIDs
        # identify replayed records; deduplicating message IDs loses content.
        phase = "final_answer" if message.get("stop_reason") in {"end_turn", "stop_sequence"} else "commentary"
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type in {"thinking", "redacted_thinking"}:
                continue
            if block_type == "tool_use":
                call_id = str(block.get("id") or "")
                entry = Entry(kind="tool", name=str(block.get("name") or "Tool"), text=pretty(block.get("input", {})), call_id=call_id, **common)
                if call_id and call_id in calls:
                    continue
                if call_id:
                    calls[call_id] = entry
                session.entries.append(entry)
            elif block_type == "tool_result":
                text, images = parts(block.get("content", ""))
                if block.get("is_error"):
                    text = "Tool error\n" + text
                entry = Entry(kind="tool", name="Unmatched tool result", result=text, images=images, call_id=str(block.get("tool_use_id") or ""), **common)
                if entry.call_id:
                    results[entry.call_id] = entry
                else:
                    session.entries.append(entry)
            else:
                text, images = parts([block])
                if text or images:
                    # Adjacent content blocks in the same record form one message.
                    previous = session.entries[-1] if session.entries else None
                    if previous and previous.kind == kind and previous.position == position:
                        previous.text = "\n\n".join(filter(None, (previous.text, text)))
                        previous.images.extend(images)
                    else:
                        session.entries.append(Entry(kind=kind, text=text, images=images, phase=phase if kind == "assistant" else "", **common))
    if not count:
        raise SessionError("No valid Claude Code session records found.")
    for call_id, result in results.items():
        if call_id in calls:
            calls[call_id].result = result.result
            calls[call_id].images.extend(result.images)
        else:
            session.entries.append(result)
    session.entries.sort(key=lambda e: e.position)
    session.title = custom_title or session.title or fallback_title(session)
    for kind, number in sorted(ignored.items()):
        session.warnings.append(f"Skipped {number} unsupported Claude {kind} record(s).")
    return session
