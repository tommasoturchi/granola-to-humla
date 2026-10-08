#!/usr/bin/env python3
"""Import a Granola CSV export into Humla.

Each Granola document becomes one Humla Note:

    Granola column     Humla notes column   Format written
    -----------------  -------------------  ------------------------------------
    document_title     title                plain text (user-owned, never auto-renamed)
    notes              body                 Tiptap HTML (what Humla's editor stores)
    transcript         transcript           plain text, one utterance per line
    summary            summary              Markdown (what Humla's summary view renders)
    document_created   created_at           epoch milliseconds
                       updated_at           = created_at, so notes sort by meeting date
                       language             detected from the transcript (see --language)

The transcript is imported exactly as Granola exported it. Imported notes have
a transcript but no recording session and no timeline, which is the shape
Humla's ADR-0004 keeps for notes with no audio: the Transcript tab shows the
text in its plain editor, and none of the timeline-rebuild paths can touch it.
Granola's export carries no speaker labels and no timings, and the script
invents neither.

The script writes only `notes` rows (plus an optional folder). Humla's search
chunks and embeddings are derived data: on its next launch Humla indexes every
note that has no chunks yet, so the imported notes become searchable from chat
and from the MCP server without this script duplicating Humla's chunker.

Quit Humla before running. The script refuses to run while the app is open,
and by default takes a consistent backup of notes.sqlite first.

Usage:
    granola-to-humla granola-export.csv --dry-run
    granola-to-humla granola-export.csv
    granola-to-humla granola-export-*.csv --folder Granola
    granola-to-humla granola-export.csv --update
    granola-to-humla granola-export.csv --language en

Each note's language is detected from its transcript with py3langid, voting
over 100-word stretches (see detect_language); a transcript that is short or
mixed gets "auto". Humla writes in that language
when you press Summarize on an imported note or ask it for a new title, and
its MCP server reports it so a connected agent knows which language to search
in. Humla's app-wide language is Norwegian unless you chose another during
setup or in Settings, which is why the script doesn't leave it to that setting.

Re-running is safe. Note ids are derived from Granola's document_id, so a
document that is already in Humla is skipped (or refreshed with --update).

Needs Python 3.11+ and the dependencies listed in pyproject.toml.
"""

from __future__ import annotations

import csv
import datetime as dt
import functools
import re
import sqlite3
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Optional

try:
    import py3langid
    import typer
    from markdown_it import MarkdownIt
    from markdown_it.token import Token
    from py3langid.langid import MODEL_FILE, LanguageIdentifier
    from rich.console import Console
    from rich.markup import escape
    from rich.table import Table
except ImportError as missing:  # pragma: no cover
    sys.exit(
        f"granola_to_humla needs the {missing.name} package. Run it with uvx, or install it with "
        "pip, which bring its dependencies along (see the README)."
    )

__version__ = "1.0.0"

DEFAULT_DATA_DIR = Path.home() / "Library" / "Application Support" / "no.humla.app"
DB_FILE = "notes.sqlite"

# Fixed forever: changing it would make a re-run mint new ids and duplicate
# every note it imported before.
GRANOLA_NAMESPACE = uuid.UUID("6a1f0c7e-2b8d-5e4a-9c3f-67726e6f6c61")

REQUIRED_COLUMNS = ("document_id", "document_title", "document_created", "summary", "notes", "transcript")

# Columns every Humla version since folders has. Anything else is optional and
# only written if the local schema has it.
REQUIRED_NOTE_COLUMNS = {"id", "title", "body", "transcript", "summary", "created_at", "updated_at"}

# Matches the main app binary inside the bundle, not the read-only humla-mcp
# server that MCP clients keep running.
APP_PROCESS_PATTERN = r"/Contents/MacOS/[Hh]umla( |$)"

EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


class ImportFailed(Exception):
    """A problem the user can fix. Reported as a message, without a traceback."""


