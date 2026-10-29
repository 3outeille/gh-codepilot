"""Read the coding agent's native session history for display in the web app.

Claude Code keeps one directory per project, named after the CWD. prime-agent
keeps every session in one flat directory and records the CWD in each file's
header line, so its sessions are found by reading those headers.
"""

import json
from pathlib import Path

import agent_backend

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
PRIME_SESSIONS_DIR = agent_backend.PRIME_AGENT_DIR / "sessions"


def encode_cwd(path):
    """Encode a CWD path to Claude's project directory name format.

    Claude replaces /, _, and . with - in project directory names.
    """
    return str(path).replace("/", "-").replace("_", "-").replace(".", "-")


def get_sessions(cwd):
    """
    List past agent sessions for a given working directory.

    Returns list of dicts sorted by most recent first:
        {session_id, slug, first_prompt, started_at, last_activity, git_branch}
    """
    if agent_backend.IS_PRIME:
        return _prime_sessions(cwd)
    encoded = encode_cwd(cwd)
    project_dir = CLAUDE_PROJECTS_DIR / encoded

    if not project_dir.exists():
        return []

    sessions = []
    for jsonl_file in project_dir.glob("*.jsonl"):
        session_id = jsonl_file.stem
        info = _parse_session_file(jsonl_file)
        if info:
            info["session_id"] = session_id
            sessions.append(info)

    sessions.sort(key=lambda s: s.get("started_at", ""), reverse=True)
    return sessions


def _parse_session_file(path):
    """Extract metadata from a session JSONL file efficiently."""
    first_prompt = ""
    started_at = ""
    git_branch = ""
    slug = ""
    last_activity = ""

    try:
        with open(path, "r") as f:
            # Read first ~30 lines to find the first user message and slug
            for i, line in enumerate(f):
                if i > 30:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Grab slug from any message that has it
                if not slug and msg.get("slug"):
                    slug = msg["slug"]

                if msg.get("type") == "user" and not first_prompt:
                    started_at = msg.get("timestamp", "")
                    git_branch = msg.get("gitBranch", "")
                    content = msg.get("message", {}).get("content", "")
                    if isinstance(content, str):
                        first_prompt = content[:150]
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                first_prompt = block.get("text", "")[:150]
                                break

                if first_prompt and slug:
                    break

            # Read last line for last_activity timestamp
            f.seek(0, 2)  # seek to end
            file_size = f.tell()
            if file_size > 0:
                # Read last ~2KB to find the last complete line
                read_size = min(2048, file_size)
                f.seek(file_size - read_size)
                chunk = f.read()
                lines = chunk.strip().split("\n")
                for last_line in reversed(lines):
                    last_line = last_line.strip()
                    if not last_line:
                        continue
                    try:
                        last_msg = json.loads(last_line)
                        last_activity = last_msg.get("timestamp", "")
                        if last_activity:
                            break
                    except json.JSONDecodeError:
                        continue

    except (OSError, IOError):
        return None

    if not first_prompt:
        return None

    return {
        "slug": slug,
        "first_prompt": first_prompt,
        "started_at": started_at,
        "last_activity": last_activity or started_at,
        "git_branch": git_branch,
    }


# ── prime-agent ──────────────────────────────────────────────────────────────

def _prime_sessions(cwd):
    if not PRIME_SESSIONS_DIR.exists():
        return []
    sessions = []
    for jsonl_file in PRIME_SESSIONS_DIR.glob("*.jsonl"):
        info = _parse_prime_session(jsonl_file, str(cwd))
        if info:
            info["session_id"] = jsonl_file.stem
            sessions.append(info)
    sessions.sort(key=lambda s: s.get("started_at", ""), reverse=True)
    return sessions


def _parse_prime_session(path, cwd):
    """Metadata for one prime-agent transcript, or None if it is not a root
    session for this checkout."""
    first_prompt = ""
    started_at = ""
    last_activity = ""

    try:
        with open(path, "r") as f:
            header = f.readline().strip()
            try:
                head = json.loads(header)
            except json.JSONDecodeError:
                return None
            # Subagents (rlmDepth > 0) are the parent session's business, not tabs.
            if head.get("type") != "session" or head.get("cwd") != cwd or head.get("rlmDepth"):
                return None
            started_at = head.get("timestamp", "")

            for i, line in enumerate(f):
                if i > 60 or first_prompt:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") != "message":
                    continue
                msg = ev.get("message") or {}
                if msg.get("role") != "user":
                    continue
                content = msg.get("content", "")
                text = content if isinstance(content, str) else next(
                    (b.get("text", "") for b in content
                     if isinstance(b, dict) and b.get("type") == "text"), "")
                # Harness messages ("[bash-done] ...") are not what the user typed.
                if text.startswith("["):
                    continue
                first_prompt = text[:150]

            f.seek(0, 2)
            file_size = f.tell()
            if file_size > 0:
                f.seek(max(0, file_size - 2048))
                for last_line in reversed(f.read().strip().split("\n")):
                    try:
                        last_activity = json.loads(last_line.strip()).get("timestamp", "")
                    except json.JSONDecodeError:
                        continue
                    if last_activity:
                        break
    except (OSError, IOError):
        return None

    if not first_prompt:
        return None

    return {
        "slug": "",
        "first_prompt": first_prompt,
        "started_at": started_at,
        "last_activity": last_activity or started_at,
        "git_branch": "",
    }
