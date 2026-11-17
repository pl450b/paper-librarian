"""Ask Claude about a paper, by driving the `claude` CLI in print mode.

Why a subprocess and not the Messages API: this app is stdlib-only, and the
CLI is already authenticated against a Claude subscription. Going through
`claude -p` means no API key to manage and no second bill -- `subprocess` is
stdlib, so the no-dependencies rule still holds.

The whole Claude-facing surface is `stream_turn()`. Everything else in the app
talks to that one generator, so swapping in a direct Messages API backend
later is a change to this file alone.

Two things here are load-bearing and easy to break:

* The child runs with cwd=papers/, never the project root. From the root it
  would auto-discover this repo's CLAUDE.md and believe its job is to work on
  the webapp. Do not "fix" this by passing --bare: that flag reads
  ANTHROPIC_API_KEY only and never touches the OAuth credentials, which
  defeats the point of using the subscription.
* The flags below are a token diet, not decoration. Without them the CLI
  loads every MCP server, skill and tool definition it can find: a one-word
  answer measured 31k tokens and $0.025. With them it is ~3.8k and $0.005.
  `--setting-sources ""` matters too: cwd=papers/ does NOT stop the CLI
  walking up to the project's CLAUDE.md files -- measured: a trivial
  call was 8,971 input tokens without the flag and 575 with it.

* If the Librarian is installed (librarian/.venv), the chat also gets its
  read-only MCP tools (library search, other papers' notes, citations) and
  the Project and Key claims sections of librarian/brief.md in its system prompt. Its note on
  the open paper goes into the first turn.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import uuid
from pathlib import Path

import db

CLI = shutil.which("claude")
LIB_DIR = Path(__file__).resolve().parent / "librarian"
LIB_PYTHON = next((p for p in (LIB_DIR / ".venv" / "bin" / "python",
                                LIB_DIR / ".venv" / "Scripts" / "python.exe") if p.exists()),
                  LIB_DIR / ".venv" / "bin" / "python")

# The CLI's own aliases, resolved by it to the current model in each family.
MODELS = ("haiku", "sonnet")
DEFAULT_MODEL = "haiku"

# Replaces Claude Code's default system prompt outright -- that prompt is
# written for a coding agent and is most of the token overhead.
SYSTEM_PROMPT = """You are a research assistant helping a researcher work through \
their reading library.

You are discussing one paper at a time. The PDF is in the working directory; \
read it with the Read tool before answering questions about what it says, and \
say so plainly when something is not in the paper rather than filling the gap \
from background knowledge.

Read the PDF once. It stays in this conversation afterwards, so answer \
follow-up questions from what you have already read instead of reading it \
again -- re-read only if you need a specific page you genuinely have not \
seen yet.

Answer in markdown. Use $...$ and $$...$$ for math. Be concise and concrete: \
prefer the paper's actual numbers, methods and claims over general \
summary. The reader is a domain expert, so skip basic background unless asked.
"""

# Generous: a first turn reads a whole PDF. The request thread is blocked for
# the duration, but ThreadingHTTPServer gives it its own thread.
TIMEOUT = 600


LIBRARIAN_ADDENDUM = """
You also have the Librarian's tools (mcp__librarian__*): search the whole \
library, open other papers' notes, and follow citations within it. Use them \
when the question reaches beyond this paper (comparisons, "what else says \
this", related work), and cite other papers by id as #123.

The project this library serves, in brief:

{brief}
"""


def librarian_ready():
    return LIB_PYTHON.exists()


def _project_brief():
    """The Project and Key claims sections of librarian/brief.md (the same
    sections the Librarian's own chat uses; see librarian/context.py)."""
    path = LIB_DIR / "brief.md"
    if not path.exists():
        return "(no brief written yet)"
    text = path.read_text(encoding="utf-8")
    if "<!-- TEMPLATE -->" in text:
        return "(the brief is still the unedited template)"
    parts = []
    for name in ("Project", "Key claims"):
        m = re.search(rf"^##\s+(?:\d+\.\s*)?{name}\b.*?$(.*?)(?=^##\s|\Z)",
                      text, re.M | re.S | re.I)
        if m and m.group(1).strip():
            parts.append(f"## {name}\n\n{m.group(1).strip()}")
    return "\n\n".join(parts)


def _mcp_config():
    path = LIB_DIR / "work" / "mcp-local.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {"librarian": {
        "command": str(LIB_PYTHON), "args": ["-m", "librarian.mcp"],
        "env": {"PYTHONPATH": str(LIB_DIR)}}}}), encoding="utf-8")
    return path


def _librarian_note(paper_id):
    for path in sorted((LIB_DIR / "notes").glob(f"{paper_id}-*.md")):
        return path.read_text(encoding="utf-8")
    return ""


def available():
    """Is the CLI installed? Drives the pane's empty state."""
    return bool(CLI)


