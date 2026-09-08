from io import StringIO
import json
from pathlib import Path

import pytest

from codex_export.cli import destinations, main, write_exports
from codex_export.discovery import Candidate, codex_home, discover, pick, terminal_text
from codex_export.render import embedded_image, markdown_text, render_html, render_markdown
from codex_export.session import Entry, Session, SessionError, read_session


def record(kind, payload):
    return {"timestamp": "2026-09-07T12:00:00Z", "type": kind, "payload": payload}


def message(role, text, id=None):
    return record("response_item", {"type": "message", "role": role, "id": id, "content": [{"type": "input_text", "text": text}]})


def event(role, text, id=None):
    return record("event_msg", {"type": "item_completed", "item": {"type": "UserMessage" if role == "user" else "AgentMessage", "id": id, "content": [{"type": "Text", "text": text}]}})


def save(tmp_path, records, name="session.jsonl"):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if name.endswith(".json"):
        path.write_text(json.dumps(records), encoding="utf-8")
    else:
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


@pytest.fixture
def records():
    return [
        record("session_meta", {"id": "example-id", "cwd": "/work/demo", "timestamp": "2026-09-07T12:00:00Z"}),
        record("event_msg", {"type": "task_started", "turn_id": "turn-1"}),
        message("developer", "private developer instructions"),
        message("system", "private system instructions"),
        message("user", "Build a **small** exporter", "u1"),
        event("user", "Build a **small** exporter", "different-u1"),
        event("assistant", "Working on it.", "a1"),
        message("assistant", "Working on it.", "a1"),
        record("response_item", {"type": "reasoning", "summary": "private reasoning"}),
        record("response_item", {"type": "function_call", "name": "exec_command", "arguments": '{"cmd": "echo hello"}', "call_id": "call1", "id": "tool1"}),
        record("response_item", {"type": "function_call_output", "call_id": "call1", "output": "hello\n```\n<script>bad()</script>"}),
        record("compacted", {"message": "private summary", "replacement_history": [message("user", "duplicate")]}),
        message("assistant", "Done.\n\n```python\nprint('hello')\n```\n\n| A | B |\n|---|---|\n| 1 | 2 |", "a2"),
    ]


def test_transcript_deduplication_tools_and_omissions(tmp_path, records):
    session = read_session(save(tmp_path, records))
    assert [e.kind for e in session.entries] == ["user", "assistant", "tool", "marker", "assistant"]
    assert session.entries[2].result.startswith("hello")
    assert session.title == "Build a **small** exporter"
    assert session.cwd == "/work/demo"
    output = render_html(session)
    assert "private" not in output
    assert "<table>" in output and 'class="highlight"' in output
    assert "&lt;script&gt;bad()&lt;/script&gt;" in output
    assert '<details class="tool"' in output
    assert '<details class="tool" open' not in output
    assert "hello" not in render_html(session, include_tools=False).split("<section class=\"transcript\"")[0]
    assert '<details class="tool"' not in render_html(session, include_tools=False)


def test_identical_messages_are_not_globally_deduplicated(tmp_path):
    records = []
    for turn in ("one", "two"):
        records.append(record("turn_context", {"turn_id": turn}))
        for i in range(2):
            records.extend([message("user", "Again", f"{turn}-{i}"), event("user", "Again", f"event-{turn}-{i}")])
    assert [e.text for e in read_session(save(tmp_path, records)).entries] == ["Again"] * 4


def test_event_only_legacy_and_command_fallback(tmp_path):
    records = [
        event("user", "Hello"),
        record("event_msg", {"type": "agent_message", "message": "Hello back"}),
        record("event_msg", {"type": "item_completed", "item": {"type": "CommandExecution", "id": "cmd", "command": ["echo", "hello"], "stdout": "hello", "exit_code": 0}}),
        record("event_msg", {"type": "turn_aborted", "reason": "interrupted"}),
    ]
    entries = read_session(save(tmp_path, records)).entries
    assert [e.kind for e in entries] == ["user", "assistant", "tool", "marker"]
    assert entries[2].text == "echo hello"
    assert "Exit code: 0" in entries[2].result


def test_equivalent_command_event_is_suppressed(tmp_path):
    records = [
        record("response_item", {"type": "function_call", "name": "exec_command", "arguments": '{"cmd":"echo hello"}', "call_id": "c"}),
        record("event_msg", {"type": "item_completed", "item": {"type": "CommandExecution", "command": ["echo", "hello"], "stdout": "hello"}}),
        record("response_item", {"type": "function_call_output", "call_id": "c", "output": "hello"}),
    ]
    assert len(read_session(save(tmp_path, records)).entries) == 1


def test_json_single_array_and_unsupported(tmp_path, records):
    assert read_session(save(tmp_path, records, "array.json")).id == "example-id"
    assert len(read_session(save(tmp_path, message("user", "Hi"), "single.json")).entries) == 1
    with pytest.raises(SessionError, match="Unsupported JSON structure"):
        read_session(save(tmp_path, {"messages": []}, "other.json"))
    with pytest.raises(SessionError, match="No valid"):
        read_session(save(tmp_path, []))


