"""Which coding agent the dashboard drives.

prime-agent is the default. Set CODEPILOT_AGENT=claude to go back to Claude Code.
The two CLIs agree on `--resume <id>` and `--append-system-prompt`, so the
interactive path only differs in the binary; headless review differs more:
prime-agent has no schema or tool-allowlist flags, so the schema is asked for
in the prompt and pulled out of its JSONL event stream.
"""

import json
import os
import shutil
from pathlib import Path

NAME = (os.environ.get("CODEPILOT_AGENT") or "prime").strip().lower()
IS_PRIME = NAME not in ("claude", "claude-code")
LABEL = "Prime Agent" if IS_PRIME else "Claude Code"
BIN = "prime-agent" if IS_PRIME else "claude"

PRIME_AGENT_DIR = Path(os.environ.get("PRIME_AGENT_CODING_AGENT_DIR") or (Path.home() / ".prime" / "agent"))


def binary():
    return shutil.which(BIN) or BIN


def interactive_cmd(resume_session_id=None, initial_prompt=None):
    """argv for the PTY session."""
    cmd = [binary()]
    if resume_session_id:
        cmd.extend(["--resume", resume_session_id])
    elif initial_prompt:
        cmd.extend(["--append-system-prompt", initial_prompt])
    return cmd


def interactive_env(env):
    """Per-agent terminal tweaks, applied in place."""
    if not IS_PRIME:
        # Claude Code's fullscreen renderer takes the alternate screen and
        # captures the mouse, which kills xterm.js scrollback.
        env.setdefault("CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN", "1")
        env.setdefault("CLAUDE_CODE_DISABLE_MOUSE", "1")
    return env


def review_cmd(prompt):
    """argv for the headless pre-review."""
    if IS_PRIME:
        return [binary(), "--mode", "json", "-p", "--", prompt]
    return [binary(), "-p", prompt, "--output-format", "json",
            "--json-schema", json.dumps(REVIEW_SCHEMA),
            "--allowedTools", "Read", "Grep", "Glob", "Bash(relore:*)",
            "--permission-mode", "dontAsk"]


def review_output_contract(schema):
    """Trailer for the review prompt. Claude Code gets the schema through
    --json-schema; prime-agent has no such flag, so it is asked for here."""
    if not IS_PRIME:
        return ""
    return (
        "\n\nOutput contract: your final message must be exactly one JSON object matching this "
        "schema — no prose before or after it, no code fence around it:\n"
        f"{json.dumps(schema)}\n"
        "Read and search files to check callers and context, but make no edits: this checkout is "
        "the human reviewer's, and the review is advisory only.")


def final_text(stdout):
    """The agent's last assistant message, out of whatever envelope the CLI printed.

    prime-agent --mode json streams one JSON event per line (message_start /
    message_update / message_end / turn_end / agent_end), each carrying the
    message built so far. Claude Code prints a single envelope that
    _parse_review_json already understands, so it passes through untouched.
    """
    if not IS_PRIME:
        return stdout
    text = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = ev.get("message")
        if ev.get("type") == "agent_end":
            msgs = ev.get("messages") or []
            msg = msgs[-1] if msgs else None
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        blocks = [b.get("text", "") for b in msg.get("content") or []
                  if isinstance(b, dict) and b.get("type") == "text"]
        joined = "\n".join(b for b in blocks if b.strip())
        if joined:
            text = joined
    return text or stdout


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "findings": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "path": {"type": ["string", "null"]}, "line": {"type": ["integer", "null"]},
                "side": {"type": "string", "enum": ["new", "old"]},
                "severity": {"type": "string", "enum": ["high", "medium", "low", "nit"]},
                "title": {"type": "string"}, "body": {"type": "string"},
            },
            "required": ["path", "line", "side", "severity", "title", "body"],
        }},
    },
    "required": ["summary", "findings"],
}
