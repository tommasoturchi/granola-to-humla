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
    python3 granola_to_humla.py granola-export.csv --dry-run
    python3 granola_to_humla.py granola-export.csv
    python3 granola_to_humla.py granola-export-*.csv --folder Granola
    python3 granola_to_humla.py granola-export.csv --update
    python3 granola_to_humla.py granola-export.csv --language en

Each note's language is detected from its transcript by counting common
function words (see detect_language); a transcript that is short, mixed or in
a language the lists don't cover gets "auto". Humla writes in that language
when you press Summarize on an imported note or ask it for a new title, and
its MCP server reports it so a connected agent knows which language to search
in. Humla's app-wide language is Norwegian unless you chose another during
setup or in Settings, which is why the script doesn't leave it to that setting.

Re-running is safe. Note ids are derived from Granola's document_id, so a
document that is already in Humla is skipped (or refreshed with --update).

Standard library only; Python 3.9+.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import re
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

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
                sys.exit(
                    f"{path}: not a Granola CSV export (missing column(s): {', '.join(missing)}).\n"
                    f"Found: {', '.join(reader.fieldnames or [])}"
                )
            for row in reader:
                doc_id = (row.get("document_id") or "").strip()
                if not doc_id or doc_id in seen:
                    continue
                seen.add(doc_id)
                rows.append(row)
    return rows


_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?\s*(Z|[+-]\d{2}:?\d{2})?$"
)


def parse_created(value: str) -> int:
    """Granola's ISO-8601 timestamp -> epoch milliseconds.

    Hand-parsed because Python 3.9's fromisoformat rejects the trailing `Z`
    and fractional seconds that aren't exactly 3 or 6 digits.
    """
    m = _ISO_RE.match(value.strip())
    if not m:
        raise ValueError(f"unrecognised timestamp {value!r}")
    year, month, day, hour, minute = (int(m.group(i)) for i in range(1, 6))
    second = int(m.group(6) or 0)
    micro = int(((m.group(7) or "") + "000000")[:6])
    tz_raw = m.group(8)
    if tz_raw in (None, "Z"):
        tz = dt.timezone.utc
    else:
        sign = 1 if tz_raw[0] == "+" else -1
        digits = tz_raw[1:].replace(":", "")
        tz = dt.timezone(sign * dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:])))
    stamp = dt.datetime(year, month, day, hour, minute, second, micro, tzinfo=tz)
    return int(stamp.timestamp() * 1000)


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
# Counts common function words per language. Only words that belong to a
# single language's list count, so "de", "la" or "en", which several
# languages share, never tip the balance; that is also what separates
# Norwegian from Danish ("hva", "litt", "mye" against "hvad", "lidt",
# "meget"). Codes are the ones Humla's language picker uses.
#
# A note gets a language only when the evidence is clear: at least
# DETECT_MIN_HITS marker words, at least DETECT_MIN_SHARE of them from one
# language. Short, mixed or unlisted-language transcripts stay on "auto".
# --------------------------------------------------------------------------

_FUNCTION_WORDS = {
    "en": "the and that this with have but not they what just about know think yeah there would "
          "because don't it's i'm we're you're can't should could which their been were "
          "really something actually also",
    "it": "il che non per sono della del anche questo questa quindi perché però cioè allora gli "
          "una più poi ho hai abbiamo fare molto tutto essere cosa dove nel alla delle dei ci "
          "magari comunque adesso ancora le qui",
    "es": "el los las pero porque para por está muy también esto eso cuando hay pues "
          "entonces bueno yo creo puede hacer sólo ahora del una cosa",
    "fr": "le les des est une pas je vous nous qui dans pour avec sur mais c'est ça donc voilà "
          "très aussi elle ils au aux du peut fait faire il non",
    "de": "der die das und ist nicht ich sie ein eine zu den mit auf für auch dass wir aber noch "
          "sehr oder wenn wie haben sind schon jetzt du also",
    "pt": "os não uma para com mas você isso isto então também eu ele ela muito tem na ao foi "
          "bem aqui fazer das",
    "nl": "het een van dat niet ik je op voor maar ook dan zijn nog wel heel dus hebben gewoon "
          "eigenlijk nu",
    "sv": "och att är inte jag ett ska vad hur också där väldigt göra varit riktigt den nu",
    "no": "ikke jeg hva kanskje litt mye noe ble sånn veldig gjøre vært riktig nå",
    "da": "ikke jeg hvad måske lidt meget noget blev sådan rigtig gøre været nu",
}


