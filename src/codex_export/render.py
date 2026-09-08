"""Standalone HTML and portable Markdown rendering."""

from __future__ import annotations

import base64
import hashlib
import html
from importlib.resources import files
import re
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.rules_inline import autolink, html_inline, image, link
from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

from .session import Entry, Session
from .privacy import duration_text, prepare_export


def embedded_image(ref: str) -> bool:
    match = re.fullmatch(r"data:image/(png|jpeg|gif|webp);base64,([A-Za-z0-9+/=\s]+)", ref, re.I)
    if not match:
        return False
    try:
        base64.b64decode(match[2], validate=False)
        return True
    except ValueError:
        return False


def safe_link(url: str) -> bool:
    if any(ord(c) < 32 for c in url):
        return False
    try:
        return urlsplit(url).scheme.lower() in {"", "http", "https", "mailto"}
    except ValueError:
        return False


def unavailable_image(ref: str) -> str:
    if ref.startswith("data:"):
        return "[Image unavailable: unsupported or invalid embedded image]"
    return f"[Image reference, not embedded: {ref or 'unknown'}]"


def image_html(ref: str, alt: str = "Session image") -> str:
    if embedded_image(ref):
        return f'<img class="session-image" src="{html.escape(ref, quote=True)}" alt="{html.escape(alt, quote=True)}" loading="lazy">'
    return f'<span class="image-placeholder">{html.escape(unavailable_image(ref))}</span>'


def code_html(code: str, language: str = "") -> str:
    try:
        lexer = get_lexer_by_name(language.split()[0]) if language.strip() else None
    except ClassNotFound:
        lexer = None
    if lexer:
        return highlight(code, lexer, HtmlFormatter(cssclass="highlight"))
    return f'<pre><code>{html.escape(code)}</code></pre>'


def markdown_engine() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False}).enable("table").enable("strikethrough")
    md.validateLink = lambda url: safe_link(url) or embedded_image(url)

    def image_rule(tokens, idx, options, env):
        token = tokens[idx]
        return image_html(token.attrGet("src") or "", token.content)

    def link_rule(tokens, idx, options, env):
        if not safe_link(tokens[idx].attrGet("href") or ""):
            tokens[idx].attrs.pop("href", None)
        tokens[idx].attrSet("rel", "noreferrer noopener")
        return md.renderer.renderToken(tokens, idx, options, env)

    md.add_render_rule("image", lambda renderer, tokens, idx, options, env: image_rule(tokens, idx, options, env))
    md.add_render_rule("link_open", lambda renderer, tokens, idx, options, env: link_rule(tokens, idx, options, env))
    # code_html returns a complete block, not the inner content expected by
    # markdown-it's highlight callback. Override fence to avoid nested <pre>.
    md.add_render_rule("fence", lambda renderer, tokens, idx, options, env: code_html(tokens[idx].content, tokens[idx].info))
    return md


def label(entry: Entry) -> str:
    if entry.kind == "assistant":
        suffix = {"commentary": " · Progress", "final": " · Final", "final_answer": " · Final", "plan": " · Plan"}.get(entry.phase, "")
        return "Assistant" + suffix
    return {"user": "You", "tool": entry.name or "Tool", "marker": "Session event"}[entry.kind]


