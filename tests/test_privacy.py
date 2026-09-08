import asyncio
import json
from pathlib import Path

from textual.widgets import Input, Static, TextArea

from codex_export.cli import destinations, main
from codex_export.discovery import Candidate
from codex_export.privacy import prepare_export
from codex_export.render import render_html, render_markdown
from codex_export.session import Entry, Session, read_session
from codex_export.tui import SessionPicker


def private_session():
    return Session(Path("/Users/alice/private-session.jsonl"), id="sensitive-session-id",
                   title="Review for Alice on alice-laptop", cwd="/Users/alice/Private Project",
                   timestamp="2026-09-08T12:30:00Z", duration_seconds=125,
                   identifiers=["Alice", "alice-laptop"], warnings=["Private file /Users/alice/bad.jsonl"], entries=[
        Entry("user", "# AGENTS.md instructions for /Users/alice\nprivate instructions"),
        Entry("user", "Please explain this code. Alice uses alice@example.com, 192.168.1.20 and alice-laptop.local.\n`/Users/alice/Private Project/code.py`\nCall +1 415-555-0123.\n[profile](https://example.com/alice?email=alice@example.com)\n```python\nowner = \"alice@example.com\"\n```", images=["data:image/png;base64,aGVsbG8="]),
        Entry("assistant", "Here is my progress.", phase="commentary"),
        Entry("tool", "private command", name="exec", result="private output"),
        Entry("assistant", "internal execution plan", phase="plan"),
        Entry("marker", "internal marker"),
        Entry("assistant", "The answer is 42.", phase="final_answer"),
    ])


def test_default_removes_identifiers_and_internal_content_everywhere():
    session = private_session()
    for output in (render_html(session), render_markdown(session)):
        for private in ("Alice", "alice", "192.168.1.20", "415-555-0123", "sensitive-session-id", "Private Project", "private-session", "private instructions", "private command", "private output", "internal execution plan", "internal marker", "aGVsbG8", "12:30:00"):
            assert private not in output, private
        assert "Please explain this code" in output
        assert "Here is my progress" in output
        assert "The answer is 42" in output
        assert "2m 5s" in output
    assert "2026-09-08" in render_html(session)
    assert '<details class="tool"' not in render_html(session)
    assert "href=\"https://example.com" not in render_html(session)
    assert session.entries[1].images  # Rendering does not mutate the source.
    assert session.cwd == "/Users/alice/Private Project"


def test_full_restores_previous_export_and_no_tools_still_works():
    session = private_session()
    for output in (render_html(session, full=True), render_markdown(session, full=True)):
        for value in ("Alice", "alice@example.com", "sensitive-session-id", "private command", "private output", "aGVsbG8"):
            assert value in output.replace("\\", "")
    assert "private output" not in render_html(session, include_tools=False, full=True)


def test_extra_redactions_paths_addresses_and_filename():
    session = private_session()
    session.entries = [Entry("user", r'Person: Jane Doe; Windows: "C:\Users\Bob Smith\secret.txt"; IPv6: 2001:db8::1; MAC: aa:bb:cc:dd:ee:ff; UUID: 12345678-1234-1234-1234-123456789abc')]
    session.title = "Alice and Jane Doe"
    public = prepare_export(session, redact=["Jane Doe", "Bob Smith"])
    assert all(value not in public.entries[0].text for value in ("Jane Doe", "Bob Smith", "2001:db8::1", "aa:bb", "12345678"))
    paths = destinations(session, "both", Path("exports"), redact=["Jane Doe"])
    assert all("alice" not in p.name and "jane" not in p.name and "sensitive-session-id" not in p.name for p, _ in paths)
    assert paths[0][0].name.startswith("2026-09-08-")


def write_session(path):
    records = [
        {"type": "session_meta", "timestamp": "2026-09-08T12:30:00Z", "payload": {"id": "secret-id", "cwd": "/Users/alice/project", "hostname": "alice-host"}},
        {"type": "response_item", "timestamp": "2026-09-08T12:30:05Z", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "alice asks about alice-host"}]}},
        {"type": "response_item", "timestamp": "2026-09-08T12:32:05Z", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "A useful response."}]}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in records))


def test_cli_default_full_both_formats_and_elapsed(tmp_path):
    path = tmp_path / "session.jsonl"
    write_session(path)
    session = read_session(path)
    assert session.duration_seconds == 125
    assert "alice-host" in session.identifiers
    assert main([str(path), "--format", "both", "-o", str(tmp_path / "safe")]) == 0
    for file in (tmp_path / "safe").iterdir():
        assert "alice" not in file.name + file.read_text()
        assert "secret-id" not in file.read_text()
    assert main([str(path), "--full", "-o", str(tmp_path / "full.html")]) == 0
    assert "/Users/alice/project" in (tmp_path / "full.html").read_text()


def test_tui_f_toggle_in_list_and_preview_and_search(tmp_path):
    path = tmp_path / "session.jsonl"
    write_session(path)
    candidate = Candidate(path, "secret-id", "Alice session", "/Users/alice/project", 0)

    async def check():
        app = SessionPicker([candidate])
        async with app.run_test(size=(100, 32)) as pilot:
            assert not app.full
            await pilot.press("f")
            assert app.full
            await pilot.press("v")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "alice-host" in app.screen.query_one(TextArea).text
            await pilot.press("f")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert not app.full
            assert "alice" not in app.screen.query_one(TextArea).text.lower()
            await pilot.press("escape", "slash", "f")
            assert app.query_one(Input).value == "f"  # Typing does not toggle.
            assert not app.full
            await pilot.press("escape", "f", "enter")
        assert app.return_value == candidate and app.full
        app = SessionPicker([candidate], full=True)
        async with app.run_test() as pilot:
            assert app.full
            await pilot.press("q")
    asyncio.run(check())


def test_relative_image_and_link_destinations_removed_from_title_and_prose():
    session = Session(Path("session.jsonl"), title="[Review](private-person-folder/report)", entries=[Entry("user", "[Read this](private-person-folder/report) ![private image name](personal-photo.png)")])
    for output in (render_html(session), render_markdown(session)):
        assert "private-person-folder" not in output
        assert "personal-photo" not in output
        assert "private image name" not in output
        assert "Read this" in output


def test_cli_uses_tui_full_selection(tmp_path, monkeypatch):
    import sys
    from io import StringIO

    class Terminal(StringIO):
        def isatty(self):
            return True

    path = tmp_path / "session.jsonl"
    write_session(path)
    candidate = Candidate(path, "secret-id", "Alice session", "/Users/alice/project", 0)
    monkeypatch.setattr("codex_export.cli.discover_all", lambda *args: ([candidate], []))
    monkeypatch.setattr("codex_export.tui.pick_tui", lambda candidates, **kwargs: (candidate, True))
    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr(sys, "stdout", Terminal())
    output = tmp_path / "selected.html"
    assert main(["-o", str(output)]) == 0
    assert "alice-host" in output.read_text()