def _marker_words() -> dict[str, str]:
    seen: dict[str, set[str]] = {}
    for lang, words in _FUNCTION_WORDS.items():
        for word in words.split():
            seen.setdefault(word, set()).add(lang)
    return {word: langs.pop() for word, langs in seen.items() if len(langs) == 1}


LANGUAGE_MARKERS = _marker_words()
DETECT_MIN_HITS = 20
DETECT_MIN_SHARE = 0.75
_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")


def detect_language(text: str) -> str | None:
    """The language code of `text`, or None when the evidence isn't clear."""
    hits: dict[str, int] = {}
    for word in _WORD_RE.findall(text.lower().replace("’", "'")):
        lang = LANGUAGE_MARKERS.get(word)
        if lang:
            hits[lang] = hits.get(lang, 0) + 1
    total = sum(hits.values())
    if total < DETECT_MIN_HITS:
        return None
    lang, top = max(hits.items(), key=lambda kv: kv[1])
    return lang if top / total >= DETECT_MIN_SHARE else None


# --------------------------------------------------------------------------
# Markdown -> Tiptap HTML (for the user's own notes)
#
# Humla's notes editor is Tiptap with StarterKit (headings 1-4, lists, bold,
# italic, strike, inline code, code blocks, blockquotes, rules). It has no Link
# extension, so a link would be dropped to its label on load: links are written
# as "label (url)" text instead, which keeps the URL visible and searchable.
# Output follows the shape Tiptap itself serialises (<li><p>…</p></li>, no
# whitespace between tags) so the editor loads it without normalising it.
# --------------------------------------------------------------------------

_LIST_RE = re.compile(r"^( *)([-*+]|\d{1,9}[.)])(?:[ \t]+(.*))?$")
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_HR_RE = re.compile(r"^ {0,3}(?:(?:-[ \t]*){3,}|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*([^`\s]*)")
_QUOTE_RE = re.compile(r"^ {0,3}>[ ]?(.*)$")
_LINK_RE = re.compile(r"\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_AUTOLINK_RE = re.compile(r"<((?:https?|mailto):[^>\s]+)>")
_ESCAPE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")
_CODE_SPAN_RE = re.compile(r"(`+)(.+?)\1")
_TOKEN_RE = re.compile("\ue000(\\d+)\ue001")


def _esc(text: str) -> str:
    # Matches what ProseMirror's serializer escapes in text nodes.
    return html.escape(text, quote=False)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _dedent(line: str, n: int) -> str:
    return line[min(n, _indent(line)):]


def _is_list_item(line: str) -> bool:
    return bool(_LIST_RE.match(line)) and not _HR_RE.match(line)


def _starts_block(line: str) -> bool:
    return bool(
        _HEADING_RE.match(line)
        or _HR_RE.match(line)
        or _FENCE_RE.match(line)
        or _QUOTE_RE.match(line)
        or _is_list_item(line)
    )


def render_inline(text: str) -> str:
    tokens: list[str] = []

    def stash(fragment: str) -> str:
        tokens.append(fragment)
        return f"\ue000{len(tokens) - 1}\ue001"

    def link(label: str, url: str) -> str:
        shown = url[len("mailto:"):] if url.lower().startswith("mailto:") else url
        label = label.strip()
        if not label or label == url or label == shown:
            return stash(_esc(shown))
        return stash(f"{render_inline(label)} ({_esc(shown)})")

    text = _CODE_SPAN_RE.sub(lambda m: stash(f"<code>{_esc(m.group(2).strip())}</code>"), text)
    text = _ESCAPE_RE.sub(lambda m: stash(_esc(m.group(1))), text)
    text = _LINK_RE.sub(lambda m: link(m.group(1), m.group(2)), text)
    text = _AUTOLINK_RE.sub(lambda m: link("", m.group(1)), text)
    text = _esc(text)
    text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)", r"<strong>\1</strong>", text)
    text = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<s>\1</s>", text)
    text = re.sub(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])", r"<em>\1</em>", text)
    text = re.sub(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)", r"<em>\1</em>", text)
    while "\ue000" in text:
        text = _TOKEN_RE.sub(lambda m: tokens[int(m.group(1))], text)
    return text


