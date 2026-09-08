"""Command-line interface for Claudex Export and its aliases."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata

from . import __version__
from .discovery import claude_home, codex_home, discover_all, pick, read_index, terminal_text
from .render import render_html, render_markdown
from .session import Session, SessionError, read_session
from .privacy import prepare_export


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Export a local Codex or Claude Code session to offline HTML or Markdown. Omit SESSION_FILE to browse local sessions.")
    result.add_argument("session_file", nargs="?", type=Path, metavar="SESSION_FILE")
    result.add_argument("--format", choices=("html", "md", "both"), default="html", help="output format (default: html)")
    result.add_argument("-o", "--output", type=Path, help="output file; for --format both, an output directory")
    result.add_argument("--full", action="store_true", help="include identifying metadata, original messages, tools, and images (default: sanitized conversation)")
    result.add_argument("--redact", action="append", default=[], metavar="TEXT", help="also redact this name or literal text in sanitized mode; repeatable")
    result.add_argument("--no-tools", action="store_true", help="omit tool calls and results even with --full")
    result.add_argument("--codex-home", type=Path, metavar="DIRECTORY", help="override CODEX_HOME (default: ~/.codex)")
    result.add_argument("--claude-home", type=Path, metavar="DIRECTORY", help="override CLAUDE_CONFIG_DIR (default: ~/.claude)")
    result.add_argument("--source", choices=("all", "codex", "claude"), default="all", help="filter picker or specify input format (default: all / auto-detect)")
    result.add_argument("--plain-picker", action="store_true", help="use the original numbered picker instead of Textual")
    result.add_argument("--include-archived", action="store_true", help="include archived sessions in the picker")
    result.add_argument("--force", action="store_true", help="replace existing output files")
    result.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return result


def slug(text: str, limit: int) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:limit].rstrip("-") or "session"


def destinations(session: Session, format: str, output: Path | None, *, full: bool = False, redact=()) -> list[tuple[Path, str]]:
    public = prepare_export(session, full=full, redact=redact)
    name = f"{slug(public.title, 70)}-{slug(public.id, 80)}" if full else (f"{public.timestamp}-" if public.timestamp else "") + slug(public.title, 70)
    formats = ("html", "md") if format == "both" else (format,)
    if output and format != "both":
        return [(output.expanduser(), format)]
    directory = output.expanduser() if output else Path.cwd()
    if directory.exists() and not directory.is_dir():
        raise SessionError(f"Expected an output directory: {directory}")
    return [(directory / f"{name}.{extension}", extension) for extension in formats]


def write_exports(session: Session, targets: list[tuple[Path, str]], include_tools: bool, force: bool, *, full: bool = False, redact=()) -> list[Path]:
    # Check every destination before writing either format.
    for path, _ in targets:
        if path.resolve() == session.source.resolve() or (path.exists() and os.path.samefile(path, session.source)):
            raise SessionError("Output cannot replace the source session, even with --force.")
        if path.is_symlink():
            raise SessionError(f"Refusing to replace a symbolic link: {path}")
        if path.exists() and (not force or not path.is_file()):
            raise SessionError(f"Output already exists: {path}. Use --force to replace a file.")
    rendered = [(path, render_html(session, include_tools, full=full, redact=redact) if format == "html" else render_markdown(session, include_tools, full=full, redact=redact)) for path, format in targets]
    written = []
    for path, content in rendered:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Stage on the destination filesystem. Link provides exclusive creation
        # without a check/write race; replace handles explicit overwrites.
        descriptor, temporary = tempfile.mkstemp(prefix=".codex-export-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(content)
            if force:
                os.replace(temporary, path)
            else:
                os.link(temporary, path)
            written.append(path.resolve())
        finally:
            Path(temporary).unlink(missing_ok=True)
    return written


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    home = codex_home(args.codex_home)
    try:
        selected = None
        source = args.session_file
        if source is None:
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                raise SessionError("No session file supplied. In a noninteractive terminal, pass a .jsonl or .json path: claudex-export SESSION_FILE")
            candidates, warnings = discover_all(home, claude_home(args.claude_home), args.source, args.include_archived)
            for warning in warnings:
                print(f"Warning: {terminal_text(warning)}", file=sys.stderr)
            if not candidates:
                raise SessionError("No local sessions found. Supply a session file or use --codex-home / --claude-home.")
            if args.plain_picker:
                selected = pick(candidates)
            else:
                from .tui import pick_tui
                selected, args.full = pick_tui(candidates, full=args.full, redact=args.redact)
            if selected is None:
                print("Cancelled; no export written.")
                return 0
            source = selected.path
        session = read_session(source.expanduser(), "auto" if args.source == "all" else args.source)
        indexed = read_index(home).get(session.id, {}).get("thread_name") if session.provider == "codex" else None
        session.title = str(indexed or (selected.title if selected else session.title))
        targets = destinations(session, args.format, args.output, full=args.full, redact=args.redact)
        paths = write_exports(session, targets, not args.no_tools, args.force, full=args.full, redact=args.redact)
        for warning in session.warnings:
            print(f"Warning: {terminal_text(warning)}", file=sys.stderr)
        for path in paths:
            print(f"Exported: {terminal_text(str(path))}")
        return 0
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except (OSError, UnicodeError, SessionError) as exc:
        print(f"Error: {terminal_text(str(exc))}", file=sys.stderr)
        return 1
