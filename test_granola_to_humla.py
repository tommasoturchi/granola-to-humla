"""Tests for granola_to_humla.py. Run with: python3 -m unittest test_granola_to_humla"""

import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path

import typer
from typer.testing import CliRunner

import granola_to_humla as g

RUNNER = CliRunner()


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

    def test_links_and_images_keep_their_url_as_text(self):
        # Humla's editor has no links or images, so <a>/<img> would lose the URL.
        md = g.markdown_to_tiptap
        self.assertEqual(md("[paper](https://x.org/p)"), "<p>paper (https://x.org/p)</p>")
        self.assertEqual(md("[https://x.org](https://x.org)"), "<p>https://x.org</p>")
        self.assertEqual(md("[a@b.c](mailto:a@b.c)"), "<p>a@b.c</p>")
        self.assertEqual(md("<https://q.r>"), "<p>https://q.r</p>")
        self.assertEqual(md("![chart](https://x.org/c.png)"), "<p>chart (https://x.org/c.png)</p>")

    def test_inline_marks_and_escaping(self):
        self.assertEqual(
            g.markdown_to_tiptap("**b** *i* ~~s~~ `x<y` a & b snake_case 2 * 3"),
            "<p><strong>b</strong> <em>i</em> <s>s</s> <code>x&lt;y</code> a &amp; b snake_case 2 * 3</p>",
        )

    def test_raw_html_is_shown_as_text(self):
        self.assertEqual(g.markdown_to_tiptap("<b>hi</b>"), "<p>&lt;b&gt;hi&lt;/b&gt;</p>")

    def test_soft_breaks_inside_a_paragraph_are_kept(self):
        self.assertEqual(g.markdown_to_tiptap("line one\nline two"), "<p>line one<br>line two</p>")


# Made-up speech in each supported language.
SAMPLES = {
    "en": "Okay so I think the main thing is that we just need to know what they want, because "
          "it's not really clear yet. Yeah, and there would be something about the budget that "
          "we should look at, which I can't really decide alone.",
    "it": "Allora, quindi il punto è che non abbiamo ancora deciso. Però magari possiamo fare "
          "così: questo lo vediamo dopo, perché adesso è molto più importante capire cosa "
          "vogliono gli studenti, cioè anche dove mettere le domande.",
    "es": "Bueno, pues yo creo que el problema es que todavía no está claro. Entonces, cuando "
          "hay tiempo, podemos hacer esto también, porque ahora es muy importante entender lo "
          "que quieren los usuarios y las empresas.",
    "fr": "Alors voilà, je pense que le problème c'est que nous ne savons pas encore. Donc il "
          "faut faire une liste des points, parce que ça peut aussi changer, et elle est très "
          "importante pour vous et pour les autres dans le projet.",
    "de": "Also ich denke, das Problem ist, dass wir noch nicht wissen, wie das genau aussieht. "
          "Aber wenn wir jetzt schon eine Liste haben, dann ist das sehr gut, und die anderen "
          "sind auch dabei, oder? Ich bin mir nicht sicher.",
    "pt": "Então, eu acho que o problema é que ainda não está claro. Mas você sabe, isso é muito "
          "importante para nós, e ela também tem uma ideia boa. Foi bem aqui que começamos, "
          "na reunião, e ele quer fazer isto com os outros.",
    "nl": "Nou, ik denk dat het probleem is dat we het nog niet weten. Maar je moet wel heel "
          "goed kijken, want dan zijn we eigenlijk ook klaar. Dus we hebben gewoon een lijst "
          "nodig van alle punten voor de vergadering.",
    "sv": "Jag tror att problemet är att vi inte vet ännu. Men det är väldigt viktigt att göra "
          "en lista, och vad vi ska göra också. Hur har det varit där? Det är riktigt bra att "
          "du kom, och vi ska ses igen.",
    "no": "Jeg tror kanskje at problemet er at vi ikke vet det ennå. Men det er veldig viktig "
          "å gjøre litt mer, og hva vi skal gjøre nå. Det ble mye snakk om noe sånn, og det har "
          "vært riktig bra.",
    "da": "Jeg tror måske at problemet er at vi ikke ved det endnu. Men det er meget vigtigt at "
          "gøre lidt mere, og hvad vi skal gøre nu. Det blev noget snak om sådan noget, og det "
          "har været rigtig godt.",
}


