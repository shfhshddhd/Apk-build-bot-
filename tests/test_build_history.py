import tempfile
import unittest
from pathlib import Path

from build_history import record_build, recent_builds


class BuildHistoryTests(unittest.TestCase):
    def test_history_is_persistent_and_scoped_to_user(self):
        with tempfile.TemporaryDirectory() as td:
            db = str(Path(td) / "history.sqlite3")
            record_build(101, "demo", "gradle", "success", "sha256:abc", db)
            record_build(202, "private", "flutter", "failed", "error", db)
            rows = recent_builds(101, db_path=db)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["project"], "demo")
            self.assertEqual(rows[0]["status"], "success")
            self.assertEqual(recent_builds(999, db_path=db), [])


if __name__ == "__main__":
    unittest.main()
