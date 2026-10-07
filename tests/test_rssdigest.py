import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import rssdigest  # noqa: E402


class FakeParsed:
    """Stand-in for feedparser.parse() output."""

    def __init__(self, entries, feed=None, bozo=0, bozo_exception=None):
        self.entries = entries
        self.feed = feed or {}
        self.bozo = bozo
        self.bozo_exception = bozo_exception


class CleanTests(unittest.TestCase):
    def test_strip_html_flattens_and_unescapes(self):
        self.assertEqual(
            rssdigest.strip_html("<p>Hello &amp; <b>world</b></p>\n\n  x"),
            "Hello & world x")
        self.assertEqual(rssdigest.strip_html(""), "")
        self.assertEqual(rssdigest.strip_html(None), "")

    def test_entry_content_prefers_content_encoded(self):
        entry = {"content": [{"value": "full body"}], "summary": "short"}
        self.assertEqual(rssdigest.entry_content(entry), "full body")

    def test_entry_content_falls_back_to_summary(self):
        self.assertEqual(rssdigest.entry_content({"summary": "short"}), "short")
        self.assertEqual(rssdigest.entry_content({"description": "d"}), "d")
        self.assertEqual(rssdigest.entry_content({}), "")

    def test_excerpt_truncates_on_word_boundary(self):
        out = rssdigest.excerpt("one two three four", limit=7)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(len(out), 8)
        self.assertEqual(rssdigest.excerpt("short", limit=99), "short")


class ConfigTests(unittest.TestCase):
    def test_env_list_splits_and_trims(self):
        with mock.patch.dict(os.environ, {"X": " a , b ,,c "}, clear=False):
            self.assertEqual(rssdigest.env_list("X", []), ["a", "b", "c"])

    def test_env_list_default_when_unset(self):
        os.environ.pop("Y", None)
        self.assertEqual(rssdigest.env_list("Y", ["d"]), ["d"])

    def test_resolve_state_no_state_wins(self):
        args = types.SimpleNamespace(no_state=True, state="/tmp/s")
        self.assertIsNone(rssdigest.resolve_state(args))

    def test_resolve_state_flag_over_env(self):
        args = types.SimpleNamespace(no_state=False, state="/tmp/flag")
        with mock.patch.dict(os.environ, {"RSS_STATE": "/tmp/env"}, clear=False):
            self.assertEqual(rssdigest.resolve_state(args), "/tmp/flag")


class JsonExtractionTests(unittest.TestCase):
    def test_plain_array(self):
        self.assertEqual(rssdigest.extract_json_array('[{"id": 1}]'), [{"id": 1}])

    def test_fenced_array(self):
        text = '```json\n[{"id": 2}]\n```'
        self.assertEqual(rssdigest.extract_json_array(text), [{"id": 2}])

    def test_chatty_array(self):
        text = 'Sure, here you go:\n[{"id": 3}]\nhope that helps'
        self.assertEqual(rssdigest.extract_json_array(text), [{"id": 3}])

    def test_invalid_raises(self):
        with self.assertRaises((json.JSONDecodeError, ValueError)):
            rssdigest.extract_json_array("not json at all")


class FetchTests(unittest.TestCase):
    def test_fetch_items_maps_fields_and_caps(self):
        entries = [
            {"id": "id1", "title": "T1", "link": "L1", "author": "A1",
             "content": [{"value": "<p>c1</p>"}]},
            {"title": "T2", "link": "L2", "summary": "c2"},  # no id -> link
        ]
        parsed = FakeParsed(entries, feed={"title": "Src"})
        with mock.patch.object(rssdigest.feedparser, "parse", return_value=parsed):
            items = rssdigest.fetch_items(["http://feed"], max_items=0)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["id"], "id1")
        self.assertEqual(items[0]["creator"], "A1")
        self.assertEqual(items[0]["content"], "c1")
        self.assertEqual(items[1]["id"], "L2")  # falls back to link
        self.assertEqual(items[0]["source"], "Src")

    def test_fetch_items_respects_max_items(self):
        entries = [{"id": str(i), "title": "t", "link": "l"} for i in range(5)]
        parsed = FakeParsed(entries)
        with mock.patch.object(rssdigest.feedparser, "parse", return_value=parsed):
            items = rssdigest.fetch_items(["http://feed"], max_items=2)
        self.assertEqual(len(items), 2)


