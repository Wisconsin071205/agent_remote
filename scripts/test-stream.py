"""Throwaway unit tests for DeepSeekClient.complete_stream with a mocked response."""
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib.util

spec = importlib.util.spec_from_file_location(
    "vasp_deepseek_agent", os.path.join(os.path.dirname(os.path.abspath(__file__)), "deepseek-agent.py")
)
sys.modules[spec.name] = spec  # must be registered before exec_module
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FakeResponse(io.BytesIO):
    """BytesIO that yields SSE lines like an HTTPResponse."""

    def __init__(self, payload: bytes):
        super().__init__(payload)

    def __iter__(self):
        return self


def frame(obj):
    return ("data: " + json.dumps(obj) + "\n\n").encode("utf-8")


def build_stream(messages):
    """Synthesize an SSE body: reasoning, content, then one tool call."""
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "用户想"}}]},
        {"choices": [{"delta": {"reasoning_content": "检查队列"}}]},
        {"choices": [{"delta": {"content": "好的，"}}]},
        {"choices": [{"delta": {"content": "我来看看。"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {"name": "list_jobs", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"un"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "known\": 1}"}}]}}]},
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    body = b"".join(frame(c) for c in chunks) + b"data: [DONE]\n\n"
    return FakeResponse(body)


client = module.DeepSeekClient("sk-test", "https://api.deepseek.com", "deepseek-chat")
client._opener = None  # not used; complete_stream is called directly on the fake below

# Patch the network boundary: complete_stream calls self._opener.open -> return fake.
client._opener = type("O", (), {"open": lambda self, req, timeout: build_stream(None)})()

deltas = []
message = client.complete_stream([{"role": "user", "content": "hi"}], lambda kind, text: deltas.append((kind, text)))

assert "".join(t for k, t in deltas if k == "reasoning") == "用户想检查队列", deltas
assert "".join(t for k, t in deltas if k == "content") == "好的，我来看看。", deltas
assert message["reasoning_content"] == "用户想检查队列"
assert message["content"] == "好的，我来看看。"
calls = message["tool_calls"]
assert len(calls) == 1 and calls[0]["id"] == "call_1" and calls[0]["function"]["name"] == "list_jobs"
assert json.loads(calls[0]["function"]["arguments"]) == {"unknown": 1}, calls[0]["function"]["arguments"]
assert message["role"] == "assistant"

# Error JSON mid-stream (HTTP 200 body) must raise.
client._opener = type("O", (), {"open": lambda self, req, timeout: FakeResponse(frame({"error": {"message": "quota"}}))})()
try:
    client.complete_stream([], lambda k, t: None)
    assert False, "expected RuntimeError"
except RuntimeError as exc:
    assert "quota" in str(exc), exc

# tool_calls-only reply: no content key.
client._opener = type("O", (), {"open": lambda self, req, timeout: FakeResponse(b"data: [DONE]\n\n")})()
message = client.complete_stream([], lambda k, t: None)
assert message == {"role": "assistant"}, message

print("complete_stream unit tests: ALL PASS")
