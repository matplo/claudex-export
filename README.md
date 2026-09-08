# Claudex Export

Export local Codex and Claude Code conversations to readable **standalone HTML**
or **Markdown**. Run `claudex-export`
without a filename to browse both providers in a full-screen Textual picker.
Preview the first and last 10 user prompts before choosing a session.

**Exports are sanitized by default.** They contain the session date, title,
recorded elapsed time when available, direct user prompts, and assistant
progress/final replies. Tools, execution plans, injected context, images,
session IDs, working directories, and individual message timestamps are omitted.
Use `--full` to restore the previous detailed transcript, or press `f` in the
Textual picker or preview to toggle between sanitized and full export.

Requires Python 3.10 or later. No API key or service is needed.

## Install

```bash
python -m pip install claudex-export
```

Or install it as an isolated command-line application with
`pipx install claudex-export`. The commands `claudex-export`, `claude-export`,
`codex-export`, and the compatibility alias `session-export` launch the same
tool and support both providers. Python and its dependencies are sufficient; `henv` is
only used for development in this repository.

## Usage

```bash
# Browse and search local sessions; HTML is the default
claudex-export

# Browse only Claude Code sessions
claudex-export --source claude

# Auto-detect a Claude Code file and export both formats
claudex-export /path/to/claude-session.jsonl --format both -o exports

# Opt in to metadata, original messages, tool details, and images
claudex-export /path/to/session.jsonl --full -o detailed.html

# Remove additional names or literal strings from sanitized output
claudex-export /path/to/session.jsonl --redact 'Jane Doe' --redact 'Acme Private Project'

# Export a specified rollout
claudex-export /path/to/rollout.jsonl -o conversation.html

# Markdown, without tool calls/results
claudex-export /path/to/rollout.jsonl --format md --no-tools -o conversation.md

# Both formats, into a directory
claudex-export /path/to/rollout.jsonl --format both -o exports

# Include archived sessions, or use another Codex home
claudex-export --include-archived
claudex-export --codex-home /path/to/.codex
```

The Textual picker supports arrow keys, Page Up/Down, and mouse navigation.
Press `/` to focus live search; search matches titles, directories, IDs, and
provider names. Enter in the search box returns focus to the results.

| Key | Action |
| --- | --- |
| `↑` / `↓` | Move between sessions |
| `Enter` on a session | Select and export |
| `v` | Preview the selected session's user prompts |
| `f` in list or preview | Toggle sanitized/full export; current mode is displayed |
| `Esc` / `v` in preview | Return to the list |
| `e` in preview | Select and export the previewed session |
| `/` | Focus search |
| `Esc` in list | Clear search and focus the list |
| `q` / `Ctrl+C` | Cancel |

Previews are scrollable and loaded in a background worker. They follow the selected
export mode and show prompt text with original numbering: the first 10 and last 10,
without duplicates where those ranges overlap. For longer sessions a marker
shows how many middle prompts were omitted. Tool results, injected context, and
compaction summaries are excluded from the prompt preview. Images are counted,
not opened. Reading a preview does not write an export.

Use `--plain-picker` for the original numbered terminal picker (`/search`,
`n`/`p`, number to select, `q` to cancel); prompt previews require the Textual
picker. Without an interactive terminal, supply an explicit input file.

By default, exports are written to the current directory with the date and a
redacted title in the filename. Full exports use the original title and session
ID. `-o` is a file path for a single format and a
directory for `--format both`. Parent directories are created as needed.
Existing files require `--force`; the source session is never overwritten.

## Sanitization and full exports

The same defaults apply to HTML and Markdown, including `--format both`.
Sanitization removes recognizable email addresses, IP/MAC addresses, common
phone-number formats, UUIDs, local paths, local hostnames, and known identity
values from metadata and local account information. It also redacts those values
in titles, prose, and code blocks. Links lose their destinations and image
payloads are omitted. Generated filenames exclude the session ID and redact the
title; an explicitly supplied `-o` name is used as given.

This is deterministic, local, **best-effort** redaction. It cannot identify every
person's name, street address, organization, or other identifying fact in arbitrary
prose. Review a file before sharing it and supply repeatable `--redact TEXT`
arguments for additional names or strings. No external service is used.

Elapsed time is the span between recorded timestamps and includes idle time;
it is omitted if the log does not provide distinct usable timestamps.

`--full` disables sanitization and restores metadata, original conversation text,
tools, images, and session event markers. `--no-tools` can still suppress tools
in full mode. `--redact` applies only to sanitized mode. Full mode does not add
raw system/developer instructions, reasoning, usage records, or unknown records
that the parser has always excluded. The local picker still displays directories
and IDs to help identify a session; they are not included in sanitized exports.

## Supported content

- Native Codex rollout and Claude Code `.jsonl` files, containing one JSON
  record per line. File content determines the format automatically; use
  `--source codex` or `--source claude` to specify it explicitly.