GRANOLA_FOOTER_RE = re.compile(
    r"\n*(?:-{3,}|\*{3,}|_{3,})\s*\n+\s*Chat with meeting transcript:\s*\S.*\s*\Z",
    re.DOTALL,
)


# --------------------------------------------------------------------------
# Granola CSV
# --------------------------------------------------------------------------


def _raise_csv_field_limit() -> None:
    # Transcripts run to hundreds of KB; the csv module's default cap is 128 KB.
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 10


def read_granola_csv(paths: list[Path]) -> list[dict[str, str]]:
    _raise_csv_field_limit()
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for path in paths:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
            if missing:
                raise ImportFailed(
                    f"{path} is not a Granola CSV export: it has no {', '.join(missing)} column. "
                    f"Its columns are: {', '.join(reader.fieldnames or [])}."
                )
            for row in reader:
                doc_id = (row.get("document_id") or "").strip()
                if not doc_id or doc_id in seen:
                    continue
                seen.add(doc_id)
                rows.append(row)
    return rows


def parse_created(value: str) -> int:
    """Granola's ISO-8601 timestamp -> epoch milliseconds. A timestamp without
    an offset is read as UTC, which is what Granola writes."""
    try:
        stamp = dt.datetime.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(f"unrecognised timestamp {value!r}") from None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return (stamp - EPOCH) // dt.timedelta(milliseconds=1)


def clean_transcript(text: str) -> str:
    """Granola's transcript is already one utterance per line, which is the
    line shape Humla's transcript reader, chunker and search all expect. Only
    line endings and trailing whitespace are normalised; no line is merged,
    split or relabelled."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip("\n")


def clean_summary(text: str, strip_granola_link: bool) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if strip_granola_link:
        text = GRANOLA_FOOTER_RE.sub("", text).rstrip()
    return text


def note_id_for(document_id: str) -> str:
    return str(uuid.uuid5(GRANOLA_NAMESPACE, "granola:" + document_id.strip()))


# --------------------------------------------------------------------------
# Language detection (--language detect, the default)
#
# py3langid (a maintained fork of langid.py) names the language of a piece of
# text, restricted here to the languages Humla's picker offers, so whatever it
# returns is a code Humla knows. On a whole transcript it is always certain,
# even when a meeting switches language halfway, so each transcript is cut
# into stretches of DETECT_CHUNK_WORDS words, every stretch is classified, and
# a language wins only if it covers DETECT_MIN_SHARE of the words. A short,
# mixed or unclear transcript stays on "auto".
# --------------------------------------------------------------------------

# The languages in Humla's picker: Whisper's set (src/lib/languages.ts in Humla).
HUMLA_LANGUAGES = frozenset(
    """
    af sq am ar hy as az ba eu be bn bs br bg my yue ca zh hr cs da nl en et fo fi
    fr gl ka de el gu ht ha haw he hi hu is id it ja jw kn kk km ko lo la lv ln lt
    lb mk mg ms ml mt mi mr mn ne no nn oc ps fa pl pt pa ro ru sa sr sn sd si sk
    sl so es su sw sv tl tg ta tt te th bo tr tk uk ur uz vi cy yi yo
    """.split()
)
_LANGID_TO_HUMLA = {"jv": "jw"}  # Javanese: py3langid says jv, Whisper says jw

DETECT_CHUNK_WORDS = 100
DETECT_MIN_WORDS = 50
DETECT_MIN_SHARE = 0.75


@functools.lru_cache(maxsize=1)
def _language_identifier() -> LanguageIdentifier:
    identifier = LanguageIdentifier.from_modelpath(Path(py3langid.__file__).parent / MODEL_FILE)
    identifier.set_languages(
        [code for code in identifier.nb_classes if _LANGID_TO_HUMLA.get(code, code) in HUMLA_LANGUAGES]
    )
    return identifier


def detect_language(text: str) -> str | None:
    """The language code of `text`, or None when the evidence isn't clear."""
    words = text.split()
    if len(words) < DETECT_MIN_WORDS:
        return None
    identifier = _language_identifier()
    votes: dict[str, int] = {}
    for start in range(0, len(words), DETECT_CHUNK_WORDS):
        chunk = words[start : start + DETECT_CHUNK_WORDS]
        lang, _score = identifier.classify(" ".join(chunk))
        votes[lang] = votes.get(lang, 0) + len(chunk)
    lang, covered = max(votes.items(), key=lambda kv: kv[1])
    if covered / len(words) < DETECT_MIN_SHARE:
        return None
    return _LANGID_TO_HUMLA.get(lang, lang)


