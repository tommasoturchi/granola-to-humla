"""Tests for granola_to_humla.py. Run with: python3 -m unittest test_granola_to_humla"""

import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path

import granola_to_humla as g


class MarkdownToTiptap(unittest.TestCase):
    def test_bullets_use_tiptap_list_item_shape(self):
        self.assertEqual(
            g.markdown_to_tiptap("- one\n- two"),
            "<ul><li><p>one</p></li><li><p>two</p></li></ul>",
        )

    def test_nested_bullets(self):
        self.assertEqual(
            g.markdown_to_tiptap("- a\n  - b\n- c"),
            "<ul><li><p>a</p><ul><li><p>b</p></li></ul></li><li><p>c</p></li></ul>",
        )

    def test_blank_lines_between_bullets_keep_one_list(self):
        self.assertEqual(
            g.markdown_to_tiptap("- a\n\n- b"),
            "<ul><li><p>a</p></li><li><p>b</p></li></ul>",
        )

    def test_empty_bullets_are_dropped(self):
        self.assertEqual(g.markdown_to_tiptap("- a\n-"), "<ul><li><p>a</p></li></ul>")
        self.assertEqual(g.markdown_to_tiptap("-\n"), "")

    def test_indented_paragraphs_stay_inside_their_item(self):
        self.assertEqual(
            g.markdown_to_tiptap("- head\n\n  \\- detail\n- next"),
            "<ul><li><p>head</p><p>- detail</p></li><li><p>next</p></li></ul>",
        )

    def test_ordered_list_keeps_its_start(self):
        self.assertEqual(g.markdown_to_tiptap("3. a\n4. b"), '<ol start="3"><li><p>a</p></li><li><p>b</p></li></ol>')
        self.assertEqual(g.markdown_to_tiptap("1) a"), "<ol><li><p>a</p></li></ol>")

    def test_headings_clamp_to_the_editors_levels(self):
        self.assertEqual(g.markdown_to_tiptap("## Two\n###### Six"), "<h2>Two</h2><h4>Six</h4>")

    def test_links_keep_their_url_as_text(self):
        # Humla's editor has no Link extension, so <a> would lose the URL.
        self.assertEqual(g.render_inline("[paper](https://x.org/p)"), "paper (https://x.org/p)")
        self.assertEqual(g.render_inline("[https://x.org](https://x.org)"), "https://x.org")
        self.assertEqual(g.render_inline("[a@b.c](mailto:a@b.c)"), "a@b.c")

    def test_inline_marks_and_escaping(self):
        self.assertEqual(
            g.render_inline("**b** *i* ~~s~~ `x<y` a & b snake_case 2 * 3"),
            "<strong>b</strong> <em>i</em> <s>s</s> <code>x&lt;y</code> a &amp; b snake_case 2 * 3",
        )

    def test_soft_breaks_inside_a_paragraph_are_kept(self):
        self.assertEqual(g.markdown_to_tiptap("line one\nline two"), "<p>line one<br>line two</p>")


class Fields(unittest.TestCase):
    def test_timestamps(self):
        self.assertEqual(g.parse_created("2026-09-14T14:28:28.349Z"), 1789396108349)
        self.assertEqual(g.parse_created("2026-09-14T16:28:28.349+02:00"), 1789396108349)
        self.assertEqual(g.parse_created("2026-09-14T14:28:28Z"), 1789396108000)

    def test_language_option(self):
        self.assertEqual(g.parse_language("auto"), "auto")
        self.assertEqual(g.parse_language(" EN "), "en")
        self.assertEqual(g.parse_language("yue"), "yue")
        self.assertEqual(g.parse_language("default"), "")
        for bad in ("english", "e", "en-US", ""):
            with self.assertRaises(g.argparse.ArgumentTypeError):
                g.parse_language(bad)

    def test_note_ids_are_stable(self):
        a = g.note_id_for("0b5e7a52-6d1c-4f3e-9a8d-2c7f1e4b9d10")
        self.assertEqual(a, g.note_id_for(" 0b5e7a52-6d1c-4f3e-9a8d-2c7f1e4b9d10 "))
        self.assertNotEqual(a, g.note_id_for("00000000-0000-0000-0000-000000000000"))

    def test_transcript_lines_are_untouched(self):
        self.assertEqual(g.clean_transcript("Hi.  \r\nHello: there\n"), "Hi.\nHello: there")

    def test_granola_footer_is_kept_unless_asked(self):
        s = "# T\n\n- x\n\n---\n\nChat with meeting transcript: [https://notes.granola.ai/t/1](https://notes.granola.ai/t/1)"
        self.assertEqual(g.clean_summary(s, False), s)
        self.assertEqual(g.clean_summary(s, True), "# T\n\n- x")


