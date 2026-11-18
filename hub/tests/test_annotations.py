import io, json, os, sys, tempfile, unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))
import swarm_annotations as sa

SUBMIT = {"received": "2026-10-01T12:00:05Z", "payload": {"event": "submit", "timestamp": 1790858400000,
    "url": "https://site.example.com/", "output": "## notes\n- fix button",
    "annotations": [
        {"id": "1", "x": 42.5, "y": 320, "comment": "fix the button contrast", "element": "BUTTON",
         "elementPath": "main > div.hero > button", "timestamp": 1790858400000,
         "boundingBox": {"x": 300, "y": 320, "width": 120, "height": 40}, "selectedText": "Get started"},
        {"id": "2", "comment": "typo in hero heading", "elementPath": "h1", "timestamp": 1790858401000}]}}
ADD = {"received": "2026-10-01T11:00:00Z", "payload": {"event": "annotation.add", "timestamp": 1790854800000,
    "url": "https://site.example.com/swarm/",
    "annotation": {"id": "3", "x": 10.5, "y": 200, "comment": "this card is off",
                   "element": "DIV", "elementPath": "div.card", "timestamp": 1790854800000}}}


class AnnotationTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def write(self, name, obj):
        (self.d / name).write_text(obj if isinstance(obj, str) else json.dumps(obj))

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = sa.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def populate(self):
        self.write("20261001T110000Z-aaaa1111.json", ADD)
        self.write("20261001T120000Z-bbbb2222.json", SUBMIT)

    def test_list_table(self):
        self.populate()
        rc, out, _ = self.cli("list", "--dir", str(self.d))
        self.assertEqual(rc, 0)
        self.assertIn("20261001T110000Z-aaaa1111.json", out)
        self.assertIn("20261001T120000Z-bbbb2222.json", out)
        self.assertIn("https://site.example.com/", out)
        self.assertIn("https://site.example.com/swarm/", out)
        self.assertIn("fix the button contrast", out)
        self.assertLess(out.index("aaaa1111"), out.index("bbbb2222"))

    def test_list_newer_than_timestamp(self):
        self.populate()
        rc, out, _ = self.cli("list", "--dir", str(self.d), "--newer-than", "2026-10-01T11:30:00Z")
        self.assertEqual(rc, 0)
        self.assertNotIn("aaaa1111", out)
        self.assertIn("bbbb2222", out)

    def test_list_newer_than_file_mtime(self):
        self.populate()
        marker = self.d / "marker"
        marker.write_text("")
        os.utime(marker, (1790854200, 1790854200))  # 2026-10-01T11:30:00Z, between the two files
        rc, out, _ = self.cli("list", "--dir", str(self.d), "--newer-than", str(marker))
        self.assertEqual(rc, 0)
        self.assertNotIn("aaaa1111", out)
        self.assertIn("bbbb2222", out)

    def test_show_details(self):
        self.populate()
        rc, out, _ = self.cli("show", "2026*", "--dir", str(self.d))
        self.assertEqual(rc, 0)
        self.assertIn("fix the button contrast", out)
        self.assertIn("main > div.hero > button", out)
        self.assertIn("boundingBox", out)
        self.assertIn("selectedText", out)
        self.assertIn("this card is off", out)

    def test_show_single_file_by_name(self):
        self.populate()
        rc, out, _ = self.cli("show", "20261001T120000Z-bbbb2222.json", "--dir", str(self.d))
        self.assertEqual(rc, 0)
        self.assertIn("submit", out)
        self.assertNotIn("this card is off", out)

    def test_show_unmatched_pattern(self):
        self.populate()
        rc, _out, err = self.cli("show", "nomatch*", "--dir", str(self.d))
        self.assertEqual(rc, 1)
        self.assertIn("no matching", err)

    def test_export_normalized(self):
        self.populate()
        rc, out, _ = self.cli("export", "--dir", str(self.d))
        self.assertEqual(rc, 0)
        rows = json.loads(out)
        self.assertEqual(len(rows), 3)
        self.assertEqual(set(rows[0]), {"file", "ts", "url", "selector", "comment", "extra"})
        by_comment = {r["comment"]: r for r in rows}
        self.assertEqual(by_comment["fix the button contrast"]["selector"], "main > div.hero > button")
        self.assertEqual(by_comment["fix the button contrast"]["file"], "20261001T120000Z-bbbb2222.json")
        self.assertEqual(by_comment["fix the button contrast"]["extra"]["event"], "submit")
        self.assertEqual(by_comment["this card is off"]["extra"]["event"], "annotation.add")
        self.assertEqual(by_comment["this card is off"]["url"], "https://site.example.com/swarm/")

    def test_export_out_file(self):
        self.populate()
        dest = self.d / "merged.json"
        rc, out, _ = self.cli("export", "--dir", str(self.d), "--out", str(dest))
        self.assertEqual(rc, 0)
        rows = json.loads(dest.read_text())
        self.assertEqual(len(rows), 3)
        self.assertIn("wrote", out)

    def test_summary_counts(self):
        self.populate()
        rc, out, _ = self.cli("summary", "--dir", str(self.d))
        self.assertEqual(rc, 0)
        self.assertIn("2  https://site.example.com/", out)
        self.assertIn("1  https://site.example.com/swarm/", out)
        self.assertIn("total: 3 annotation(s) across 2 file(s)", out)

    def test_malformed_file_warned_and_skipped(self):
        self.populate()
        self.write("20261001T130000Z-bad.json", "{not valid json")
        self.write("20261001T140000Z-alsolist.json", "[1, 2, 3]")
        rc, out, err = self.cli("list", "--dir", str(self.d))
        self.assertEqual(rc, 0)
        self.assertIn("20261001T130000Z-bad.json", err)
        self.assertIn("20261001T140000Z-alsolist.json", err)
        self.assertIn("bbbb2222", out)

    def test_empty_dir_exit_0(self):
        for cmd in (["list"], ["summary"], ["export"], ["show", "*.json"]):
            rc, out, _ = self.cli(*cmd, "--dir", str(self.d))
            self.assertEqual(rc, 0, cmd)
            self.assertIn("no annotations", out)

    def test_missing_dir_exit_0(self):
        gone = self.d / "nope"
        for cmd in (["list"], ["summary"], ["export"], ["show", "*.json"]):
            rc, out, _ = self.cli(*cmd, "--dir", str(gone))
            self.assertEqual(rc, 0, cmd)
            self.assertIn("does not exist", out)

    def test_annotation_missing_keys_tolerated(self):
        self.write("20261001T150000Z-sparse.json",
                   {"payload": {"event": "submit", "annotations": [{"comment": "bare"}, {}]}})
        rc, out, _ = self.cli("show", "*.json", "--dir", str(self.d))
        self.assertEqual(rc, 0)
        self.assertIn("bare", out)
        rc, out, _ = self.cli("export", "--dir", str(self.d))
        self.assertEqual(rc, 0)
        rows = json.loads(out)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["comment"], "")
        self.assertEqual(rows[1]["selector"], "")

    def test_env_dir_default(self):
        self.populate()
        saved = os.environ.get("SWARM_ANNOTATIONS_DIR")
        os.environ["SWARM_ANNOTATIONS_DIR"] = str(self.d)
        try:
            rc, out, _ = self.cli("summary")
        finally:
            if saved is None:
                del os.environ["SWARM_ANNOTATIONS_DIR"]
            else:
                os.environ["SWARM_ANNOTATIONS_DIR"] = saved
        self.assertEqual(rc, 0)
        self.assertIn("total: 3", out)


if __name__ == "__main__":
    unittest.main()
