"""Library-wide chat with the Librarian (the web app's "Ask the Librarian" view).

Each turn is one `claude -p` run with the Librarian's MCP tools and nothing
else; the session is resumed per channel so follow-ups keep context.
"""

import json
import os
import signal
import subprocess
import sys

from . import config, context, llm, store

SYSTEM_PROMPT = """You are the Librarian: the research assistant who has catalogued \
the author's reading library. Answer questions about the library using your \
tools; do not answer from memory about what a paper says.

- Search before you answer, and open notes (get_note) for the papers you rely on.
- Always cite papers by id, as #123, so the app can link them.
- Say which papers you actually read in full (tier "deep") and which you only know \
from an abstract.
- Ghost papers (gN) are cited by the library but not in it; say so when you mention one.
- You are also a running background process. For any question about your own progress \
(what has been cataloged, how many, what's queued, what you are doing now, whether you \
are paused, the backfill), call status, recent or queue and report what they return. \
Never guess or say you don't know.
- If the author has a LaTeX draft configured, use draft_outline, draft_section, \
search_draft and draft_cites when asked what the draft says, whether a paper is \
already cited, or where a paper should go. Quote the draft, not the brief, for what \
it currently says.
- Be concise and concrete. The reader is the author and knows the field.
- If the library has nothing on a question, say so plainly and suggest what to look for.

================ RESEARCH BRIEF (brief.md) ================
{context}
"""

TIMEOUT = 600


def mcp_config():
    path = config.WORK_DIR / "mcp-local.json"
    cfg = {"mcpServers": {"librarian": {
        "command": sys.executable, "args": ["-m", "librarian.mcp"],
        "env": {"PYTHONPATH": str(config.LIB_DIR)}}}}
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def _first_turn_context():
    if config.DIGEST_PATH.exists():
        return ("Current library digest (LIBRARY.md):\n\n"
                + config.DIGEST_PATH.read_text(encoding="utf-8")[:24000]
                + "\n\n---\n\n")
    return ""


def stream_turn(channel, message, model=config.DEEP_MODEL):
    """Yield events like the web app's chat.py:
    {"type": "tool", "name"} | {"type": "delta", "text"} | {"type": "done", "text"}
    | {"type": "error", "message"}. Persists the turn on completion."""
    if not llm.CLI:
        yield {"type": "error", "message": "the `claude` CLI is not on PATH"}
        return
    c = store.conn()
    session = store.get_session(c, channel)
    resume = bool(session)
    if not resume:
        session = llm.new_session_id()
    system = SYSTEM_PROMPT.replace("{context}", context.text())
    prompt = message if resume else _first_turn_context() + message
    cmd = [llm.CLI, "-p", "--output-format", "stream-json", "--include-partial-messages",
           "--verbose", "--model", model, "--system-prompt", system,
           "--tools", "", "--mcp-config", str(mcp_config()), "--strict-mcp-config",
           "--allowedTools", "mcp__librarian__*", "--disable-slash-commands",
           "--setting-sources", "", "--permission-mode", "dontAsk"]
    cmd += ["--resume", session] if resume else ["--session-id", session]

    store.add_message(c, channel, "user", message)
    proc = subprocess.Popen(cmd, cwd=str(config.WORK_DIR), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", start_new_session=True,
                            env={**os.environ, "PYTHONPATH": str(config.LIB_DIR)})
    answer, failed = [], None
    try:
        proc.stdin.write(prompt)
        proc.stdin.close()
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("type")
            if kind == "stream_event":
                d = (ev.get("event") or {}).get("delta") or {}
                if d.get("type") == "text_delta" and d.get("text"):
                    answer.append(d["text"])
                    yield {"type": "delta", "text": d["text"]}
            elif kind == "assistant":
                for block in (ev.get("message") or {}).get("content", []):
                    if block.get("type") == "tool_use":
                        yield {"type": "tool", "name": block.get("name", "").split("__")[-1]}
            elif kind == "result":
                if ev.get("is_error"):
                    failed = str(ev.get("result") or "the CLI reported an error")[:500]
                elif not answer and ev.get("result"):
                    answer.append(ev["result"])
                    yield {"type": "delta", "text": ev["result"]}
                break
        proc.wait(timeout=TIMEOUT)
        if failed is None and proc.returncode not in (0, None):
            failed = (proc.stderr.read() or "").strip()[:400] or f"claude exited {proc.returncode}"
    except subprocess.TimeoutExpired:
        failed = f"no answer within {TIMEOUT}s"
    finally:
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    if failed:
        if "session" in failed.lower() and resume:
            store.set_session(c, channel, "")   # stale session: next turn starts fresh
        yield {"type": "error", "message": failed}
        return
    store.set_session(c, channel, session)
    text = "".join(answer)
    store.add_message(c, channel, "assistant", text)
    yield {"type": "done", "text": text}


def ask(channel, message, model=config.DEEP_MODEL):
    """Non-streaming convenience: returns (final text, tools used); raises on error."""
    tools_used = []
    for ev in stream_turn(channel, message, model):
        if ev["type"] == "tool":
            tools_used.append(ev["name"])
        elif ev["type"] == "error":
            raise llm.LLMError(ev["message"])
        elif ev["type"] == "done":
            return ev["text"], tools_used
    return "", tools_used


if __name__ == "__main__":
    text, used = ask("web", " ".join(sys.argv[1:]))
    print("tools:", used)
    print(text)
