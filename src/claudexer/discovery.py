"""Read session metadata and provide a dependency-free terminal picker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
from typing import TextIO

from .session import content_parts


@dataclass
class Candidate:
    path: Path
    id: str
    title: str
    cwd: str
    updated: float
    archived: bool = False
    provider: str = "codex"


def codex_home(override: Path | None = None) -> Path:
    return (override or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")).expanduser()


def read_index(home: Path) -> dict[str, dict]:
    index = {}
    try:
        with (home / "session_index.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                    if isinstance(item, dict) and item.get("id"):
                        index[str(item["id"])] = item
                except ValueError:
                    continue
    except OSError:
        pass
    return index


def discover(home: Path, include_archived: bool = False) -> tuple[list[Candidate], list[str]]:
    index, found, warnings = read_index(home), [], []
    roots = [(home / "sessions", False)]
    if include_archived:
        roots.append((home / "archived_sessions", True))
    for root, archived in roots:
        for path in root.rglob("*.jsonl"):
            try:
                identity, cwd, title = path.stem, "", ""
                # Metadata and the first prompt normally occur near the start.
                # Bound discovery work; full parsing happens after selection.
                with path.open(encoding="utf-8-sig") as stream:
                    for number, line in enumerate(stream):
                        if number >= 200:
                            break
                        try:
                            record = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(record, dict):
                            continue
                        p = record.get("payload", {})
                        if not isinstance(p, dict):
                            continue
                        if record.get("type") == "session_meta":
                            identity = str(p.get("id") or p.get("session_id") or identity)
                            cwd = str(p.get("cwd") or "")
                            title = str(p.get("thread_name") or p.get("title") or "")
                        if record.get("type") == "response_item" and p.get("role") == "user":
                            text, _ = content_parts(p.get("content", []))
                            if text and not text.lstrip().startswith(("# AGENTS.md instructions", "<environment_context>", "<INSTRUCTIONS>")):
                                title = title or " ".join(text.split())[:100]
                                break
                item = index.get(identity, {})
                title = str(item.get("thread_name") or title or identity)
                updated = path.stat().st_mtime
                try:
                    updated = max(updated, datetime.fromisoformat(item["updated_at"].replace("Z", "+00:00")).timestamp())
                except (KeyError, ValueError, TypeError, AttributeError):
                    pass
                found.append(Candidate(path, identity, title, cwd, updated, archived))
            except (OSError, UnicodeError) as exc:
                warnings.append(f"Could not inspect {path.name}: {exc}")
    found.sort(key=lambda c: (c.updated, str(c.path)), reverse=True)
    # Prefer the most recently updated copy if a rollout was moved/duplicated.
    unique = {}
    for candidate in found:
        unique.setdefault(candidate.id, candidate)
    return list(unique.values()), warnings


def terminal_text(text: str) -> str:
    # Session titles are untrusted text; strip terminal control sequences.
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return " ".join("".join(c for c in text if c.isprintable() or c.isspace()).split())


def claude_home(override: Path | None = None) -> Path:
    return (override or Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")).expanduser()


def discover_claude(home: Path) -> tuple[list[Candidate], list[str]]:
    from .claude import parts
    from .session import iter_records

    found, warnings = [], []
    # Nested subagent logs and legacy agent-*.jsonl files are separate sessions.
    for path in (home / "projects").glob("*/*.jsonl"):
        if path.name.startswith("agent-"):
            continue
        try:
            identity, cwd, title, custom, prompt = path.stem, "", "", "", ""
            for record in iter_records(path, [], require_payload=False):
                identity = str(record.get("sessionId") or identity)
                cwd = str(record.get("cwd") or cwd)
                kind = record.get("type")
                if kind in {"ai-title", "summary"}:
                    title = str(record.get("aiTitle") or record.get("summary") or title)
                elif kind == "custom-title":
                    custom = str(record.get("customTitle") or custom)
                elif kind == "user" and not prompt and not record.get("isMeta") and not record.get("isCompactSummary"):
                    message = record.get("message", {})
                    if isinstance(message, dict):
                        text, _ = parts(message.get("content", []))
                        if not text.lstrip().startswith(("<local-command-caveat>", "<local-command-stdout>", "[Request interrupted")):
                            prompt = " ".join(text.split())[:100]
            found.append(Candidate(path, identity, custom or title or prompt or identity, cwd, path.stat().st_mtime, provider="claude"))
        except (OSError, UnicodeError, ValueError) as exc:
            warnings.append(f"Could not inspect {path.name}: {exc}")
    return sorted(found, key=lambda c: c.updated, reverse=True), warnings


def discover_all(codex: Path, claude: Path, source: str = "all", include_archived: bool = False) -> tuple[list[Candidate], list[str]]:
    candidates, warnings = [], []
    if source in {"all", "codex"}:
        found, notes = discover(codex, include_archived)
        candidates.extend(found)
        warnings.extend(notes)
    if source in {"all", "claude"}:
        found, notes = discover_claude(claude)
        candidates.extend(found)
        warnings.extend(notes)
    return sorted(candidates, key=lambda c: c.updated, reverse=True), warnings


def pick(candidates: list[Candidate], stdin: TextIO | None = None, stdout: TextIO | None = None, page_size: int = 10) -> Candidate | None:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    query, page = "", 0
    while True:
        matches = [c for c in candidates if query.casefold() in f"{c.title} {c.cwd} {c.id}".casefold()]
        pages = max(1, (len(matches) + page_size - 1) // page_size)
        page = min(page, pages - 1)
        print(f"\nSessions · {len(matches)} match(es) · page {page + 1}/{pages}", file=stdout)
        for i, candidate in enumerate(matches[page * page_size:(page + 1) * page_size], page * page_size + 1):
            date = datetime.fromtimestamp(candidate.updated).strftime("%Y-%m-%d %H:%M")
            archive = " · archived" if candidate.archived else ""
            print(f" {i:>3}. [{candidate.provider}] {terminal_text(candidate.title)[:90]}{archive}\n      {date} · {terminal_text(candidate.cwd) or 'unknown directory'} · {terminal_text(candidate.id)[:8]}", file=stdout)
        print("Number to export · /text to search · / to clear · n/p pages · q to cancel\n> ", end="", file=stdout, flush=True)
        answer = stdin.readline()
        if not answer or answer.strip().lower() in {"q", "quit"}:
            return None
        answer = answer.strip()
        if answer.startswith("/"):
            query, page = answer[1:], 0
        elif answer.lower() == "n":
            page = min(page + 1, pages - 1)
        elif answer.lower() == "p":
            page = max(0, page - 1)
        elif answer.isdigit() and 1 <= int(answer) <= len(matches):
            return matches[int(answer) - 1]
        else:
            print("Enter a listed number, /search, n, p, or q.", file=stdout)