def test_corrupt_records_and_incomplete_tail(tmp_path):
    path = save(tmp_path, [message("user", "Hi"), record("future_type", {}), record("response_item", [])])
    with path.open("a") as stream:
        stream.write('broken\n{"type":')
    session = read_session(path)
    assert session.entries[0].text == "Hi"
    assert any("line 4" in w for w in session.warnings)
    assert any("incomplete final" in w for w in session.warnings)
    assert any("future_type" in w for w in session.warnings)
    assert any("payload must" in w for w in session.warnings)


PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="


def test_html_images_links_escaping_and_no_remote_assets():
    session = Session(Path("unused.jsonl"), title="<script>title</script>", entries=[Entry("user", text='<script>alert(1)</script>\n\n[bad](javascript:alert%281%29) [good](https://example.com)\n\n![remote](https://example.com/image.png)', images=[PNG, "/tmp/private.png", "data:image/svg+xml;base64,PHN2Zz4="])])
    output = render_html(session)
    assert '<script>alert(1)</script>' not in output
    assert '&lt;script&gt;title&lt;/script&gt;' in output
    assert 'href="javascript:' not in output
    assert 'href="https://example.com"' in output
    assert 'src="https://' not in output
    assert f'src="{PNG}"' in output
    assert 'src="/tmp/' not in output
    assert 'src="data:image/svg' not in output
    assert "Content-Security-Policy" in output
    assert 'src="' not in output.split("<script>")[-1]
    assert not embedded_image("data:image/png;base64,?")


def test_markdown_preserves_code_and_uses_safe_fences(tmp_path, records):
    session = read_session(save(tmp_path, records))
    output = render_markdown(session)
    assert "**small**" in output
    assert "```python\nprint('hello')\n```" in output
    assert "````\nhello\n```" in output
    assert "private" not in output
    assert "**Input**" not in render_markdown(session, False)


