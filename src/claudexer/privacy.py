"""Deterministic, local redaction for conversation-only exports.

This is best-effort identifier removal, not semantic anonymization of prose.
"""

from dataclasses import replace
from datetime import datetime, timezone
import getpass
import html
import ipaddress
from pathlib import Path
import re
import socket
from urllib.parse import unquote

from .session import Session, iter_records, user_prompts


HOME_USER = re.compile(r"(?:/(?:Users|home)/|[A-Za-z]:[\\/]+Users[\\/]+)([^\\/\s\"'`<>]+)", re.I)
IDENTITY_KEYS = {"username", "user_name", "hostname", "host_name", "computer_name", "email", "user_email", "full_name", "home", "home_dir", "cwd"}


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError):
        return None


def collect_metadata(session: Session) -> None:
    """Read only structured identifiers and timestamp bounds; retain no raw log."""
    moments = []
    values = set()

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key.lower() in IDENTITY_KEYS and isinstance(item, str) and item:
                    values.add(item)
                elif isinstance(item, (dict, list)):
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, (dict, list)):
                    visit(item)

    for record in iter_records(session.source, [], require_payload=False):
        visit(record)
        if moment := timestamp(record.get("timestamp")):
            moments.append(moment)
    if started := timestamp(session.timestamp):
        moments.append(started)
    if moments:
        if not session.timestamp:
            session.timestamp = min(moments).isoformat()
        if len(set(moments)) > 1:
            session.duration_seconds = (max(moments) - min(moments)).total_seconds()
    session.identifiers = sorted(values)


def duration_text(seconds: float | None) -> str:
    if seconds is None:
        return ""
    minutes, seconds = divmod(max(0, int(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return (f"{hours}h " if hours else "") + (f"{minutes}m " if minutes or hours else "") + f"{seconds}s"


class Redactor:
    def __init__(self, session: Session, extra=()):
        values = {session.id, str(session.source), session.cwd, *session.identifiers}
        # Infer usernames from imported session paths as well as the local host.
        for text in [session.title, *values, *(e.text for e in session.entries)]:
            values.update(HOME_USER.findall(text))
        values.update({str(Path.home()), Path.home().name, socket.gethostname()})
        try:
            values.add(getpass.getuser())
            import pwd
            import os
            values.add(pwd.getpwuid(os.getuid()).pw_gecos.split(",")[0])
        except (ImportError, KeyError, OSError):
            pass
        common = {"user", "root", "admin", "home", "codex", "claude", "assistant"}
        self.values = sorted((v for v in values if len(v) >= 3 and v.casefold() not in common), key=len, reverse=True)
        self.extra = [v for v in extra if v]

    def __call__(self, text: str) -> str:
        for _ in range(2):
            text = html.unescape(unquote(text))
        text = re.sub(r"[\w.+%\-]+@[\w.\-]+", "[identity]", text)
        for value in self.values:
            text = re.sub(r"(?<!\w)" + re.escape(value) + r"(?!\w)", "[identity]", text, flags=re.I)
        for value in self.extra:
            text = re.sub(re.escape(value), "[redacted]", text, flags=re.I)
        # Omit all image payloads and destinations, including those in code.
        text = re.sub(r"data:[^\s<>\"'`]+", "[image omitted]", text, flags=re.I)
        text = re.sub(r"(?:https?|ftp|ssh|file)://[^\s<>\"'`]+", "[link removed]", text, flags=re.I)
        text = re.sub(r"[\w.+%\-]+@[\w.\-]+", "[identity]", text)
        text = re.sub(r"\b[\w-]+\.(?:local|lan|internal)\b", "[host]", text, flags=re.I)
        text = re.sub(r'''(?P<q>["'`])(?:[A-Za-z]:[\\/]|~[/\\]|/(?!\s)|\\\\)[^\n]*?(?P=q)''', lambda m: m['q'] + '[path]' + m['q'], text)
        text = re.sub(r'''(?<![\w/<])(?:[A-Za-z]:[\\/]|~[/\\]|\\\\|/(?![/\s]))[^\s<>"'`()\[\]{};,]+''', "[path]", text)
        text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "[id]", text, flags=re.I)
        text = re.sub(r"\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b", "[device]", text, flags=re.I)

        def ip(match):
            try:
                ipaddress.ip_address(match[0])
                return "[address]"
            except ValueError:
                return match[0]

        text = re.sub(r"(?<![\w:])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])|(?<![\w:])[0-9a-fA-F]*:[0-9a-fA-F:]+(?![\w:])", ip, text)
        text = re.sub(r"(?<!\w)(?:\+\d{1,3}[ .-])?(?:\(\d{3}\)[ .-]?|\d{3}[ .-])\d{3}[ .-]\d{4}\b", "[phone]", text)
        return text


def prepare_export(session: Session, *, full: bool = False, redact=()) -> Session:
    if full:
        return session
    # Resolve link/image syntax before redaction changes its delimiters. The
    # import is local because renderers also call this export-policy function.
    from .render import markdown_text
    scrub = Redactor(session, redact)
    prompt_ids = {id(e) for e in user_prompts(session)}
    entries = []
    for entry in session.entries:
        if id(entry) not in prompt_ids and not (entry.kind == "assistant" and entry.phase in {"", "commentary", "final", "final_answer"}):
            continue
        text = scrub(markdown_text(entry.text, omit_links=True))
        if entry.images:
            text += "\n\n[Image omitted]"
        entries.append(replace(entry, text=text, timestamp="", images=[], id="", call_id="", turn="", name="", result=None))
    date = timestamp(session.timestamp)
    title = "Conversation" if session.title in {session.id, session.source.stem} else scrub(markdown_text(session.title, omit_links=True))
    return replace(session, title=title, id="", cwd="", timestamp=date.date().isoformat() if date else "", entries=entries, warnings=[], identifiers=[])