def render_html(session: Session, include_tools: bool = True, *, full: bool = False, redact=()) -> str:
    session = prepare_export(session, full=full, redact=redact)
    md = markdown_engine()
    entries = [e for e in session.entries if include_tools or e.kind != "tool"]
    escape = html.escape
    provider_name = "Claude Code" if session.provider == "claude" else "Codex"
    sections = []
    for number, entry in enumerate(entries, 1):
        time = f'<time>{escape(entry.timestamp)}</time>' if entry.timestamp else ""
        images = "".join(image_html(ref) for ref in entry.images)
        if entry.kind == "tool":
            result = '<p class="muted">No result recorded.</p>' if entry.result is None else '<h4>Result</h4>' + code_html(entry.result, "json" if entry.result.lstrip().startswith(("{", "[")) else "")
            body = f'<details class="tool" id="entry-{number}"><summary><span class="tool-badge">TOOL</span> {escape(label(entry))}{time}</summary><div class="tool-body"><h4>Input</h4>{code_html(entry.text)}{images}{result}</div></details>'
        elif entry.kind == "marker":
            body = f'<div class="marker" id="entry-{number}">{escape(entry.text)} {time}</div>'
        else:
            final_class = " final" if entry.kind == "assistant" and entry.phase in {"final", "final_answer"} else ""
            text = entry.text if full else markdown_text(entry.text, omit_links=True)
            body = f'<article class="message {entry.kind}{final_class}" id="entry-{number}"><header><span>{escape(label(entry))}</span>{time}<a class="permalink" href="#entry-{number}" aria-label="Link to message {number}">#</a></header><div class="prose">{md.render(text)}{images}</div></article>'
        sections.append(body)
    warning_html = ""
    if session.warnings:
        warning_html = '<details class="warnings"><summary>Export notes</summary><ul>' + "".join(f"<li>{escape(w)}</li>" for w in session.warnings) + "</ul></details>"
    css = files("codex_export").joinpath("style.css").read_text(encoding="utf-8") + HtmlFormatter(style="friendly").get_style_defs(".highlight")
    script = files("codex_export").joinpath("controls.js").read_text(encoding="utf-8")
    script_hash = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    count = sum(e.kind in {"user", "assistant"} for e in entries)
    tools = sum(e.kind == "tool" for e in entries)
    controls = '<button type="button" id="expand">Expand tools</button><button type="button" id="collapse">Collapse tools</button>' if tools else ""
    metadata = [("Session", session.id), ("Started", session.timestamp or "Unknown"), ("Directory", session.cwd or "Unknown")] if full else [("Date", session.timestamp or "Unknown")]
    if session.duration_seconds is not None:
        metadata.append(("Elapsed", duration_text(session.duration_seconds)))
    metadata_html = "".join(f"<div><dt>{key}</dt><dd>{escape(value)}</dd></div>" for key, value in metadata)
    subtitle = f"{count} messages" + (f" <span>·</span> {tools} tool calls" if full else "")
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; script-src 'sha256-{script_hash}'; base-uri 'none'; form-action 'none'">
<title>{escape(session.title)} · {provider_name} session</title><style>{css}</style></head>
<body><main><div class="eyebrow">{provider_name.upper()} / SESSION EXPORT</div><section class="session-header"><h1>{escape(session.title)}</h1>
<p class="subtitle">{subtitle} <span>·</span> Saved for reading</p>
<dl>{metadata_html}</dl>
<div class="controls">{controls}<button type="button" id="print">Print / save PDF</button></div></section>
{warning_html}<section class="transcript" aria-label="Conversation">{''.join(sections) or '<p class="muted">No conversation messages recorded.</p>'}</section>
<footer>Exported locally with claudex-export</footer></main><script>{script}</script></body></html>'''


def fence(text: str, language: str = "") -> str:
    runs = [len(m[0]) for m in re.finditer(r"`+", text)]
    ticks = "`" * max(3, max(runs, default=0) + 1)
    return f"{ticks}{language}\n{text}\n{ticks}"


def plain_markdown(text: str) -> str:
    text = " ".join(text.split())
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", html.escape(text))


def markdown_text(text: str, *, omit_links: bool = False) -> str:
    """Sanitize prose using parser source positions; leave code verbatim."""
    md = MarkdownIt("commonmark", {"html": True, "store_labels": True})
    # Recognize unsafe destinations too, so they can be explicitly removed.
    md.validateLink = lambda url: True
    env: dict = {}
    tokens = md.parse(text, env)
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    masked = list(text)
    edits: list[tuple[int, int, str]] = []
    for token in tokens:
        if token.type in {"fence", "code_block"} and token.map:
            for index in range(offsets[token.map[0]], offsets[token.map[1]]):
                if masked[index] != "\n":
                    masked[index] = " "
    # Resolve reference links inline and remove their definitions, preventing
    # references from one message from affecting another message in the export.
    for ref in env.get("references", {}).values():
        start, end = (offsets[i] for i in ref["map"])
        edits.append((start, end, ""))
        masked[start:end] = ["\n" if c == "\n" else " " for c in text[start:end]]
    source = "".join(masked)

    def capture(rule, kind):
        def wrapped(state, silent):
            start, before = state.pos, len(state.tokens)
            matched = rule(state, silent)
            if not matched or silent or state.src is not source:
                return matched
            added = state.tokens[before:]
            if kind == "html":
                edits.append((start, state.pos, html.escape(text[start:state.pos], quote=False)))
            elif kind == "image":
                token = next(t for t in added if t.type == "image")
                ref = token.attrGet("src") or ""
                replacement = "[Image omitted]" if omit_links else f"![{plain_markdown(token.content)}](<{ref}>)" if embedded_image(ref) else plain_markdown(unavailable_image(ref))
                edits.append((start, state.pos, replacement))
            else:
                token = next(t for t in added if t.type == "link_open")
                ref = token.attrGet("href") or ""
                if omit_links or not safe_link(ref):
                    visible = "".join(t.content for t in added if t.type in {"text", "code_inline"})
                    edits.append((start, state.pos, plain_markdown(visible) + ("" if omit_links else " (unsafe link omitted)")))
                elif token.meta.get("label"):
                    # Replace only the reference suffix so nested image edits
                    # inside the label can still be applied independently.
                    end_label = state.md.helpers.parseLinkLabel(state, start, True)
                    title = token.attrGet("title")
                    title_text = ' "' + title.replace("\\", "\\\\").replace('"', '\\"') + '"' if title else ""
                    edits.append((end_label + 1, state.pos, f"(<{html.escape(ref, quote=False)}>{title_text})"))
            return matched
        return wrapped

    for name, rule, kind in (("image", image, "image"), ("link", link, "link"), ("autolink", autolink, "link"), ("html_inline", html_inline, "html")):
        md.inline.ruler.at(name, capture(rule, kind))
    md.inline.parse(source, md, env, [])
    output, cursor = [], 0
    for start, end, replacement in sorted(edits, key=lambda edit: (edit[0], -edit[1])):
        if start < cursor:
            continue
        output.extend((text[cursor:start], replacement))
        cursor = end
    output.append(text[cursor:])
    return "".join(output)


def render_markdown(session: Session, include_tools: bool = True, *, full: bool = False, redact=()) -> str:
    session = prepare_export(session, full=full, redact=redact)
    provider_name = "Claude Code" if session.provider == "claude" else "Codex"
    metadata = f"- **Source:** {provider_name}\n- **Session:** {plain_markdown(session.id)}\n- **Started:** {plain_markdown(session.timestamp or 'Unknown')}\n- **Directory:** {plain_markdown(session.cwd or 'Unknown')}" if full else f"- **Date:** {plain_markdown(session.timestamp or 'Unknown')}"
    if session.duration_seconds is not None:
        metadata += f"\n- **Elapsed:** {duration_text(session.duration_seconds)}"
    parts = [f"# {plain_markdown(session.title)}", metadata]
    if session.warnings:
        parts.extend(["## Export notes", "\n".join(f"- {plain_markdown(w)}" for w in session.warnings)])
    for entry in session.entries:
        if entry.kind == "tool" and not include_tools:
            continue
        parts.append(f"## {plain_markdown(label(entry))}")
        if entry.timestamp:
            parts.append(f"*{plain_markdown(entry.timestamp)}*")
        if entry.kind == "tool":
            parts.extend(["**Input**", fence(entry.text), "**Result**", fence(entry.result) if entry.result is not None else "No result recorded."])
        elif entry.kind == "marker":
            parts.append(plain_markdown(entry.text))
        else:
            parts.append(markdown_text(entry.text, omit_links=not full))
        for ref in entry.images:
            parts.append(f"![Session image]({ref})" if embedded_image(ref) else plain_markdown(unavailable_image(ref)))
    return "\n\n".join(parts).rstrip() + "\n"