def _parse_list(lines: list[str], i: int):
    first = _LIST_RE.match(lines[i])
    base = len(first.group(1))
    ordered = first.group(2)[0].isdigit()
    start = int(first.group(2)[:-1]) if ordered else 1
    items: list[list] = []

    def sibling(line: str) -> bool:
        m = _LIST_RE.match(line)
        return bool(m) and not _HR_RE.match(line) and len(m.group(1)) == base and m.group(2)[0].isdigit() == ordered

    while i < len(lines):
        line = lines[i]
        if not line.strip():
            j = i
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and sibling(lines[j]):
                i = j
                continue
            break
        if not sibling(line):
            break
        m = _LIST_RE.match(line)
        marker = m.group(2)
        content = m.group(3) or ""
        after = line[base + len(marker):]
        gap = len(after) - len(after.lstrip(" \t"))
        if not content or gap > 4:
            gap = 1
        content_indent = base + len(marker) + gap
        item_lines = [content]
        i += 1
        while i < len(lines):
            cur = lines[i]
            if not cur.strip():
                j = i
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j < len(lines) and _indent(lines[j]) > base:
                    item_lines.extend([""] * (j - i))
                    i = j
                    continue
                break
            if _indent(cur) > base:
                item_lines.append(_dedent(cur, content_indent))
                i += 1
                continue
            if _starts_block(cur):
                break
            item_lines.append(cur.strip())  # lazy continuation of the item's paragraph
            i += 1
        items.append(parse_blocks(item_lines))
    return ("list", ordered, start, items), i


def parse_blocks(lines: list[str]) -> list:
    blocks: list = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        fence = _FENCE_RE.match(line)
        if fence:
            closer, lang = fence.group(1), fence.group(2)
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(closer):
                body.append(lines[i])
                i += 1
            i += 1
            blocks.append(("code", lang, "\n".join(body)))
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            blocks.append(("h", min(len(heading.group(1)), 4), heading.group(2)))
            i += 1
            continue
        if _HR_RE.match(line):
            blocks.append(("hr",))
            i += 1
            continue
        if _QUOTE_RE.match(line):
            quoted: list[str] = []
            while i < len(lines) and lines[i].strip():
                q = _QUOTE_RE.match(lines[i])
                quoted.append(q.group(1) if q else lines[i].strip())
                i += 1
            blocks.append(("quote", parse_blocks(quoted)))
            continue
        if _is_list_item(line):
            block, i = _parse_list(lines, i)
            blocks.append(block)
            continue
        para = [line.strip()]
        i += 1
        while i < len(lines) and lines[i].strip() and not _starts_block(lines[i]):
            para.append(lines[i].strip())
            i += 1
        blocks.append(("p", para))
    return blocks


def render_blocks(blocks: list) -> str:
    out: list[str] = []
    for block in blocks:
        kind = block[0]
        if kind == "p":
            out.append("<p>" + "<br>".join(render_inline(l) for l in block[1]) + "</p>")
        elif kind == "h":
            out.append(f"<h{block[1]}>{render_inline(block[2])}</h{block[1]}>")
        elif kind == "hr":
            out.append("<hr>")
        elif kind == "code":
            cls = f' class="language-{_esc(block[1])}"' if block[1] else ""
            out.append(f"<pre><code{cls}>{_esc(block[2])}</code></pre>")
        elif kind == "quote":
            inner = render_blocks(block[1])
            if inner:
                out.append(f"<blockquote>{inner}</blockquote>")
        elif kind == "list":
            _, ordered, start, items = block
            rendered = []
            for item in items:
                if not item:
                    continue  # an empty bullet ("-") carries nothing
                inner = render_blocks(item)
                if item[0][0] != "p":
                    inner = "<p></p>" + inner  # Tiptap list items open with a paragraph
                rendered.append(f"<li>{inner}</li>")
            if rendered:
                tag = "ol" if ordered else "ul"
                attrs = f' start="{start}"' if ordered and start != 1 else ""
                out.append(f"<{tag}{attrs}>{''.join(rendered)}</{tag}>")
    return "".join(out)


