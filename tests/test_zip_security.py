import os
import tempfile
import unittest
import zipfile
from pathlib import Path

os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "123")

import bot


class ZipSecurityTests(unittest.TestCase):
    def make_zip(self, root: Path, entries):
        archive = root / "fixture.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            for name, content in entries:
                zf.writestr(name, content)
        return archive

    def test_extracts_normal_zip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = self.make_zip(root, [("app/src/Main.java", "class Main {}")])
            destination = root / "out"
            destination.mkdir()
            bot.safe_extract(archive, destination)
            self.assertEqual((destination / "app/src/Main.java").read_text(), "class Main {}")

    def test_rejects_parent_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = self.make_zip(root, [("../outside.txt", "no")])
            destination = root / "out"
            destination.mkdir()
            with self.assertRaisesRegex(ValueError, "unsafe file path"):
                bot.safe_extract(archive, destination)
            self.assertFalse((root / "outside.txt").exists())

    def test_rejects_windows_absolute_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = self.make_zip(root, [("C:/Windows/evil.txt", "no")])
            destination = root / "out"
            destination.mkdir()
            with self.assertRaisesRegex(ValueError, "unsafe file path"):
                bot.safe_extract(archive, destination)

    def test_rejects_duplicate_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = self.make_zip(root, [("app.txt", "one"), ("app.txt", "two")])
            destination = root / "out"
            destination.mkdir()
            with self.assertRaisesRegex(ValueError, "duplicate/conflicting"):
                bot.safe_extract(archive, destination)

    def test_rejects_non_zip_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            archive = root / "bad.zip"
            archive.write_bytes(b"not a zip")
            destination = root / "out"
            destination.mkdir()
            with self.assertRaises(zipfile.BadZipFile):
                bot.safe_extract(archive, destination)


if __name__ == "__main__":
    unittest.main()