# --------------------------------------------------------------------------
# Markdown -> Tiptap HTML (for the user's own notes)
#
# Parsed with markdown-it-py (CommonMark, so two-space nested bullets nest the
# way Granola writes them), then shaped for Humla's notes editor: Tiptap with
# StarterKit, which has headings 1-4, lists, bold, italic, strike, inline
# code, code blocks, blockquotes and rules, and no links or images. A link
# would lose its address on load, so links and images become "label (url)"
# text. Output follows the shape Tiptap itself serialises (<li><p>...</p></li>,
# no whitespace between tags) so the editor loads it without normalising it.
# --------------------------------------------------------------------------

_MD = MarkdownIt("commonmark", {"breaks": True, "html": False}).enable("strikethrough")


def _text_token(content: str) -> Token:
    token = Token("text", "", 0)
    token.content = content
    return token


def _flatten_links(children: list[Token]) -> list[Token]:
    out: list[Token] = []
    i = 0
    while i < len(children):
        token = children[i]
        if token.type == "link_open":
            href = str(token.attrGet("href") or "")
            shown = href[len("mailto:"):] if href.lower().startswith("mailto:") else href
            j = i + 1
            while j < len(children) and children[j].type != "link_close":
                j += 1
            label = children[i + 1 : j]
            label_text = "".join(t.content for t in label if t.type in ("text", "code_inline")).strip()
            out.extend(label)
            if not label_text:
                out.append(_text_token(shown))
            elif label_text not in (href, shown):
                out.append(_text_token(f" ({shown})"))
            i = j + 1
            continue
        if token.type == "image":
            src = str(token.attrGet("src") or "")
            out.append(_text_token(f"{token.content} ({src})" if token.content else src))
        else:
            out.append(token)
        i += 1
    return out


def _shape_for_tiptap(tokens: list[Token]) -> list[Token]:
    shaped: list[Token] = []
    for i, token in enumerate(tokens):
        # An empty bullet ("-" on its own line) carries nothing.
        if token.type == "list_item_open" and i + 1 < len(tokens) and tokens[i + 1].type == "list_item_close":
            continue
        if token.type == "list_item_close" and i > 0 and tokens[i - 1].type == "list_item_open":
            continue
        if token.type == "paragraph_open" or token.type == "paragraph_close":
            token.hidden = False  # tight lists hide them; Tiptap wants <li><p>
        if token.type in ("heading_open", "heading_close") and token.tag in ("h5", "h6"):
            token.tag = "h4"
        if token.type == "inline" and token.children:
            token.children = _flatten_links(token.children)
        shaped.append(token)
    # A list left with no items at all goes too.
    out: list[Token] = []
    for token in shaped:
        if token.type in ("bullet_list_close", "ordered_list_close") and out and out[-1].type == token.type.replace("_close", "_open"):
            out.pop()
            continue
        out.append(token)
    return out


def markdown_to_tiptap(markdown: str) -> str:
    text = markdown.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return ""
    env: dict = {}
    tokens = _shape_for_tiptap(_MD.parse(text, env))
    rendered = _MD.renderer.render(tokens, _MD.options, env)
    rendered = rendered.replace("<br />\n", "<br>").replace("\n</code></pre>", "</code></pre>")
    # Drop the newlines markdown-it puts between tags. Text never contains a
    # literal "<" or ">" (they are escaped), so this only touches markup.
    return re.sub(r">\n(?=<)", ">", rendered).strip()


# --------------------------------------------------------------------------
# Humla database
# --------------------------------------------------------------------------


