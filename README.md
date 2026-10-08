# granola-to-humla

Imports a [Granola](https://granola.ai) CSV export into [Humla](https://github.com/michaelwilhelmsen/humla), the open-source macOS meeting-notes app. Each Granola document becomes one Humla note, with the transcript, the AI summary and your own typed notes each in the place Humla keeps them.

This is an independent project, not affiliated with Granola or Humla.

## Requirements

- macOS with Humla installed and opened at least once.
- Python 3.9 or newer. The `python3` that comes with Xcode's command line tools is fine.
- [markdown-it-py](https://github.com/executablebooks/markdown-it-py), the one dependency. Both ways of running the importer below install it for you.
- A Granola CSV export with the columns `document_id`, `document_title`, `document_created`, `summary`, `notes` and `transcript`. The script stops with a clear message if any are missing.

## Installing

With [uv](https://docs.astral.sh/uv/), there is nothing to install: `uvx` fetches the importer and its dependency and runs it.

```sh
uvx --from git+https://github.com/tommasoturchi/granola-to-humla granola-to-humla --help
```

Without uv, install it into a virtual environment of its own:

```sh
python3 -m venv ~/.granola-to-humla
~/.granola-to-humla/bin/pip install git+https://github.com/tommasoturchi/granola-to-humla
~/.granola-to-humla/bin/granola-to-humla --help
```

The steps below write `granola-to-humla` for whichever of these you used.

## Usage

1. Launch Humla at least once so it creates its database, then quit it (Humla > Quit). The script refuses to run while Humla is open.
2. Preview the import:

   ```sh
   granola-to-humla ~/Downloads/granola-export-*.csv --dry-run
   ```

3. Import:

   ```sh
   granola-to-humla ~/Downloads/granola-export-*.csv --folder Granola
   ```

4. Open Humla. On launch it indexes the new notes for search and chat (see below).

Before writing anything the script saves a consistent copy of the database next to it, as `notes.sqlite.before-granola-import-<timestamp>`.

## What goes where

| Granola CSV column | Humla note field | Stored as |
| --- | --- | --- |
| `document_title` | title | plain text |
| `notes` (what you typed) | notes body | Tiptap HTML, the format Humla's editor saves |
| `summary` (AI notes) | summary | Markdown, unchanged |
| `transcript` | transcript | plain text, one utterance per line, unchanged |
| `document_created` | created and updated time | epoch milliseconds |
| (detected) | note language | the transcript's language, such as `it` or `en`, or `auto` when unclear (see `--language`) |

`user_email` and `workspace_name` are not used. Notes land in your Personal space, which in Humla is local-only.

Granola's notes are Markdown. The script parses them with markdown-it-py, which follows the CommonMark standard, so two-space nested bullets nest the way Granola writes them, and converts headings, nested bullet and numbered lists, bold, italics, strikethrough, inline code, code blocks, quotes and rules to the HTML Humla's editor uses. Humla's editor has no links or images, so both are written as `label (url)` to keep the address visible and searchable, and headings below level 4 become level 4, the smallest the editor has. Summaries stay Markdown because that is what Humla renders them from.

## Options

| Option | Effect |
| --- | --- |
| `--dry-run` | Print counts and a sample; write nothing. |
| `--folder NAME` | File imported notes in this Humla folder, created if it doesn't exist. |
| `--update` | Refresh notes from an earlier import with the CSV's content. Notes you have edited in Humla since importing are left alone. |
| `--overwrite-edited` | With `--update`, overwrite those edited notes too. |
| `--language CODE` | Each note's language. `detect` (the default) reads it from the transcript, see below. `auto` leaves every note on Humla's auto-detect, a code such as `en` or `it` gives all of them that language, and `default` follows Humla's app-wide setting. |
| `--strip-granola-links` | Remove the "Chat with meeting transcript: <granola link>" footer Granola appends to every summary. Kept by default. |
| `--limit N` | Import only the first N documents, for a trial. |
| `--data-dir PATH` | Humla's data folder. Default: `~/Library/Application Support/no.humla.app`. |
| `--no-backup` | Skip the database backup. |

Running the script twice does not duplicate anything. Each note's id is derived from its Granola `document_id`, so documents already imported are skipped, and a newer export with extra meetings imports only the new ones.

## How it fits Humla's data model

The script writes rows to Humla's `notes` table (and `folders`, with `--folder`) and nothing else.

Humla normally builds a note's transcript from per-recording timelines with speaker labels and word timings ([ADR-0004](https://github.com/michaelwilhelmsen/humla/blob/main/docs/adr/0004-the-timeline-is-canonical-for-a-notes-content.md)). Granola's export has neither labels nor timings, and the script doesn't invent them: each transcript is imported exactly as Granola wrote it, with no recording session behind it. The ADR keeps that shape for notes without audio: the Transcript tab shows the text in its plain editor, it stays editable, and the timeline operations that rebuild a transcript cannot clear it.

Search chunks, the speaker list and embeddings are derived data that Humla maintains itself. At startup it indexes every note that has no chunks yet, then embeds them with whatever embedding provider you have configured. The importer leaves all of that to Humla instead of reimplementing its chunker. If your chat provider is OpenAI, the first launch after the import sends the imported text through `text-embedding-3-small` once, on your API key.

Each note's language is detected from its transcript, by counting common function words in English, Italian, Spanish, French, German, Portuguese, Dutch, Swedish, Norwegian and Danish. Only words that belong to one of those languages count, so a word like `de` or `la` that several of them share never tips the balance. A note gets a language when at least 20 such words turn up and three quarters of them point the same way; a transcript that is short, mixed or in another language stays on `auto`. In a test export of 226 meetings that gave 120 Italian, 103 English and 3 mixed.

The language doesn't change the imported text. Humla writes in it when you press Summarize on an imported note or ask for a new title, and its MCP server reports it, so a connected agent knows which language to search in. Humla's app-wide language is Norwegian unless you picked another during setup or in Settings, which is why imported notes get their own language instead of following that setting.

Each note's updated time is set to the meeting date, so imported meetings sort by date in Humla's lists instead of all appearing under today.

## Known quirks

Humla reads a line that starts with up to 40 characters followed by a colon and a space as a speaker label. Granola sometimes breaks a line mid-sentence, so a line such as `and then she said: we ship on Friday` gets picked up as a speaker called "and then she said" in Humla's speaker filters. In one export of 226 meetings this affected 18 of them. The transcript text itself is imported unchanged.

## Undo

Every run that writes saves a backup first, named `notes.sqlite.before-granola-import-<timestamp>`. Restoring the earliest one puts your library back exactly as it was before your first import, which also throws away anything else you changed in Humla since then, including meetings you recorded after the import.

1. Quit Humla. If you turned on its MCP server (Settings > General > Integrations), also quit the apps you connected to it, such as Claude Code or Codex: the server keeps the library open for as long as they run.
2. In `~/Library/Application Support/no.humla.app/`, list the backups and pick the earliest:

   ```sh
   cd ~/Library/Application\ Support/no.humla.app
   ls notes.sqlite.before-granola-import-*
   ```

3. Move SQLite's two side files out of the way, then copy the backup over the library:

   ```sh
   mkdir -p ~/humla-wal-old && mv notes.sqlite-wal notes.sqlite-shm ~/humla-wal-old/ 2>/dev/null
   cp notes.sqlite.before-granola-import-<timestamp> notes.sqlite
   ```

Don't skip the move. If Humla was killed rather than quit, its most recent changes are still sitting in `notes.sqlite-wal`, and SQLite would replay them onto the restored file. In testing that left the database failing SQLite's integrity check.

## Tests

From a clone of this repository:

```sh
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/python -m unittest test_granola_to_humla
```

The importer was also checked against Humla's own Rust database code (`src-tauri/src/db.rs` from version 0.65.2, unchanged in 0.66.0, compiled into a small harness): its schema and migrations open the imported database, its startup backfill indexes every imported note, keyword search returns hits from transcripts, summaries and notes, and every transcript and summary matches the CSV text exactly, apart from surrounding whitespace.

## Licence

MIT. See [LICENSE](LICENSE).
