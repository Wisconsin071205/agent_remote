"""Unit tests for local_store.LocalStore (model list + project registry)."""

import json
import os
import tempfile
import unittest

from local_store import LocalStore, DEFAULT_MODELS


class LocalStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "local.json")
        self.store = LocalStore(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_models_default(self):
        self.assertEqual(self.store.get_models(), DEFAULT_MODELS)

    def test_models_roundtrip(self):
        models = ["deepseek-chat", "deepseek-reasoner", "kimi", "qwen-max"]
        self.store.set_models(models)
        self.assertEqual(self.store.get_models(), models)
        # Persisted for a fresh store.
        self.assertEqual(LocalStore(self.path).get_models(), models)

    def test_models_rejects_empty(self):
        with self.assertRaises(ValueError):
            self.store.set_models([])
        with self.assertRaises(ValueError):
            self.store.set_models(["deepseek-chat", ""])

    def test_models_rejects_duplicates(self):
        with self.assertRaises(ValueError):
            self.store.set_models(["a", "a"])

    def test_models_rejects_whitespace_and_junk(self):
        with self.assertRaises(ValueError):
            self.store.set_models(["bad name"])
        with self.assertRaises(ValueError):
            self.store.set_models(["x" * 65])
        with self.assertRaises(ValueError):
            self.store.set_models(["a", 42])

    def test_projects_roundtrip(self):
        project = self.store.add_project("LiFePO4 计算", r"D:\VASP project\lfp")
        self.assertRegex(project["id"], r"^[A-Za-z0-9_-]{1,64}$")
        listed = self.store.list_projects()
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["name"], "LiFePO4 计算")
        self.assertEqual(listed[0]["path"], r"D:\VASP project\lfp")
        self.assertEqual(LocalStore(self.path).list_projects()[0]["id"], project["id"])

    def test_projects_rejects_duplicate_name_or_path(self):
        self.store.add_project("a", r"D:\x")
        with self.assertRaises(ValueError):
            self.store.add_project("a", r"D:\y")
        with self.assertRaises(ValueError):
            self.store.add_project("b", r"D:\x")

    def test_projects_rejects_bad_input(self):
        with self.assertRaises(ValueError):
            self.store.add_project("", r"D:\x")
        with self.assertRaises(ValueError):
            self.store.add_project("a", "")
        with self.assertRaises(ValueError):
            self.store.add_project("a" * 41, r"D:\x")
        with self.assertRaises(ValueError):
            self.store.add_project("a", "x" * 261)

    def test_projects_remove(self):
        project = self.store.add_project("a", r"D:\x")
        self.store.add_project("b", r"D:\y")
        self.store.remove_project(project["id"])
        ids = [p["id"] for p in self.store.list_projects()]
        self.assertEqual(ids, [p["id"] for p in [self.store.list_projects()[0]]])
        self.assertEqual(len(ids), 1)
        with self.assertRaises(ValueError):
            self.store.remove_project(project["id"])
        with self.assertRaises(ValueError):
            self.store.remove_project("does-not-exist")

    def test_corrupted_file_recovers(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        self.assertEqual(self.store.get_models(), DEFAULT_MODELS)
        self.assertEqual(self.store.list_projects(), [])
        # A write after corruption replaces the file wholesale.
        self.store.set_models(["deepseek-chat"])
        self.assertEqual(LocalStore(self.path).get_models(), ["deepseek-chat"])

    def test_file_contains_no_secrets_keys(self):
        self.store.add_project("a", r"D:\x")
        self.store.set_models(["deepseek-chat"])
        with open(self.path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        for key in ("api_key", "password", "token", "seed", "salt", "hash"):
            self.assertNotIn(key, json.dumps(data))


if __name__ == "__main__":
    unittest.main(verbosity=2)