def humla_is_running() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        result = subprocess.run(["pgrep", "-f", APP_PROCESS_PATTERN], capture_output=True, text=True)
    except FileNotFoundError:
        return False
    return result.returncode == 0


def open_humla_db(data_dir: Path) -> sqlite3.Connection:
    db_path = data_dir / DB_FILE
    if not db_path.is_file():
        raise ImportFailed(
            f"There is no Humla database at {db_path}. Open Humla once so it creates one, quit it, "
            "and run this again, or point --data-dir at the folder that holds notes.sqlite."
        )
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def note_columns(conn: sqlite3.Connection) -> set[str]:
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(notes)")}
    missing = REQUIRED_NOTE_COLUMNS - cols
    if missing:
        raise ImportFailed(
            "This doesn't look like a Humla database: its notes table has no "
            f"{', '.join(sorted(missing))} column."
        )
    return cols


def backup_db(conn: sqlite3.Connection, data_dir: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = data_dir / f"{DB_FILE}.before-granola-import-{stamp}"
    # The backup API copies a consistent snapshot, WAL contents included,
    # which a plain file copy of notes.sqlite would not.
    target = sqlite3.connect(str(dest))
    try:
        conn.backup(target)
    finally:
        target.close()
    return dest


def setting(conn: sqlite3.Connection, key: str) -> str | None:
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row["value"] if row else None


def ensure_folder(conn: sqlite3.Connection, name: str, now: int) -> tuple[str, bool]:
    """A Personal folder with this name, created if missing. Returns (id, created)."""
    has_workspace = "workspace_id" in {r["name"] for r in conn.execute("PRAGMA table_info(folders)")}
    where = "name = ? COLLATE NOCASE" + (" AND workspace_id = ''" if has_workspace else "")
    row = conn.execute(f"SELECT id FROM folders WHERE {where} LIMIT 1", (name,)).fetchone()
    if row:
        return row["id"], False
    folder_id = str(uuid.uuid4())
    if has_workspace:
        conn.execute(
            "INSERT INTO folders (id, name, created_at, updated_at, workspace_id) VALUES (?, ?, ?, ?, '')",
            (folder_id, name, now, now),
        )
    else:
        conn.execute(
            "INSERT INTO folders (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (folder_id, name, now, now),
        )
    return folder_id, True


def drop_derived_index(conn: sqlite3.Connection, note_id: str) -> None:
    """Forget a note's search chunks so Humla re-indexes it on next launch.

    Humla's startup backfill indexes notes that have no chunks; embeddings are
    cached by chunk text, so unchanged text costs nothing to re-embed.
    """
    for table in ("note_chunks", "note_chunks_fts"):
        try:
            conn.execute(f"DELETE FROM {table} WHERE note_id = ?", (note_id,))
        except sqlite3.OperationalError:
            pass  # older schema without the retrieval tables


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------


@dataclass
class Options:
    csv_files: list[Path]
    data_dir: Path = DEFAULT_DATA_DIR
    dry_run: bool = False
    folder: Optional[str] = None
    update: bool = False
    overwrite_edited: bool = False
    language: str = "detect"
    strip_granola_links: bool = False
    limit: Optional[int] = None
    no_backup: bool = False
    yes: bool = False
    allow_running: bool = False


@dataclass
class Plan:
    documents: int
    to_insert: list[dict] = field(default_factory=list)
    to_update: list[dict] = field(default_factory=list)
    already_imported: list[dict] = field(default_factory=list)
    edited_in_humla: list[dict] = field(default_factory=list)
    bad_dates: list[str] = field(default_factory=list)

    @property
    def to_write(self) -> list[dict]:
        return self.to_insert + self.to_update


def build_note(row: dict[str, str], opts: Options) -> dict:
    created = parse_created(row["document_created"])
    note = {
        "id": note_id_for(row["document_id"]),
        "title": " ".join((row.get("document_title") or "").split()),
        "body": markdown_to_tiptap(row.get("notes") or ""),
        "transcript": clean_transcript(row.get("transcript") or ""),
        "summary": clean_summary(row.get("summary") or "", opts.strip_granola_links),
        "created_at": created,
        "updated_at": created,
        "language": opts.language,
    }
    if opts.language == "detect":
        # The transcript is what was said. Granola sometimes writes the summary
        # in another language, so it only stands in when there is no transcript.
        text = note["transcript"] or "\n".join((row.get("summary") or "", row.get("notes") or ""))
        note["language"] = detect_language(text) or "auto"
    return note


def plan_import(opts: Options, rows: list[dict[str, str]], conn: sqlite3.Connection, cols: set[str]) -> Plan:
    plan = Plan(documents=len(rows))
    notes = []
    for row in rows:
        try:
            notes.append(build_note(row, opts))
        except ValueError as e:
            plan.bad_dates.append(f"{row.get('document_title') or row['document_id']}: {e}")
    notes.sort(key=lambda n: n["created_at"])
    existing = {r["id"]: r for r in conn.execute("SELECT id, created_at, updated_at FROM notes")}
    for n in notes:
        prior = existing.get(n["id"])
        if prior is None:
            plan.to_insert.append(n)
        elif not opts.update:
            plan.already_imported.append(n)
        elif prior["updated_at"] != prior["created_at"] and not opts.overwrite_edited:
            # The importer writes updated_at = created_at; anything else means
            # the note was edited in Humla since.
            plan.edited_in_humla.append(n)
        else:
            plan.to_update.append(n)
    return plan


def write_import(plan: Plan, opts: Options, conn: sqlite3.Connection, cols: set[str]) -> Optional[bool]:
    """Write the plan in one transaction. Returns whether the folder was created
    (None without --folder)."""
    summary_preset = (setting(conn, "default_summary_preset") or "").strip() or "meeting"
    now = (dt.datetime.now(dt.timezone.utc) - EPOCH) // dt.timedelta(milliseconds=1)
    optional_defaults = {
        "audio_path": None,
        "summary_preset": summary_preset,
        "summary_provider": "",
        "expected_speakers": None,
        "owner": "",
        "workspace_id": "",  # Personal: local-only, as Granola's "Personal" workspace was
        "private": 0,
        "deleted_at": None,
        "client_id": None,
        "detected_language": None,
        "speakers": "",  # derived by Humla's indexer on next launch
    }
    folder_created = None
    with conn:  # one transaction: all or nothing
        folder_id = None
        if opts.folder:
            folder_id, folder_created = ensure_folder(conn, opts.folder, now)
        for n in plan.to_insert:
            values = dict(n)
            if "language" not in cols:
                del values["language"]
            for col, default in optional_defaults.items():
                if col in cols:
                    values[col] = default
            if folder_id and "folder_id" in cols:
                values["folder_id"] = folder_id
            names = list(values)
            conn.execute(
                f"INSERT INTO notes ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
                [values[c] for c in names],
            )
        for n in plan.to_update:
            conn.execute(
                "UPDATE notes SET title = ?, body = ?, transcript = ?, summary = ?, "
                "created_at = ?, updated_at = ? WHERE id = ?",
                (n["title"], n["body"], n["transcript"], n["summary"], n["created_at"], n["updated_at"], n["id"]),
            )
            if "language" in cols:
                conn.execute("UPDATE notes SET language = ? WHERE id = ?", (n["language"], n["id"]))
            drop_derived_index(conn, n["id"])
            if folder_id and "folder_id" in cols:
                conn.execute("UPDATE notes SET folder_id = ? WHERE id = ? AND folder_id IS NULL", (folder_id, n["id"]))
    return folder_created


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

console = Console(highlight=False)
err_console = Console(stderr=True, highlight=False)


def _show_path(path: Path) -> str:
    try:
        return escape("~/" + str(path.relative_to(Path.home())))
    except ValueError:
        return escape(str(path))


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def describe_language(opts: Options, notes: list[dict]) -> str:
    if opts.language == "detect":
        counts: dict[str, int] = {}
        for n in notes:
            counts[n["language"]] = counts.get(n["language"], 0) + 1
        parts = ", ".join(f"{lang} {k}" for lang, k in sorted(counts.items(), key=lambda kv: -kv[1]))
        return f"detected per note: {parts}" if parts else "detected per note"
    if opts.language == "auto":
        return "auto-detect for every note"
    return f"{opts.language} for every note" if opts.language else "Humla's default language setting"


def show_plan(plan: Plan, opts: Options) -> None:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column(overflow="fold")
    grid.add_row("Granola export", f"{_plural(plan.documents, 'meeting')} in {_plural(len(opts.csv_files), 'file')}")
    grid.add_row("Humla library", _show_path(opts.data_dir / DB_FILE))
    grid.add_row("New notes", str(len(plan.to_insert)))
    if opts.update:
        grid.add_row("Refreshed", str(len(plan.to_update)))
        if plan.edited_in_humla:
            grid.add_row("Kept as they are", f"{len(plan.edited_in_humla)} (edited in Humla since the import)")
    elif plan.already_imported:
        grid.add_row("Already imported", f"{len(plan.already_imported)} (skipped; --update refreshes them)")
    grid.add_row("With your own notes", str(sum(1 for n in plan.to_write if n["body"])))
    grid.add_row("Language", describe_language(opts, plan.to_write))
    if opts.folder:
        grid.add_row("Folder", escape(opts.folder))
    for msg in plan.bad_dates:
        grid.add_row("[red]Skipped[/red]", escape(msg))
    console.print(grid)


def show_sample(plan: Plan, rows: int = 8) -> None:
    table = Table(title=f"First {min(rows, len(plan.to_write))} of {len(plan.to_write)}", title_justify="left")
    table.add_column("Date")
    table.add_column("Title", overflow="ellipsis", no_wrap=True, max_width=48)
    table.add_column("Transcript", justify="right")
    table.add_column("Summary", justify="right")
    table.add_column("Notes")
    table.add_column("Language")
    for n in plan.to_write[:rows]:
        table.add_row(
            dt.datetime.fromtimestamp(n["created_at"] / 1000).strftime("%Y-%m-%d %H:%M"),
            escape(n["title"]),
            f"{len(n['transcript'].splitlines())} lines",
            f"{len(n['summary'])} chars",
            "yes" if n["body"] else "no",
            n["language"] or "default",
        )
    console.print(table)


def run_import(opts: Options) -> int:
    with console.status("Reading the Granola export…"):
        rows = read_granola_csv(opts.csv_files)
        if opts.limit:
            rows = rows[: opts.limit]
    if not opts.dry_run and humla_is_running() and not opts.allow_running:
        raise ImportFailed(
            "Humla is running. Quit it first (Cmd+Q, or Quit from its menu-bar icon), then run this "
            "again. Humla only indexes newly imported notes for search when it starts."
        )
    conn = open_humla_db(opts.data_dir)
    try:
        cols = note_columns(conn)
        with console.status("Converting notes and detecting languages…"):
            plan = plan_import(opts, rows, conn, cols)
        show_plan(plan, opts)

        if opts.dry_run:
            if plan.to_write:
                console.print()
                show_sample(plan)
            console.print("\n[bold]Dry run[/bold]: nothing was written.")
            return 0
        if not plan.to_write:
            console.print("\nNothing to write.")
            return 0

        console.print()
        if not opts.yes:
            if not sys.stdin.isatty():
                raise ImportFailed("Not running in a terminal, so I can't ask before writing. Add --yes to import without asking.")
            if not typer.confirm(f"Write {_plural(len(plan.to_write), 'note')} to Humla?", default=False):
                console.print("Nothing written.")
                return 1
        if not opts.no_backup:
            backup = backup_db(conn, opts.data_dir)
            console.print(f"Backup: {_show_path(backup)}", soft_wrap=True)
        folder_created = write_import(plan, opts, conn, cols)
    finally:
        conn.close()

    done = f"Imported {_plural(len(plan.to_insert), 'note')}"
    if plan.to_update:
        done += f", refreshed {len(plan.to_update)}"
    if opts.folder:
        done += f" into {'a new' if folder_created else 'the'} {escape(opts.folder)} folder"
    console.print(f"[green]{done}.[/green]")
    console.print("Open Humla: it indexes the new notes for search and chat as it starts.")
    return 0


def _language_option(value: str) -> str:
    """--language: "detect", "auto", "default" (stored as "", Humla's fallback
    to its app-wide setting), or a code as Humla's picker uses them."""
    code = value.strip().lower()
    if code == "default":
        return ""
    if code in ("detect", "auto") or code in HUMLA_LANGUAGES:
        return code
    raise typer.BadParameter("use detect, auto, default, or a language code from Humla's list, such as en or it")


def _version_option(value: bool) -> None:
    if value:
        console.print(f"granola-to-humla {__version__}")
        raise typer.Exit()


app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)


