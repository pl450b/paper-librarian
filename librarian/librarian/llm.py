"""Every LLM call the Librarian makes goes through `claude -p`, on the user's
Claude subscription (no API key), exactly as the web app's chat.py does.

The flag set is a token diet. Measured: a trivial call costs ~575
input tokens with `--setting-sources ""` and ~8,970 without it, because the
CLI otherwise loads every CLAUDE.md up the tree plus user settings. The cwd
is librarian/work/, an empty directory, for the same reason.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import time
import uuid

from . import config

CLI = shutil.which("claude")
TIMEOUT = 900


class LLMError(RuntimeError):
    pass


class UsageLimit(LLMError):
    """The subscription's usage limit was hit; `resume_at` is a unix time."""
    def __init__(self, message, resume_at):
        super().__init__(message)
        self.resume_at = resume_at


_LIMIT_RE = re.compile(r"(usage limit|rate limit|limit reached|too many requests|"
                       r"overloaded|quota)", re.I)


def _base_flags(model, system_prompt):
    return [
        CLI, "-p",
        "--model", model,
        "--system-prompt", system_prompt,
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--setting-sources", "",
        "--permission-mode", "dontAsk",
    ]


def _resume_time(text):
    """Best-effort parse of 'resets at 3pm' / 'try again in 20 minutes'."""
    m = re.search(r"in (\d+)\s*(minute|min|hour|hr)", text or "", re.I)
    if m:
        n = int(m.group(1)) * (3600 if m.group(2).lower().startswith("h") else 60)
        return time.time() + n + 60
    return time.time() + 30 * 60


def _run(cmd, stdin_text, timeout=TIMEOUT):
    proc = subprocess.Popen(cmd, cwd=str(config.WORK_DIR), stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", start_new_session=True)
    try:
        out, err = proc.communicate(stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.communicate()
        raise LLMError(f"claude did not finish within {timeout}s")
    return proc.returncode, out, err


def structured(prompt, schema, system_prompt, model, timeout=TIMEOUT):
    """One stateless call that must return JSON matching `schema`.

    Returns (data, info) where info has model, usage and cost.
    """
    if not CLI:
        raise LLMError("the `claude` CLI is not on PATH")
    cmd = _base_flags(model, system_prompt) + [
        "--output-format", "json",
        "--tools", "",
        "--no-session-persistence",
        "--json-schema", json.dumps(schema),
    ]
    code, out, err = _run(cmd, prompt, timeout)
    try:
        d = json.loads(out)
    except ValueError:
        msg = (err or out).strip()[:500]
        if _LIMIT_RE.search(msg):
            raise UsageLimit(msg, _resume_time(msg))
        raise LLMError(msg or f"claude exited {code} with no output")
    if d.get("is_error"):
        msg = str(d.get("result") or "claude reported an error")
        if _LIMIT_RE.search(msg):
            raise UsageLimit(msg, _resume_time(msg))
        raise LLMError(msg[:500])
    data = d.get("structured_output")
    if data is None:
        try:
            data = json.loads(d.get("result") or "")
        except ValueError:
            raise LLMError("no structured output in claude's reply")
    usage = d.get("usage") or {}
    used_model = next(iter(d.get("modelUsage") or {}), model)
    info = {
        "model": used_model,
        "input_tokens": (usage.get("input_tokens", 0)
                         + usage.get("cache_creation_input_tokens", 0)
                         + usage.get("cache_read_input_tokens", 0)),
        "cache_read": usage.get("cache_read_input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cost_usd": d.get("total_cost_usd"),
    }
    return data, info


def new_session_id():
    return str(uuid.uuid4())
