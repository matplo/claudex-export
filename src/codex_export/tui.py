"""Textual session browser with on-demand user-prompt previews."""

import asyncio
from datetime import datetime

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, Static, TextArea

from .discovery import Candidate, terminal_text
from .session import read_session, user_prompts


def prompt_preview(candidate: Candidate) -> str:
    session = read_session(candidate.path, candidate.provider)
    prompts = user_prompts(session)
    total = len(prompts)
    lines = [f"{candidate.title}\n{candidate.provider.upper()} · {candidate.cwd}\n{candidate.id}", f"{total} user prompts · first 10 and last 10 (overlap shown once)"]
    indexes = sorted(set(range(min(10, total))) | set(range(max(0, total - 10), total)))
    previous = -1
    for index in indexes:
        if index > previous + 1:
            lines.append(f"── {index - previous - 1} middle prompts omitted ──")
        entry = prompts[index]
        lines.append(f"── Prompt {index + 1} · {entry.timestamp or 'time unavailable'} ──\n{entry.text or '[Image-only prompt]'}" + (f"\n[{len(entry.images)} attached image(s)]" if entry.images else ""))
        previous = index
    if not prompts:
        lines.append("No user prompts recorded in this session.")
    if session.warnings:
        lines.append("Export notes\n" + "\n".join(session.warnings))
    # Keep paragraph formatting, but do not interpret terminal controls/markup.
    return "\n\n".join("".join(c for c in line if c.isprintable() or c in "\n\t") for line in lines)


class PreviewScreen(ModalScreen[Candidate | None]):
    BINDINGS = [Binding("escape,v", "close", "Back", priority=True), Binding("e", "export", "Export session", priority=True)]
    DEFAULT_CSS = """
    PreviewScreen { align: center middle; background: $background 65%; }
    #preview-dialog { width: 94%; height: 92%; border: round #a4c9bb; background: $surface; }
    #preview-title { height: 3; padding: 1 2 0 2; color: #a4c9bb; text-style: bold; }
    #prompts { height: 1fr; margin: 1 2; border: none; }
    #preview-help { height: 2; padding: 0 2; color: $text-muted; }
    """

    def __init__(self, candidate: Candidate):
        super().__init__()
        self.candidate = candidate
        self.ready = False

    def compose(self) -> ComposeResult:
        with Vertical(id="preview-dialog"):
            yield Static("USER PROMPTS", id="preview-title")
            yield TextArea("Loading prompts…", read_only=True, soft_wrap=True, id="prompts")
            yield Static("↑↓ / Page Up/Down: scroll   •   Esc / v: back   •   e: export", id="preview-help")

    def on_mount(self) -> None:
        self.query_one(TextArea).focus()
        self.load_preview()

    @work(exclusive=True)
    async def load_preview(self) -> None:
        try:
            text = await asyncio.to_thread(prompt_preview, self.candidate)
            self.ready = True
        except (OSError, ValueError, UnicodeError) as exc:
            text = f"Could not preview this session:\n{exc}\n\nPress Esc to return to the list."
        self.query_one(TextArea).load_text(text)

    def action_close(self) -> None:
        self.dismiss(None)

    def action_export(self) -> None:
        if self.ready:
            self.dismiss(self.candidate)


class SessionPicker(App[Candidate | None]):
    TITLE = "Claudex Export"
    SUB_TITLE = "Codex + Claude Code"
    BINDINGS = [Binding("v", "preview", "View prompts"), Binding("slash", "search", "Search"), Binding("escape", "back", "Clear / focus list"), Binding("q", "cancel", "Quit"), Binding("ctrl+c", "cancel", "Quit", show=False, priority=True)]
    CSS = """
    Screen { background: #18262b; }
    Header { background: #25413f; }
    #search { margin: 1 2; border: round #779f91; }
    #status { height: 1; margin: 0 2 1 2; color: #a9c8be; }
    DataTable { height: 1fr; margin: 0 2; }
    DataTable > .datatable--cursor { background: #c1ded0; color: #172e2b; text-style: bold; }
    #help { height: 2; margin: 1 2 0 2; color: #b2c7d4; }
    Footer { background: #25413f; }
    """

    def __init__(self, candidates: list[Candidate]):
        super().__init__()
        self.candidates = candidates
        self.matches = candidates

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="Search title, directory, session ID, or provider…  (/ to focus)", id="search")
        yield Static("", id="status", markup=False)
        yield DataTable(id="sessions", cursor_type="row", zebra_stripes=True)
        yield Static("↑↓ navigate   •   Enter export   •   v preview first / last 10 prompts", id="help")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("Source", width=8)
        table.add_column("Updated", width=16)
        table.add_column("Session", width=48)
        table.add_column("Directory", width=42)
        table.add_column("ID", width=8)
        self.refresh_rows("")
        table.focus()

    def refresh_rows(self, query: str) -> None:
        self.matches = [c for c in self.candidates if query.casefold() in f"{c.title} {c.cwd} {c.id} {c.provider}".casefold()]
        table = self.query_one(DataTable)
        table.clear()
        for i, candidate in enumerate(self.matches):
            title = candidate.title + (" (archived)" if candidate.archived else "")
            table.add_row(Text(candidate.provider), Text(datetime.fromtimestamp(candidate.updated).strftime("%Y-%m-%d %H:%M")), Text(terminal_text(title)), Text(terminal_text(candidate.cwd)), Text(terminal_text(candidate.id)[:8]), key=str(i))
        self.query_one("#status", Static).update(f"{len(self.matches)} of {len(self.candidates)} sessions" + (" · No matching sessions; change or clear your search." if not self.matches else ""))

    @on(Input.Changed, "#search")
    def search_changed(self, event: Input.Changed) -> None:
        self.refresh_rows(event.value)

    @on(Input.Submitted, "#search")
    def search_submitted(self) -> None:
        self.query_one(DataTable).focus()

    def selected(self) -> Candidate | None:
        row = self.query_one(DataTable).cursor_row
        return self.matches[row] if 0 <= row < len(self.matches) else None

    @on(DataTable.RowSelected)
    def row_selected(self) -> None:
        if candidate := self.selected():
            self.exit(candidate)

    def action_preview(self) -> None:
        if candidate := self.selected():
            self.push_screen(PreviewScreen(candidate), self.preview_closed)

    def preview_closed(self, candidate: Candidate | None) -> None:
        if candidate:
            self.exit(candidate)
        else:
            self.query_one(DataTable).focus()

    def action_search(self) -> None:
        self.query_one(Input).focus()

    def action_back(self) -> None:
        search = self.query_one(Input)
        if search.value:
            search.value = ""
        self.query_one(DataTable).focus()

    def action_cancel(self) -> None:
        self.exit(None)


def pick_tui(candidates: list[Candidate]) -> Candidate | None:
    return SessionPicker(candidates).run()
