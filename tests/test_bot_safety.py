import os
import socket
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

# bot.py reads these settings at import time; test-only values avoid requiring
# real Telegram credentials in the CI environment.
os.environ.setdefault("BOT_TOKEN", "123456:test-token")
os.environ.setdefault("ALLOWED_USER_IDS", "123")

import bot


class UploadSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.archive = self.root / "project.zip"
        self.destination = self.root / "source"
        self.destination.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def write_zip(self, entries):
        with zipfile.ZipFile(self.archive, "w") as archive:
            for name, value in entries:
                archive.writestr(name, value)

    def test_safe_extract_accepts_normal_project(self):
        self.write_zip([("settings.gradle", "rootProject.name='demo'"), ("app/build.gradle", "plugins {}")])
        bot.safe_extract(self.archive, self.destination)
        self.assertEqual((self.destination / "settings.gradle").read_text(), "rootProject.name='demo'")
        self.assertTrue((self.destination / "app" / "build.gradle").is_file())

    def test_safe_extract_rejects_parent_traversal(self):
        self.write_zip([("../outside.txt", "not allowed")])
        with self.assertRaisesRegex(ValueError, "unsafe file path"):
            bot.safe_extract(self.archive, self.destination)
        self.assertFalse((self.root / "outside.txt").exists())

    def test_safe_extract_rejects_symlinks(self):
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(self.archive, "w") as archive:
            archive.writestr(info, "target")
        with self.assertRaisesRegex(ValueError, "symlink"):
            bot.safe_extract(self.archive, self.destination)

    def test_safe_extract_enforces_entry_count(self):
        self.write_zip([("a.txt", "a"), ("b.txt", "b")])
        with patch.object(bot, "MAX_ZIP_FILES", 1):
            with self.assertRaisesRegex(ValueError, "bahut zyada files"):
                bot.safe_extract(self.archive, self.destination)

    def test_download_url_rejects_non_https_and_private_hosts(self):
        for url in (
            "http://example.com/project.zip",
            "https://localhost/project.zip",
            "https://127.0.0.1/project.zip",
            "https://user:pass@example.com/project.zip",
            "https://example.com:8443/project.zip",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    bot.validate_public_https_url(url)

    def test_download_url_accepts_public_dns_result(self):
        public_record = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        with patch.object(socket, "getaddrinfo", return_value=[public_record]):
            bot.validate_public_https_url("https://example.com/project.zip")


if __name__ == "__main__":
    unittest.main()
