"""Unit tests for ConversationStore project isolation."""

import os
import tempfile
import unittest

from history import ConversationStore


CONV = {
    "id": "abc123",
    "title": "t",
    "created": "2026-08-14T10:00:00",
    "updated": "2026-08-14T10:00:00",
    "messages": [{"role": "user", "content": "hi"}],
}


class ProjectIsolationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self._tmp.name, "conversations")
        self.store = ConversationStore(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_save_load_under_project(self):
        self.store.save(CONV, project="proj-a")
        data = self.store.load("abc123", project="proj-a")
        self.assertEqual(data["id"], "abc123")
        # Invisible from the top level and from other projects.
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.store.list("proj-b"), [])

    def test_top_level_and_project_coexist(self):
        self.store.save(CONV)  # no project
        self.store.save(dict(CONV, id="def456"), project="proj-a")
        self.assertEqual([c["id"] for c in self.store.list()], ["abc123"])
        self.assertEqual([c["id"] for c in self.store.list("proj-a")], ["def456"])

    def test_delete_scoped_to_project(self):
        self.store.save(CONV)
        self.store.save(dict(CONV, id="def456"), project="proj-a")
        self.store.delete("def456", project="proj-a")
        self.assertEqual(self.store.list("proj-a"), [])
        self.assertEqual([c["id"] for c in self.store.list()], ["abc123"])

    def test_delete_project_directory(self):
        self.store.save(dict(CONV, id="def456"), project="proj-a")
        self.store.delete_project("proj-a")
        self.assertFalse(self.store.list("proj-a"))
        with self.assertRaises(FileNotFoundError):
            self.store.load("def456", project="proj-a")

    def test_invalid_project_rejected(self):
        # None is the valid "top level" address; everything else must fail.
        self.assertEqual(self.store._subdir(None), self.store.root)
        for bad in ("../escape", "a/b", "a\\b", "x" * 65, "", 42):
            with self.assertRaises(ValueError):
                self.store._subdir(bad)
        with self.assertRaises(ValueError):
            self.store.save(CONV, project="../escape")
        with self.assertRaises(ValueError):
            self.store.load("abc123", project="../escape")
        with self.assertRaises(ValueError):
            self.store.delete_project("../escape")

    def test_escape_cannot_write_outside_root(self):
        with self.assertRaises(ValueError):
            self.store.save(CONV, project="..")
        self.assertFalse(os.path.exists(os.path.join(self._tmp.name, "abc123.json")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