class EndToEnd(unittest.TestCase):
    """Against a minimal notes/folders schema. The full check against Humla's
    own db.rs (schema, startup re-index, search) is described in the README."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        conn = sqlite3.connect(self.dir / "notes.sqlite")
        conn.executescript(
            """
            CREATE TABLE notes (id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
              transcript TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '', audio_path TEXT,
              summary_preset TEXT NOT NULL DEFAULT 'meeting', folder_id TEXT, language TEXT NOT NULL DEFAULT '',
              created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, workspace_id TEXT NOT NULL DEFAULT '',
              deleted_at INTEGER);
            CREATE TABLE folders (id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at INTEGER NOT NULL,
              updated_at INTEGER NOT NULL, workspace_id TEXT NOT NULL DEFAULT '');
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        conn.close()
        self.csv = self.dir / "export.csv"
        with self.csv.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["document_id", "user_email", "document_title", "workspace_name", "document_created", "summary", "notes", "transcript"])
            w.writerow(["d1", "a@b.c", "Standup", "Personal", "2026-01-02T09:00:00.000Z", "## Done\n\n- shipped", "- send the slides", "Hello.\nHi there."])
            w.writerow(["d2", "a@b.c", "Review", "Personal", "2026-01-03T09:00:00.000Z", "## Notes\n\n- fine", "", "Okay."])

    def tearDown(self):
        self.tmp.cleanup()

    def run_import(self, *extra):
        return g.main([str(self.csv), "--data-dir", str(self.dir), "--no-backup", *extra])

    def rows(self):
        conn = sqlite3.connect(self.dir / "notes.sqlite")
        conn.row_factory = sqlite3.Row
        out = {r["title"]: dict(r) for r in conn.execute("SELECT * FROM notes")}
        conn.close()
        return out

    def test_fields_land_in_the_right_columns(self):
        self.run_import("--folder", "Granola")
        n = self.rows()["Standup"]
        self.assertEqual(n["body"], "<ul><li><p>send the slides</p></li></ul>")
        self.assertEqual(n["transcript"], "Hello.\nHi there.")
        self.assertEqual(n["summary"], "## Done\n\n- shipped")
        self.assertEqual(n["created_at"], n["updated_at"])
        self.assertEqual(n["workspace_id"], "")
        self.assertIsNotNone(n["folder_id"])
        self.assertEqual(self.rows()["Review"]["body"], "")
        self.assertEqual(n["language"], "auto")

    def test_language_can_be_set_and_refreshed(self):
        self.run_import("--language", "en")
        self.assertEqual(self.rows()["Standup"]["language"], "en")
        self.run_import("--update", "--language", "default")
        self.assertEqual(self.rows()["Standup"]["language"], "")

    def test_rerun_is_a_no_op_and_update_spares_edited_notes(self):
        self.run_import()
        conn = sqlite3.connect(self.dir / "notes.sqlite")
        conn.execute("UPDATE notes SET body = '<p>mine</p>', updated_at = updated_at + 1 WHERE title = 'Standup'")
        conn.commit()
        conn.close()
        self.run_import()
        self.assertEqual(len(self.rows()), 2)
        self.run_import("--update")
        self.assertEqual(self.rows()["Standup"]["body"], "<p>mine</p>")
        self.run_import("--update", "--overwrite-edited")
        self.assertEqual(self.rows()["Standup"]["body"], "<ul><li><p>send the slides</p></li></ul>")


if __name__ == "__main__":
    unittest.main()