- `.json` containing a single native session record or an array of records.
  Codex records use `type`/`payload`; Claude Code uses `type`/`message` and
  session metadata. Arbitrary chat JSON formats are not supported.
- User messages and assistant progress/final replies in recorded order. Tool
  inputs/results and compaction/interruption markers are included with `--full`.
- Native transcript records take priority over matching completion events.
  Duplicates are matched by IDs and by content within each turn, one occurrence
  at a time. Separate repeated messages remain present.
- System/developer messages, reasoning, usage records, and replacement history
  from compaction are omitted. Instructions stored as actual **user** messages
  remain part of the full transcript. Known injected context is omitted in
  sanitized mode.
- Tools invoked inside an orchestration tool may appear as separate execution
  events as well as in that tool's output: these describe nested operations.
- Claude Code tool-use blocks pair with tool results by tool-use ID. Replayed
  UUIDs are deduplicated while separate blocks sharing a message ID survive.
  Thinking blocks, metadata, and compacted summaries are omitted. End-of-turn
  replies use the light blue final-answer styling.

HTML includes responsive styling, highlighted code, tables, links, message
anchors, collapsed tool details, expand/collapse buttons, and a print button.
It uses embedded CSS/JavaScript and works offline. Printing expands tool details.
Markdown keeps conversation formatting and uses fenced blocks for tool data.

With `--full`, embedded PNG, JPEG, GIF, and WebP data images are preserved. Other image
references become placeholders; the exporter does not fetch remote images or
read image paths from the transcript. Raw HTML is escaped in conversation prose;
HTML exports restrict scripts and resource loads with a Content Security Policy.

Codex discovery respects `--codex-home`, then `CODEX_HOME`, then `~/.codex`.
It scans `sessions/` (and optionally `archived_sessions/`) and uses
`session_index.jsonl` titles when available. Sessions are sorted by recent
activity. Each export covers one file; child/subagent sessions are not recursively
included. Claude Code discovery respects `--claude-home`, then
`CLAUDE_CONFIG_DIR`, then `~/.claude`, and reads main session files under
`projects/`. Custom or automatic session titles take priority over the first
prompt. Subagent logs are excluded from discovery; pass a subagent file explicitly
to export it. `--source all` (the default) combines both providers; archived
session inclusion applies to Codex. Source files and databases are never modified.

Unknown record types and malformed JSONL lines produce warnings. An incomplete
final line in a running session is skipped; rerun the export later to include it.
Local session formats can evolve, so inspect export notes when a newer record type
appears. Full exports contain the selected conversation and tool data as stored;
sanitized exports apply the redaction described above.

## Development

```bash
henv -n export_codex_session_dev -x python -m pip install -e '.[dev]'
henv -n export_codex_session_dev -x python -m pytest
```

Tests use synthetic sessions, including headless Textual keyboard/preview tests.
Keep private session files and generated exports
out of source control; `exports/` is ignored for local previews.

## Releases to PyPI

Pushing a version tag triggers `.github/workflows/release.yml`. It runs tests
on Python 3.10–3.14, builds an sdist and wheel, checks package metadata, and
smoke-tests an installation of the built wheel. The tag must exactly match
`v` followed by the package version. Only then does it publish to PyPI and
create a GitHub release with both distribution files attached.

### One-time PyPI setup

In your PyPI account, open [Publishing](https://pypi.org/manage/account/publishing/)
and add a **pending GitHub publisher** with these exact values:

| Field | Value |
| --- | --- |
| PyPI project name | `claudex-export` |
| GitHub owner | `matplo` |
| Repository | `claudex-export` |
| Workflow filename | `release.yml` |
| Environment name | `pypi` |

If the PyPI project already exists in your account, add the same publisher
under that project's Publishing settings. The GitHub workflow uses OIDC
Trusted Publishing; no `PYPI_API_TOKEN` secret is needed. The GitHub environment
`pypi` must not require reviewer approval if you want fully automatic releases.
The PyPI project is created by the first successful publish, not by registering
the pending publisher.

### Publish a version

Set `__version__` in `src/codex_export/__init__.py` (the single version source),
commit the change, and push a matching tag. For the initial `0.1.0` release:

```bash
henv -n export_codex_session_dev -x git push origin main
henv -n export_codex_session_dev -x git tag -a v0.1.0 -m 'Release 0.1.0'
henv -n export_codex_session_dev -x git push origin v0.1.0
```

For later releases, change the version and tag together. Prereleases such as
`0.2.0rc1` use `v0.2.0rc1`. PyPI versions are immutable: publish a new version
for changed artifacts. If publishing fails before upload, fix the configuration
and rerun the failed workflow jobs. If only GitHub release creation fails after
PyPI succeeds, rerun that failed job rather than publishing again.

See [PyPI's Trusted Publishing documentation](https://docs.pypi.org/trusted-publishers/)
for account setup details. Ordinary pushes to `main` and pull requests run the
same tests and packaging checks without publishing.

## License

MIT; see [LICENSE](LICENSE).