def normalise_model(model):
    return model if model in MODELS else DEFAULT_MODEL


def _command(model, session_id, resume):
    cmd = [
        CLI, "-p",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",                      # required for stream-json under -p
        "--model", normalise_model(model),
        "--tools", "Read",                # no Bash, no Write, no web
        "--strict-mcp-config",            # no Gmail/Docs/etc. tool definitions
        "--disable-slash-commands",       # no skill listing in the prompt
        "--setting-sources", "",          # no CLAUDE.md / settings from up the tree
        "--permission-mode", "dontAsk",
    ]
    system = SYSTEM_PROMPT
    if librarian_ready():
        system += LIBRARIAN_ADDENDUM.replace("{brief}", _project_brief())
        cmd += ["--mcp-config", str(_mcp_config()),
                "--allowedTools", "Read", "mcp__librarian__*"]
    cmd += ["--system-prompt", system]
    cmd += ["--resume", session_id] if resume else ["--session-id", session_id]
    return cmd


def _opening_context(paper):
    """What Claude needs on the first turn to know which paper this is."""
    bits = [f"Paper: {paper['title'] or '(untitled)'}"]
    if paper.get("authors"):
        bits.append(f"Authors: {paper['authors']}")
    if paper.get("year") or paper.get("venue"):
        bits.append(f"Published: {paper.get('venue','')} {paper.get('year','')}".strip())
    if paper.get("pdf_path"):
        bits.append(f"PDF (read this first): ./{paper['pdf_path']}")
    else:
        bits.append("No PDF is attached, so you only have the citation below. "
                    "Answer from it and say clearly that you have not read the "
                    "full text.")
        bits.append(f"Citation: {paper.get('citation','')}")
    note = _librarian_note(paper["id"]) if librarian_ready() else ""
    if note:
        bits.append("\nThe Librarian's existing note on this paper (written by an "
                    "earlier model; verify against the PDF before relying on it):\n\n"
                    + note[:12000])
    return "\n".join(bits)


def stream_turn(paper, message, model=DEFAULT_MODEL, session_id=""):
    """Run one turn. Yields event dicts until the turn ends.

    Events: {"type": "session", "id"}      -- the resumable session id
            {"type": "tool", "name"}       -- Claude started using a tool
            {"type": "delta", "text"}      -- a chunk of the answer
            {"type": "done", "text", "cost"}
            {"type": "error", "message"}

    The caller is responsible for persisting the turn; nothing here writes to
    the database. Closing the generator early kills the child process, which
    is what makes a disconnecting browser not leak a `claude` process.
    """
    if not CLI:
        yield {"type": "error", "message":
               "The `claude` CLI is not on PATH, so the chat pane has no "
               "backend. Install Claude Code or switch this app to an API key."}
        return

    resume = bool(session_id)
    if not resume:
        session_id = str(uuid.uuid4())
        message = _opening_context(paper) + "\n\n---\n\n" + message

    proc = subprocess.Popen(
        _command(model, session_id, resume),
        cwd=str(db.PAPERS_DIR),           # never the project root -- see module docstring
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,           # lets us kill the whole group below
    )

    yield {"type": "session", "id": session_id}

    answer = []
    failed = None
    cost = None
    try:
        proc.stdin.write(message)
        proc.stdin.close()

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue                  # non-JSON noise on stdout; ignore

            kind = event.get("type")

            if kind == "stream_event":
                inner = event.get("event", {})
                if inner.get("type") == "content_block_delta":
                    delta = inner.get("delta", {})
                    # text_delta only: thinking_delta and input_json_delta are
                    # the model reasoning and tool arguments, not the answer.
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        answer.append(delta["text"])
                        yield {"type": "delta", "text": delta["text"]}

            elif kind == "assistant":
                # Surface tool use so a long PDF read shows progress instead
                # of looking like the pane has hung.
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        yield {"type": "tool", "name": block.get("name", "tool")}

            elif kind == "result":
                if event.get("is_error"):
                    failed = event.get("result") or "the CLI reported an error"
                elif not answer and event.get("result"):
                    # No deltas arrived but there is a final answer; use it.
                    answer.append(event["result"])
                    yield {"type": "delta", "text": event["result"]}
                cost = event.get("total_cost_usd")
                break

        proc.wait(timeout=TIMEOUT)
        if failed is None and proc.returncode not in (0, None):
            failed = (proc.stderr.read() or "").strip()[:400] or \
                     f"claude exited with status {proc.returncode}"
    except subprocess.TimeoutExpired:
        failed = f"Claude did not finish within {TIMEOUT}s."
    except BrokenPipeError:
        failed = "The claude process closed its input unexpectedly."
    finally:
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        for pipe in (proc.stdout, proc.stderr):
            try:
                pipe.close()
            except Exception:
                pass

    if failed:
        yield {"type": "error", "message": failed}
    else:
        yield {"type": "done", "text": "".join(answer), "cost": cost}
