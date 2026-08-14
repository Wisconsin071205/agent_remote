"""Export a Claude Code session jsonl into a readable chat-record HTML page.

Extracts user text messages and assistant text replies (tool calls and tool
results are skipped). Scans for known-sensitive substrings and refuses to
emit them into the page.
"""
import html
import json
import os
import sys

LOG = r"C:\Users\weikx\.claude\projects\D--VASP-project-vasp-remote-agent\1eae4dcb-8689-493b-8fd5-bde43cebb8a0.jsonl"
OUT = r"D:\VASP project\vasp-remote-agent\conversation-export.html"

SENSITIVE = ["ksolv", "YVOIV", "totp", "TOTP"]

import re

REDACTED = 0


def redact(text):
    """Replace any known credential fragments with a marker."""
    global REDACTED
    # bare tokens first covers both full credentials and isolated mentions
    patterns = [
        r"ksolv",  # cl10 account password (incl. "ksolv#chem")
        r"YVOIV",  # cl10 TOTP base32 seed
    ]
    for pat in patterns:
        text, n = re.subn(pat, "[凭据已打码]", text, flags=re.IGNORECASE)
        REDACTED += n
    return text


def text_of(content):
    """Extract plain text from a message content (str or list of blocks)."""
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif block.get("type") == "tool_result":
                # never include raw tool output on the page
                continue
    return "\n".join(parts).strip()


turns = []  # (role, text)
with open(LOG, "r", encoding="utf-8") as fh:
    for line in fh:
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        mtype = entry.get("type")
        if mtype == "user":
            msg = entry.get("message") or {}
            if msg.get("role") != "user":
                continue
            text = text_of(msg.get("content"))
            if text:
                turns.append(("user", text))
        elif mtype == "assistant":
            msg = entry.get("message") or {}
            text = text_of(msg.get("content"))
            if text:
                turns.append(("assistant", text))

# safety scan: never let known credentials onto the page
joined = "\n".join(t for _, t in turns).lower()
for marker in ["ksolv", "yvoiv"]:
    if marker in joined:
        print(f"WARNING: '{marker}' found {joined.count(marker)}x in conversation text; redacting.")

turns = [(role, redact(text)) for role, text in turns]
if REDACTED:
    print(f"redacted {REDACTED} credential occurrences before export")

BUBBLE = """<div class="turn {cls}">
  <div class="who">{who}</div>
  <div class="bubble">{body}</div>
</div>"""

body = []
for role, text in turns:
    cls = "user" if role == "user" else "ai"
    who = "胡伟" if role == "user" else "VASPilot"
    text = html.escape(text)
    text = text.replace("\n", "<br>")
    body.append(BUBBLE.format(cls=cls, who=who, body=text))

page = """<!doctype html>
<html lang="zh-CN">
<title>VASPilot 会话记录</title>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root {
  --bg: #0f1217; --panel: #151a21; --line: #262d38;
  --ink: #e9e7e2; --ink-dim: #9aa1ad; --ink-faint: #69717d;
  --accent: #d9a441; --accent-soft: rgba(217,164,65,.13);
  --bubble-ai: #1a2029; --bubble-user: #20262f;
  --font-display: "Bahnschrift", "Segoe UI", sans-serif;
  --font-body: "Segoe UI", system-ui, sans-serif;
}
@media (prefers-color-scheme: light) {
  :root:not([data-theme="dark"]) {
    --bg: #f7f5f0; --panel: #fffdf9; --line: #e0dbd0;
    --ink: #20232a; --ink-dim: #565d68; --ink-faint: #8b919c;
    --accent: #8a6a1f; --accent-soft: rgba(138,106,31,.10);
    --bubble-ai: #ffffff; --bubble-user: #ede9df;
  }
}
:root[data-theme="light"] {
  --bg: #f7f5f0; --panel: #fffdf9; --line: #e0dbd0;
  --ink: #20232a; --ink-dim: #565d68; --ink-faint: #8b919c;
  --accent: #8a6a1f; --accent-soft: rgba(138,106,31,.10);
  --bubble-ai: #ffffff; --bubble-user: #ede9df;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  background: var(--bg); color: var(--ink);
  font-family: var(--font-body); font-size: 15px; line-height: 1.7;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 760px; margin: 0 auto; padding: 40px 24px 80px; }
.seal { height: 3px; background: linear-gradient(90deg, var(--accent), var(--accent) 38%, transparent 38%); margin-bottom: 30px; }
h1 { font-family: var(--font-display); font-weight: 500; font-size: 30px; letter-spacing: .01em; margin-bottom: 8px; }
.meta { font-size: 13px; color: var(--ink-faint); margin-bottom: 34px; font-family: var(--font-mono, Consolas, monospace); }
.turn { display: flex; flex-direction: column; margin-bottom: 22px; }
.who {
  font-family: var(--font-display); font-size: 11.5px; letter-spacing: .16em;
  text-transform: uppercase; color: var(--ink-faint); margin-bottom: 6px;
}
.bubble {
  background: var(--bubble-ai); border: 1px solid var(--line);
  border-radius: 12px; padding: 14px 18px; max-width: 92%;
  word-break: break-word;
}
.turn.user .bubble {
  align-self: flex-end;
  background: var(--bubble-user);
  border-color: color-mix(in srgb, var(--accent) 30%, var(--line));
}
.turn.user .who { align-self: flex-end; }
.foot { margin-top: 40px; padding-top: 18px; border-top: 1px dashed var(--line); font-size: 12.5px; color: var(--ink-faint); }
</style>
<body>
<div class="wrap">
  <div class="seal"></div>
  <h1>VASPilot 会话记录</h1>
  <div class="meta">__TURNS__ 轮对话 · 2026-08-13 → 2026-08-14</div>
  __BODY__
  <div class="foot">本页面由 VASPilot 开发会话导出生成，仅展示对话文本。</div>
</div>
</body>
</html>"""

page = page.replace("__TURNS__", str(len(turns))).replace("__BODY__", "\n".join(body))

# final assertion: the page must be free of any credential fragment
lower = page.lower()
for marker in ["ksolv", "yvoiv"]:
    if marker in lower:
        print(f"ABORT: '{marker}' still present in final page; not writing.")
        sys.exit(1)

with open(OUT, "w", encoding="utf-8") as fh:
    fh.write(page)
print(f"exported {len(turns)} turns -> {OUT}")