@app.command(
    help="Import a Granola CSV export into Humla.",
    epilog="Quit Humla first. Imported notes land in your Personal space, and a backup of your "
    "library is saved before anything is written.",
)
def cli(
    csv_files: Annotated[
        list[Path],
        typer.Argument(
            metavar="EXPORT.csv", help="Granola export CSV file(s).", exists=True, dir_okay=False, show_default=False
        ),
    ],
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show what would be imported; write nothing.")] = False,
    folder: Annotated[
        Optional[str], typer.Option(metavar="NAME", help="File the imported notes in this folder, created if missing.")
    ] = None,
    language: Annotated[
        str,
        typer.Option(
            metavar="CODE",
            callback=_language_option,
            help="Each note's language: [bold]detect[/bold] it from the transcript (auto when unclear), "
            "[bold]auto[/bold] for all, a code such as [bold]en[/bold] or [bold]it[/bold] for all, "
            "or [bold]default[/bold] to follow Humla's setting.",
        ),
    ] = "detect",
    update: Annotated[
        bool, typer.Option("--update", help="Refresh notes from an earlier import; ones you edited in Humla are kept.")
    ] = False,
    overwrite_edited: Annotated[
        bool, typer.Option("--overwrite-edited", help="With --update, overwrite the edited ones too.")
    ] = False,
    strip_granola_links: Annotated[
        bool,
        typer.Option(
            "--strip-granola-links", help='Drop the "Chat with meeting transcript" link Granola adds to summaries.'
        ),
    ] = False,
    limit: Annotated[
        Optional[int], typer.Option(metavar="N", min=1, help="Import only the first N meetings, for a trial.")
    ] = None,
    data_dir: Annotated[
        Path,
        typer.Option(help="Humla's data folder.", file_okay=False, show_default="~/Library/Application Support/no.humla.app"),
    ] = DEFAULT_DATA_DIR,
    no_backup: Annotated[bool, typer.Option("--no-backup", help="Skip the backup of your Humla library.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask before writing.")] = False,
    allow_running: Annotated[bool, typer.Option("--allow-running", hidden=True)] = False,
    version: Annotated[
        Optional[bool],
        typer.Option("--version", callback=_version_option, is_eager=True, help="Show the version and exit."),
    ] = None,
) -> None:
    opts = Options(
        csv_files=csv_files,
        data_dir=data_dir.expanduser(),
        dry_run=dry_run,
        folder=folder,
        update=update,
        overwrite_edited=overwrite_edited,
        language=language,
        strip_granola_links=strip_granola_links,
        limit=limit,
        no_backup=no_backup,
        yes=yes,
        allow_running=allow_running,
    )
    try:
        code = run_import(opts)
    except ImportFailed as e:
        err_console.print(f"[red]{escape(str(e))}[/red]")
        raise typer.Exit(1) from None
    raise typer.Exit(code)


def main() -> None:
    app(prog_name="granola-to-humla")


if __name__ == "__main__":
    main()