def test_discovery_titles_order_archives_and_index(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    save(home / "sessions" / "2026", [record("session_meta", {"id": "first", "cwd": "/a"}), message("user", "First prompt")])
    save(home / "sessions", [record("session_meta", {"id": "second"}), message("user", "# AGENTS.md instructions for /a"), message("user", "Fallback title")], "second.jsonl")
    save(home / "archived_sessions", [record("session_meta", {"id": "archive"})])
    (home / "session_index.jsonl").write_text(json.dumps({"id": "first", "thread_name": "Indexed title", "updated_at": "2099-01-01T00:00:00Z"}) + "\nbroken\n")
    candidates, warnings = discover(home)
    assert not warnings
    assert len(candidates) == 2
    assert candidates[0].title == "Indexed title"
    assert candidates[1].title == "Fallback title"
    assert len(discover(home, True)[0]) == 3
    assert discover(tmp_path / "missing")[0] == []
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-claude"))
    assert codex_home() == home
    assert codex_home(tmp_path) == tmp_path


def test_picker_search_pagination_invalid_and_cancel():
    candidates = [Candidate(Path(str(i)), f"id-{i}", f"Task {i}", "/work", i) for i in range(13)]
    output = StringIO()
    assert pick(candidates, StringIO("n\np\ninvalid\n/Task 12\n1\n"), output).id == "id-12"
    assert "page 2/2" in output.getvalue()
    assert pick(candidates, StringIO("q\n"), StringIO()) is None
    assert pick(candidates, StringIO(""), StringIO()) is None
    assert "\x1b" not in terminal_text("\x1b[31mUnsafe\nTitle")


def test_cli_formats_overwrite_source_protection_and_noninteractive(tmp_path, records, capsys, monkeypatch):
    source = save(tmp_path, records)
    original = source.read_bytes()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty"))
    output = tmp_path / "out"
    assert main([str(source), "--format", "both", "-o", str(output)]) == 0
    assert len(list(output.glob("*.html"))) == 1
    assert len(list(output.glob("*.md"))) == 1
    assert main([str(source), "--format", "both", "-o", str(output)]) == 1
    assert main([str(source), "--format", "both", "-o", str(output), "--force"]) == 0
    assert main([str(source), "-o", str(source), "--force"]) == 1
    assert source.read_bytes() == original
    monkeypatch.setattr("sys.stdin", StringIO())
    assert main([]) == 1
    assert "noninteractive" in capsys.readouterr().err


def test_destinations_sanitize_and_both_preflight(tmp_path, records):
    session = read_session(save(tmp_path, records))
    session.title = "../../Bad / Title <script>"
    targets = destinations(session, "both", tmp_path / "exports")
    assert all(p.parent == tmp_path / "exports" for p, _ in targets)
    targets[1][0].parent.mkdir()
    targets[1][0].write_text("keep")
    with pytest.raises(SessionError, match="already exists"):
        write_exports(session, targets, True, False)
    assert not targets[0][0].exists()
    assert targets[1][0].read_text() == "keep"


def test_duplicate_record_ids_and_output_before_call(tmp_path):
    call = record("response_item", {"type": "custom_tool_call", "id": "tc", "name": "tool", "call_id": "c", "input": "input"})
    result = record("response_item", {"type": "custom_tool_call_output", "call_id": "c", "output": "result"})
    session = read_session(save(tmp_path, [result, call, call]))
    assert len(session.entries) == 1
    assert session.entries[0].result == "result"


def test_replayed_tool_call_does_not_lose_result(tmp_path):
    call = record("response_item", {"type": "function_call", "id": "tc", "name": "tool", "call_id": "c", "arguments": "{}"})
    result = record("response_item", {"type": "function_call_output", "call_id": "c", "output": "result"})
    session = read_session(save(tmp_path, [call, call, result]))
    assert len(session.entries) == 1
    assert session.entries[0].result == "result"


def test_orphan_results_keep_record_order(tmp_path):
    result = record("response_item", {"type": "function_call_output", "call_id": "unknown", "output": "result"})
    entries = read_session(save(tmp_path, [message("user", "Before"), result, message("assistant", "After")])).entries
    assert [e.kind for e in entries] == ["user", "tool", "assistant"]


def test_legacy_tool_events_merge_results_and_compaction(tmp_path):
    records = [
        record("response_item", {"type": "function_call", "name": "exec_command", "call_id": "cmd", "arguments": '{"cmd":"echo hi"}'}),
        record("event_msg", {"type": "exec_command_end", "call_id": "cmd", "command": ["echo", "hi"], "stdout": "hi", "exit_code": 0}),
        record("response_item", {"type": "web_search_call", "action": {"query": "example"}}),
        record("event_msg", {"type": "web_search_end", "action": {"query": "example"}}),
        record("event_msg", {"type": "context_compacted"}),
        record("compacted", {"message": "summary"}),
    ]
    session = read_session(save(tmp_path, records))
    assert not session.warnings
    assert [e.kind for e in session.entries] == ["tool", "tool", "marker"]
    assert "hi" in session.entries[0].result


@pytest.mark.parametrize("source", [
    "![remote](https://example.com/a(b).png)",
    "![remote][pic]\n\n[pic]: https://example.com/image.png",
    "![remote](<https://example.com/image.png> 'title')",
    "[![remote](https://example.com/image.png)](https://example.com)",
])
def test_markdown_remote_images_become_placeholders(source):
    result = markdown_text(source)
    assert "Image reference" in result
    assert '<img ' not in __import__("markdown_it").MarkdownIt().render(result)


@pytest.mark.parametrize("source", [
    "[bad](javascript:alert(1))",
    "[bad](jav&#x61;script:alert(1))",
    "[bad][x]\n\n[x]: javascript:alert(1)",
    "<javascript:alert(1)>",
])
def test_markdown_unsafe_links_removed(source):
    assert "unsafe link omitted" in markdown_text(source)


def test_markdown_references_raw_html_and_code():
    output = markdown_text('[**good**][x]\n\n[x]: https://example.com/a(b) "Title"\n\n<script>alert(1)</script>\n\n`<b>literal</b>`\n\n```html\n<script>code()</script>\n```')
    assert '[**good**](<https://example.com/a(b)> "Title")' in output
    assert "[x]:" not in output
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in output
    assert "`<b>literal</b>`" in output
    assert "```html\n<script>code()</script>\n```" in output


def test_inline_embedded_image_is_rendered():
    session = Session(Path("unused.jsonl"), entries=[Entry("user", text=f"![tiny]({PNG})")])
    assert f'src="{PNG}"' in render_html(session)
    assert f"(<{PNG}>)" in render_markdown(session)


def test_code_blocks_have_valid_html_structure(tmp_path, records):
    from html.parser import HTMLParser

    class CodeStructure(HTMLParser):
        depth = 0
        count = 0

        def handle_starttag(self, tag, attrs):
            if tag == "pre":
                assert self.depth == 0, "Preformatted blocks must not nest"
                self.depth += 1
                self.count += 1
            if tag == "div":
                assert self.depth == 0, "Block containers must not be inside pre"

        def handle_endtag(self, tag):
            if tag == "pre":
                self.depth -= 1

    checker = CodeStructure()
    checker.feed(render_html(read_session(save(tmp_path, records))))
    assert checker.count >= 3
    assert checker.depth == 0


def test_cli_no_sessions_cancellation_and_selection(tmp_path, monkeypatch, capsys):
    class Terminal(StringIO):
        def isatty(self):
            return True

    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr("sys.stdin", Terminal("q\n"))
    monkeypatch.setattr("sys.stdout", Terminal())
    assert main(["--plain-picker", "--source", "codex"]) == 1
    assert "No local sessions" in capsys.readouterr().err
    save(home / "sessions", [message("user", "Pick me")])
    assert main(["--plain-picker", "--source", "codex"]) == 0
    monkeypatch.setattr("sys.stdin", Terminal("1\n"))
    output = tmp_path / "picked.html"
    assert main(["--plain-picker", "--source", "codex", "-o", str(output)]) == 0
    assert "Pick me" in output.read_text()