class LanguageDetection(unittest.TestCase):
    def test_each_supported_language_is_recognised(self):
        for lang, text in SAMPLES.items():
            with self.subTest(lang=lang):
                self.assertEqual(g.detect_language(" ".join([text] * 3)), lang)

    def test_unclear_text_gets_no_language(self):
        self.assertIsNone(g.detect_language(" ".join(SAMPLES["it"].split()[:30])))  # too short
        half_and_half = " ".join([SAMPLES["it"]] * 4 + [SAMPLES["en"]] * 4)
        self.assertIsNone(g.detect_language(half_and_half))
        self.assertIsNone(g.detect_language(""))

    def test_a_meeting_mostly_in_one_language_gets_it(self):
        mostly_italian = " ".join([SAMPLES["it"]] * 9 + [SAMPLES["en"]])
        self.assertEqual(g.detect_language(mostly_italian), "it")

    def test_only_codes_from_humlas_picker(self):
        self.assertIn("no", g.HUMLA_LANGUAGES)
        self.assertNotIn("auto", g.HUMLA_LANGUAGES)
        self.assertEqual(len(g.HUMLA_LANGUAGES), 100)


class Fields(unittest.TestCase):
    def test_timestamps(self):
        self.assertEqual(g.parse_created("2026-09-14T14:28:28.349Z"), 1789396108349)
        self.assertEqual(g.parse_created("2026-09-14T16:28:28.349+02:00"), 1789396108349)
        self.assertEqual(g.parse_created("2026-09-14T14:28:28Z"), 1789396108000)

    def test_timestamps_without_an_offset_are_utc(self):
        self.assertEqual(g.parse_created("2026-09-14T14:28:28.349"), 1789396108349)
        with self.assertRaises(ValueError):
            g.parse_created("14/09/2026")

    def test_language_option(self):
        self.assertEqual(g._language_option("detect"), "detect")
        self.assertEqual(g._language_option("auto"), "auto")
        self.assertEqual(g._language_option(" EN "), "en")
        self.assertEqual(g._language_option("yue"), "yue")
        self.assertEqual(g._language_option("default"), "")
        for bad in ("english", "xx", "en-US", ""):
            with self.assertRaises(typer.BadParameter):
                g._language_option(bad)

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
            w.writerow(["d3", "a@b.c", "Planning", "Personal", "2026-01-04T09:00:00.000Z", "## Plan", "", "\n".join([SAMPLES["en"]] * 3)])

    def tearDown(self):
        self.tmp.cleanup()

    def run_import(self, *extra, ok=True):
        result = RUNNER.invoke(g.app, [str(self.csv), "--data-dir", str(self.dir), "--no-backup", *extra])
        if ok:
            self.assertEqual(result.exit_code, 0, result.output)
        return result

    def rows(self):
        conn = sqlite3.connect(self.dir / "notes.sqlite")
        conn.row_factory = sqlite3.Row
        out = {r["title"]: dict(r) for r in conn.execute("SELECT * FROM notes")}
        conn.close()
        return out

    def test_fields_land_in_the_right_columns(self):
        self.run_import("--yes", "--folder", "Granola")
        n = self.rows()["Standup"]
        self.assertEqual(n["body"], "<ul><li><p>send the slides</p></li></ul>")
        self.assertEqual(n["transcript"], "Hello.\nHi there.")
        self.assertEqual(n["summary"], "## Done\n\n- shipped")
        self.assertEqual(n["created_at"], n["updated_at"])
        self.assertEqual(n["workspace_id"], "")
        self.assertIsNotNone(n["folder_id"])
        self.assertEqual(self.rows()["Review"]["body"], "")
        self.assertEqual(n["language"], "auto")  # too little text to tell
        self.assertEqual(self.rows()["Planning"]["language"], "en")

    def test_language_can_be_set_and_refreshed(self):
        self.run_import("--yes", "--language", "en")
        self.assertEqual(self.rows()["Standup"]["language"], "en")
        self.run_import("--yes", "--update", "--language", "default")
        self.assertEqual(self.rows()["Standup"]["language"], "")
        self.run_import("--yes", "--update", "--language", "auto")
        self.assertEqual(self.rows()["Planning"]["language"], "auto")

    def test_dry_run_writes_nothing(self):
        result = self.run_import("--dry-run")
        self.assertIn("Dry run", result.output)
        self.assertEqual(self.rows(), {})

    def test_it_asks_before_writing_and_wont_guess_without_a_terminal(self):
        result = self.run_import(ok=False)
        self.assertEqual(result.exit_code, 1)
        self.assertIn("--yes", result.output)
        self.assertEqual(self.rows(), {})

    def test_rerun_is_a_no_op_and_update_spares_edited_notes(self):
        self.run_import("--yes")
        conn = sqlite3.connect(self.dir / "notes.sqlite")
        conn.execute("UPDATE notes SET body = '<p>mine</p>', updated_at = updated_at + 1 WHERE title = 'Standup'")
        conn.commit()
        conn.close()
        self.run_import("--yes")
        self.assertEqual(len(self.rows()), 3)
        self.run_import("--yes", "--update")
        self.assertEqual(self.rows()["Standup"]["body"], "<p>mine</p>")
        self.run_import("--yes", "--update", "--overwrite-edited")
        self.assertEqual(self.rows()["Standup"]["body"], "<ul><li><p>send the slides</p></li></ul>")


if __name__ == "__main__":
    unittest.main()
