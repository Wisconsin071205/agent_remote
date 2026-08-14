"""Throwaway unit tests for history.py (run with a temp VASPILOT_HISTORY_DIR)."""
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
TEST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".history-test")
os.environ["VASPILOT_HISTORY_DIR"] = TEST_DIR
shutil.rmtree(TEST_DIR, ignore_errors=True)

from history import ConversationStore

s = ConversationStore()
assert s.list() == [], "empty list"
conv = {
    "id": "conv-a", "title": "检查任务队列",
    "created": "2026-08-14T10:00:00", "updated": "2026-08-14T10:00:00",
    "messages": [
        {"role": "user", "content": "检查任务队列"},
        {"role": "assistant", "content": "有 3 个任务"},
        {"role": "tool", "tool_call_id": "x", "content": "{}"},
    ],
}
s.save(conv)
s.save({
    "id": "conv-b", "title": "另一个",
    "created": "2026-08-14T09:00:00", "updated": "2026-08-14T09:00:00",
    "messages": [],
})
lst = s.list()
assert [c["id"] for c in lst] == ["conv-a", "conv-b"], f"order: {lst}"
loaded = s.load("conv-a")
assert loaded["messages"][1]["content"] == "有 3 个任务"
# Corrupt file is skipped by list.
path = os.path.join(TEST_DIR, "conv-b.json")
with open(path, "w", encoding="utf-8") as fh:
    fh.write("{broken")
assert [c["id"] for c in s.list()] == ["conv-a"]
# Invalid ids rejected (path traversal guard).
for bad in ("../evil", "a" * 65, "x/y", ""):
    try:
        s.load(bad)
        assert False, bad
    except ValueError:
        pass
    try:
        s.save({"id": bad, "messages": []})
        assert False, bad
    except ValueError:
        pass
s.delete("conv-a")
assert s.list() == []
try:
    s.delete("conv-a")
    assert False
except FileNotFoundError:
    pass
shutil.rmtree(TEST_DIR, ignore_errors=True)
print("history.py unit tests: ALL PASS")