def markdown_to_tiptap(markdown: str) -> str:
    text = markdown.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
    if not text.strip():
        return ""
    return render_blocks(parse_blocks(text.split("\n")))


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
        sys.exit(
            f"No Humla database at {db_path}.\n"
            "Launch Humla once so it creates its database, quit it, then run this again "
            "(or point --data-dir at the folder that holds notes.sqlite)."
        )
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def note_columns(conn: sqlite3.Connection) -> set[str]:
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(notes)")}
    missing = REQUIRED_NOTE_COLUMNS - cols
    if missing:
        sys.exit(
            "This database doesn't look like a Humla notes database "
            f"(notes table is missing: {', '.join(sorted(missing))})."
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


def build_note(row: dict[str, str], args) -> dict:
    created = parse_created(row["document_created"])
    note = {
        "id": note_id_for(row["document_id"]),
        "title": " ".join((row.get("document_title") or "").split()),
        "body": markdown_to_tiptap(row.get("notes") or ""),
        "transcript": clean_transcript(row.get("transcript") or ""),
        "summary": clean_summary(row.get("summary") or "", args.strip_granola_links),
        "created_at": created,
        "updated_at": created,
        "language": args.language,
    }
    if args.language == "detect":
        # The transcript is what was said. Granola sometimes writes the summary
        # in another language, so it only stands in when there is no transcript.
        text = note["transcript"] or "\n".join((row.get("summary") or "", row.get("notes") or ""))
        note["language"] = detect_language(text) or "auto"
    return note


def parse_language(value: str) -> str:
    """--language: "detect" (per note, see detect_language), "auto", "default"
    (stored as "", Humla's own fallback to the app-wide language setting), or a
    language code as Humla's picker uses them (ISO 639-1, plus a few
    three-letter Whisper codes such as "haw" and "yue")."""
    code = value.strip().lower()
    if code == "default":
        return ""
    if code in ("detect", "auto") or re.fullmatch(r"[a-z]{2,3}", code):
        return code
    raise argparse.ArgumentTypeError(
        f"{value!r}: use detect, auto, default, or a language code such as en or it"
    )


def describe_language(code: str, notes: list[dict]) -> str:
    if code == "detect":
        counts: dict[str, int] = {}
        for n in notes:
            counts[n["language"]] = counts.get(n["language"], 0) + 1
        parts = ", ".join(f"{lang} {k}" for lang, k in sorted(counts.items(), key=lambda kv: -kv[1]))
        return f"detected per note ({parts})" if parts else "detected per note"
    if code == "auto":
        return "auto-detect"
    return code or "Humla's default language setting"


def run(args) -> int:
    paths = [Path(p).expanduser() for p in args.csv]
    for p in paths:
        if not p.is_file():
            sys.exit(f"No such file: {p}")
    rows = read_granola_csv(paths)
    if args.limit:
        rows = rows[: args.limit]

    notes, bad = [], []
    for row in rows:
        try:
            notes.append(build_note(row, args))
        except ValueError as e:
            bad.append(f"{row.get('document_title') or row['document_id']}: {e}")
    notes.sort(key=lambda n: n["created_at"])

    data_dir = Path(args.data_dir).expanduser()
    if not args.dry_run and humla_is_running() and not args.allow_running:
        sys.exit(
            "Humla is running. Quit it first (Humla > Quit), then run this again.\n"
            "It only indexes newly imported notes for search when it starts."
        )

    conn = open_humla_db(data_dir)
    cols = note_columns(conn)
    existing = {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, created_at, updated_at, "
            + ("deleted_at" if "deleted_at" in cols else "NULL AS deleted_at")
            + " FROM notes"
        )
    }

    to_insert = [n for n in notes if n["id"] not in existing]
    to_update, skipped_present, skipped_edited = [], [], []
    for n in notes:
        prior = existing.get(n["id"])
        if prior is None:
            continue
        if not args.update:
            skipped_present.append(n)
        elif prior["updated_at"] != prior["created_at"] and not args.overwrite_edited:
            # The importer writes updated_at = created_at; anything else means
            # the note was edited in Humla since.
            skipped_edited.append(n)
        else:
            to_update.append(n)

    with_body = sum(1 for n in to_insert + to_update if n["body"])
    print(f"Granola documents read: {len(rows)} from {len(paths)} file(s)")
    print(f"Humla database:         {data_dir / DB_FILE}")
    print(f"  new notes:            {len(to_insert)}")
    if args.update:
        print(f"  refreshed:            {len(to_update)}")
        if skipped_edited:
            print(f"  kept (edited in Humla since import): {len(skipped_edited)}")
    elif skipped_present:
        print(f"  already imported:     {len(skipped_present)} (skipped; --update refreshes them)")
    print(f"  with your own notes:  {with_body}")
    print(f"  note language:        {describe_language(args.language, to_insert + to_update)}")
    for msg in bad:
        print(f"  skipped, bad date:    {msg}")

    if args.dry_run:
        print("\nDry run, nothing written. First few:")
        for n in (to_insert + to_update)[:8]:
            when = dt.datetime.fromtimestamp(n["created_at"] / 1000).strftime("%Y-%m-%d %H:%M")
            print(
                f"  {when}  {n['title'][:48]:<48}  transcript {len(n['transcript'].splitlines()):>5} lines"
                f"  summary {len(n['summary']):>5} chars  notes {'yes' if n['body'] else 'no':<3}"
                f"  language {n['language'] or 'default'}"
            )
        conn.close()
        return 0

    if not to_insert and not to_update:
        print("\nNothing to write.")
        conn.close()
        return 0

    if not args.no_backup:
        print(f"\nBackup: {backup_db(conn, data_dir)}")

    summary_preset = (setting(conn, "default_summary_preset") or "").strip() or "meeting"
    now = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)

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

    try:
        with conn:  # one transaction: all or nothing
            folder_id = None
            if args.folder:
                folder_id, created = ensure_folder(conn, args.folder, now)
                print(f"Folder: {args.folder}" + (" (created)" if created else ""))

            for n in to_insert:
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

            for n in to_update:
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
    finally:
        conn.close()

    print(f"\nImported {len(to_insert)} note(s)" + (f", refreshed {len(to_update)}" if to_update else "") + ".")
    print("Open Humla: it indexes the new notes for search and chat as it starts.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import a Granola CSV export into Humla.",
        epilog="Quit Humla before importing. Imported notes land in your Personal space.",
    )
    parser.add_argument("csv", nargs="+", help="Granola export CSV file(s)")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help=f"Humla data folder (default: {DEFAULT_DATA_DIR})")
    parser.add_argument("--dry-run", action="store_true", help="show what would be imported, write nothing")
    parser.add_argument("--folder", metavar="NAME", help="file the imported notes in this folder (created if missing)")
    parser.add_argument("--update", action="store_true", help="refresh notes imported by an earlier run")
    parser.add_argument(
        "--overwrite-edited",
        action="store_true",
        help="with --update, also overwrite notes you have edited in Humla since importing",
    )
    parser.add_argument(
        "--strip-granola-links",
        action="store_true",
        help='drop the "Chat with meeting transcript: <granola link>" footer from summaries',
    )
    parser.add_argument(
        "--language",
        type=parse_language,
        default="detect",
        metavar="CODE",
        help="each note's language: detect (default; from its transcript, auto when unclear), "
        "auto, a code such as en or it, or default (Humla's own setting)",
    )
    parser.add_argument("--limit", type=int, metavar="N", help="import only the first N documents (for a trial run)")
    parser.add_argument("--no-backup", action="store_true", help="skip the database backup")
    parser.add_argument("--allow-running", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
