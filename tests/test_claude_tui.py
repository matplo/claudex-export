import asyncio
import json

from textual.widgets import DataTable, Input, TextArea

from codex_export.cli import main
from codex_export.discovery import Candidate, discover_all, discover_claude
from codex_export.render import render_html, render_markdown
from codex_export.session import read_session, user_prompts
from codex_export.tui import PreviewScreen, SessionPicker, prompt_preview


def claude(kind, content, uuid, **extra):
    return {"type": kind, "uuid": uuid, "sessionId": "claude-demo", "cwd": "/demo", "timestamp": "2026-09-07T12:00:00Z", "message": {"id": "shared-message-id", "role": kind, "content": content, "stop_reason": "end_turn" if kind == "assistant" else None}, **extra}


def save(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def candidate(tmp_path, count=25):
    records = [claude("user", f"Question {i + 1}?", f"u-{i}") for i in range(count)]
    path = save(tmp_path / "demo.jsonl", records)
    return Candidate(path, "claude-demo", "A demo [not markup]", "/demo", 0, provider="claude")


def test_claude_auto_detection_tools_replays_and_final_color(tmp_path):
    question = claude("user", "Do something", "u")
    records = [
        {"type": "ai-title", "aiTitle": "Claude demo", "sessionId": "claude-demo"},
        question, question,
        claude("user", "Injected context", "meta", isMeta=True),
        claude("assistant", [{"type": "thinking", "thinking": "hidden"}, {"type": "text", "text": "Working"}, {"type": "tool_use", "id": "tool1", "name": "Bash", "input": {"command": "echo hello"}}], "a1"),
        claude("user", [{"type": "tool_result", "tool_use_id": "tool1", "content": [{"type": "text", "text": "hello"}], "is_error": True}], "r"),
        {"type": "system", "subtype": "compact_boundary", "uuid": "compact"},
        claude("user", "Compacted summary", "summary", isCompactSummary=True),
        claude("assistant", [{"type": "text", "text": "Done"}], "a2"),
    ]
    session = read_session(save(tmp_path / "demo.jsonl", records))
    assert session.provider == "claude"
    assert session.title == "Claude demo"
    assert [e.kind for e in session.entries] == ["user", "assistant", "tool", "marker", "assistant"]
    assert session.entries[2].result == "Tool error\nhello"
    assert [e.text for e in user_prompts(session)] == ["Do something"]
    output = render_html(session)
    assert "CLAUDE CODE / SESSION EXPORT" in output
    assert 'class="message assistant final"' in output
    assert "Injected context" not in output and "Compacted summary" not in output
    assert "hidden" not in render_markdown(session)
    # Different UUIDs with the same assistant message ID must survive.
    assert "Working" in output and "Done" in output


def test_claude_json_array_images_and_missing_tool_call(tmp_path):
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="}}
    records = [claude("user", [image], "u"), claude("user", [{"type": "tool_result", "tool_use_id": "missing", "content": "result"}], "r")]
    path = tmp_path / "session.json"
    path.write_text(json.dumps(records))
    session = read_session(path)
    assert session.entries[0].images == ["data:image/png;base64,aGVsbG8="]
    assert len(user_prompts(session)) == 1
    assert session.entries[1].result == "result"
    assert "Unmatched" in session.entries[1].name


def test_discover_claude_main_sessions_titles_and_source_filter(tmp_path):
    home = tmp_path / "claude"
    save(home / "projects/p/main.jsonl", [claude("user", "First prompt", "u"), {"type": "custom-title", "customTitle": "My title"}, {"type": "ai-title", "aiTitle": "Automatic title"}])
    save(home / "projects/p/agent-side.jsonl", [claude("user", "Hidden", "u")])
    save(home / "projects/p/main/subagents/agent-nested.jsonl", [claude("user", "Hidden", "u")])
    found, warnings = discover_claude(home)
    assert not warnings and len(found) == 1
    assert found[0].title == "My title" and found[0].provider == "claude"
    assert discover_all(tmp_path / "codex", home, "codex")[0] == []
    assert len(discover_all(tmp_path / "codex", home)[0]) == 1


def test_prompt_preview_first_last_overlap_empty_and_literal_markup(tmp_path):
    c = candidate(tmp_path)
    preview = prompt_preview(c)
    assert "Question 1?" in preview and "Question 10?" in preview
    assert "Question 11?" not in preview and "Question 15?" not in preview
    assert "Question 16?" in preview and "Question 25?" in preview
    assert "5 middle prompts omitted" in preview
    preview = prompt_preview(candidate(tmp_path, 15))
    assert preview.count("Question 8?") == 1
    assert "middle prompts omitted" not in preview
    empty = candidate(tmp_path, 1)
    save(empty.path, [{"type": "ai-title", "sessionId": "empty", "aiTitle": "No prompts"}])
    assert "No user prompts" in prompt_preview(empty)


def test_tui_search_preview_return_selection_and_cancel(tmp_path):
    async def run():
        c = candidate(tmp_path)
        second = Candidate(c.path, "other", "Second session", "/other", 1, provider="claude")
        app = SessionPicker([c, second])
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()
            assert app.query_one(DataTable).row_count == 2
            await pilot.press("v")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, PreviewScreen)
            assert "Question 25?" in app.screen.query_one(TextArea).text
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("slash")
            await pilot.press("S", "e", "c", "o", "n", "d")
            await pilot.pause()
            assert app.query_one(DataTable).row_count == 1
            await pilot.press("enter", "enter")
        assert app.return_value == second
        app = SessionPicker([c])
        async with app.run_test() as pilot:
            await pilot.press("q")
        assert app.return_value is None
    asyncio.run(run())


def test_tui_empty_search_preview_error_and_export_from_preview(tmp_path):
    async def run():
        c = candidate(tmp_path, 1)
        app = SessionPicker([c])
        async with app.run_test(size=(80, 24)) as pilot:
            app.query_one(Input).value = "nothing matches"
            await pilot.pause()
            await pilot.press("v")
            assert not isinstance(app.screen, PreviewScreen)
            await pilot.press("escape", "v")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("e")
        assert app.return_value == c
        broken = Candidate(tmp_path / "missing.jsonl", "missing", "Missing file", "", 0)
        app = SessionPicker([broken])
        async with app.run_test() as pilot:
            await pilot.press("v")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "Could not preview" in app.screen.query_one(TextArea).text
            await pilot.press("e")
            assert isinstance(app.screen, PreviewScreen)
            await pilot.press("escape", "q")
    asyncio.run(run())


def test_cli_claude_auto_and_explicit_both_formats(tmp_path, monkeypatch):
    c = candidate(tmp_path, 2)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    assert main([str(c.path), "--format", "both", "-o", str(tmp_path / "out")]) == 0
    html = next((tmp_path / "out").glob("*.html")).read_text()
    assert "Claude Code session" in html
    assert main([str(c.path), "--source", "claude", "-o", str(tmp_path / "explicit.html")]) == 0


def test_wrong_explicit_source_fails_without_export(tmp_path):
    path = save(tmp_path / "codex.jsonl", [{"type": "session_meta", "payload": {"id": "codex"}}])
    target = tmp_path / "wrong.html"
    assert main([str(path), "--source", "claude", "-o", str(target)]) == 1
    assert not target.exists()
