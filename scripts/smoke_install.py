"""Run with python -I after installing a wheel, outside source imports."""

from importlib.metadata import distribution
from importlib.resources import files
from pathlib import Path
from tempfile import TemporaryDirectory

from codex_export import __version__
from codex_export.cli import main
from codex_export.render import render_html, render_markdown
from codex_export.session import Entry, Session
from codex_export.tui import SessionPicker


package = distribution("claudex-export")
assert package.version == __version__
assert {"claudex-export", "claude-export", "codex-export", "session-export"} <= {entry.name for entry in package.entry_points}
assert files("codex_export").joinpath("style.css").is_file()
assert files("codex_export").joinpath("controls.js").is_file()
for provider in ("codex", "claude"):
    session = Session(Path("smoke.jsonl"), title="Installed wheel smoke test", provider=provider,
                      entries=[Entry("user", "Hello"), Entry("assistant", "Done", phase="final_answer")])
    assert 'class="message assistant final"' in render_html(session)
    assert "Hello" in render_markdown(session)
assert SessionPicker([]).candidates == []
with TemporaryDirectory() as directory:
    source = Path(directory) / "session.jsonl"
    source.write_text('{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"Smoke test"}]}}\n', encoding="utf-8")
    assert main([str(source), "--format", "both", "-o", str(Path(directory) / "out")]) == 0
print(f"Installed wheel {package.version}: entry points, renderers, TUI, and bundled assets OK")