class StateTests(unittest.TestCase):
    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "seen.txt")
            rssdigest.save_seen(path, {"b", "a"})
            self.assertEqual(rssdigest.load_seen(path), {"a", "b"})
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")

    def test_load_seen_missing_is_empty(self):
        self.assertEqual(rssdigest.load_seen("/no/such/file"), set())
        self.assertEqual(rssdigest.load_seen(None), set())


class RenderTests(unittest.TestCase):
    def test_render_escapes_untrusted_fields(self):
        items = [{"title": "<script>x</script>", "link": 'http://e/"onmouseover',
                  "creator": "A & B", "summary": "<b>hi</b>"}]
        html = rssdigest.render_html(items)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertIn("A &amp; B", html)
        self.assertIn("&lt;b&gt;hi&lt;/b&gt;", html)

    def test_render_defaults_missing_creator(self):
        html = rssdigest.render_html([{"title": "T", "link": "L", "summary": "s"}])
        self.assertIn("By Unknown", html)


class SummariseLocalTests(unittest.TestCase):
    def _fake_proc(self, returncode=0, stdout="", stderr=""):
        return types.SimpleNamespace(returncode=returncode, stdout=stdout,
                                     stderr=stderr)

    def test_merges_summaries_by_id(self):
        items = [{"id": "a", "title": "T", "content": "body"}]
        envelope = {"result": json.dumps([{"id": "a", "summary": "two sentences."}])}
        proc = self._fake_proc(stdout=json.dumps(envelope))
        with mock.patch.object(rssdigest.shutil, "which", return_value="/x/claude"), \
             mock.patch.object(rssdigest.subprocess, "run", return_value=proc):
            ok = rssdigest.summarise_local(items)
        self.assertTrue(ok)
        self.assertEqual(items[0]["summary"], "two sentences.")

    def test_nonzero_exit_returns_false(self):
        items = [{"id": "a", "title": "T", "content": "body"}]
        proc = self._fake_proc(returncode=1, stderr="boom")
        with mock.patch.object(rssdigest.shutil, "which", return_value="/x/claude"), \
             mock.patch.object(rssdigest.subprocess, "run", return_value=proc), \
             redirect_stderr(io.StringIO()):
            ok = rssdigest.summarise_local(items)
        self.assertFalse(ok)
        self.assertNotIn("summary", items[0])

    def test_missing_cli_returns_false(self):
        with mock.patch.object(rssdigest.shutil, "which", return_value=None), \
             redirect_stderr(io.StringIO()):
            self.assertFalse(rssdigest.summarise_local([{"id": "a", "content": "x"}]))


class DeliverTests(unittest.TestCase):
    def test_out_writes_file_and_skips_state(self):
        items = [{"title": "Headline", "link": "L", "creator": "C",
                  "summary": "DISTINCT_SUMMARY", "id": "x"}]
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "preview.html")
            state = os.path.join(d, "seen.txt")
            with redirect_stdout(io.StringIO()):
                rc = rssdigest.deliver(items, mail_to=[], dry_run=False,
                                       state_path=state, out_path=out)
            self.assertEqual(rc, 0)
            with open(out, encoding="utf-8") as f:
                body = f.read()
            self.assertIn("DISTINCT_SUMMARY", body)
            self.assertIn("Headline", body)
            self.assertFalse(os.path.exists(state))  # preview must not record state

    def test_dry_run_prints_and_skips_state(self):
        items = [{"title": "T", "link": "L", "creator": "C", "summary": "s",
                  "id": "x"}]
        with tempfile.TemporaryDirectory() as d:
            state = os.path.join(d, "seen.txt")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = rssdigest.deliver(items, mail_to=[], dry_run=True,
                                       state_path=state, out_path=None)
            self.assertEqual(rc, 0)
            self.assertIn("<div", buf.getvalue())
            self.assertFalse(os.path.exists(state))


if __name__ == "__main__":
    unittest.main()
