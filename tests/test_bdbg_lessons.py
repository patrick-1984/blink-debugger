import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bdbg_lessons import lesson_text, record_lesson, suggest_lessons


class LessonTests(unittest.TestCase):
    def test_generalized_plain_prose_excludes_identifying_formats(self):
        for text in ("http://example.invalid", "/home/private/export", "name@example.invalid",
                     "address 192.0.2.7", "address aa:bb:cc:dd:ee:ff", "address 2001::1",
                     "secret: value", "line one\nline two", '{"event":"raw"}'):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    lesson_text(text)
        self.assertEqual(lesson_text("  Explain   why the result is indeterminate. "), "Explain why the result is indeterminate.")

    def test_round_trip_and_retry_deduplication(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "lessons"
            with patch.dict(os.environ, BLINK_DEBUGGER_STATE_DIR=str(directory)):
                self.assertEqual(suggest_lessons()["suggestions"], [])
                self.assertFalse(directory.exists())
                entry = record_lesson("feature", "Export summaries are hard to compare", "Add a stable summary format")
                self.assertTrue(entry["recorded"])
                duplicate = record_lesson("feature", "Export summaries are hard to compare", "Add a stable summary format")
                self.assertEqual(duplicate, {**entry, "recorded": False})
                self.assertEqual(suggest_lessons()["suggestions"], [{
                    "id": entry["id"], "kind": "feature", "observation": "Export summaries are hard to compare",
                    "suggestion": "Add a stable summary format",
                }])

    @unittest.skipUnless(os.name == "posix", "POSIX permission and symlink checks")
    def test_existing_unsafe_database_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = directory / "other-file"
            target.write_bytes(b"existing content")
            (directory / "software-lessons.sqlite3").symlink_to(target)
            with patch.dict(os.environ, BLINK_DEBUGGER_STATE_DIR=temporary):
                with self.assertRaisesRegex(ValueError, "regular database"):
                    record_lesson("fix", "The fixture needed a clearer message", "Explain the supported input")
            self.assertEqual(target.read_bytes(), b"existing content")
