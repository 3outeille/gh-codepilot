"""A read/unread layer over events.jsonl.

Nothing in the app tracked whether you had *seen* an event. `seen.json` marks
whole items as archived on the board, and `.notif_state.json` only remembers
which GitHub event ids already fired a desktop notification — keyed by ids
that never reach events.jsonl. So the inbox keeps its own state.

Three things shape the design:

  Events carry no id. One is derived from the line's own content, which is
  stable across restarts and works for history written before this existed.

  Read state is a plain set of ids, with no "everything before T" shortcut.
  A time sweep is tempting and smaller, but then marking one item unread
  afterwards contradicts it and needs a second exception list to undo. The
  events file is pruned to about a week — a few hundred ids — so storing them
  outright costs nothing and cannot disagree with itself.

  Events are overwhelmingly comments on a few threads — 135 of 149 at the time
  of writing. A flat list of those is unreadable, so the inbox groups by item
  and counts unread per thread, the way a mail client groups a conversation.
"""
import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "inbox_state.json"
_lock = threading.Lock()


def event_id(event):
    """A stable id for an event line, derived from its content.

    Two events identical in all of these fields are the same event — the same
    author cannot post the same text on the same item in the same second twice
    — so collapsing them is correct rather than lossy.
    """
    raw = "|".join(str(event.get(k, "")) for k in
                   ("timestamp", "event_type", "repo", "number", "author", "details"))
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def load_state():
    if not STATE_FILE.exists():
        return {"read": {}}
    try:
        s = json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {"read": {}}
    s.setdefault("read", {})
    return s


def _save(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE_FILE)


def mark(read_ids=(), unread_ids=(), live_ids=None):
    """Mark ids read or unread, and forget ids that have aged out of the feed."""
    with _lock:
        state = load_state()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for i in read_ids:
            state["read"][i] = now
        for i in unread_ids:
            state["read"].pop(i, None)
        if live_ids is not None:
            state["read"] = {k: v for k, v in state["read"].items() if k in live_ids}
        _save(state)
        return state


def threads(events, items=None, state=None):
    """Group events into one entry per item, newest first.

    `items` is open_items.json, used only to say *why* a thread concerns you —
    your PR, a review request, a mention — which is the difference between an
    inbox and a firehose.
    """
    state = load_state() if state is None else state
    read_set = state.get("read", {})
    items = items or {}
    by_key, live = {}, set()

    for e in events:
        eid = event_id(e)
        if eid in live:
            continue        # the sync has written byte-identical lines twice
        live.add(eid)
        key = f"{e.get('repo', '')}#{e.get('number', '')}"
        read = eid in read_set
        t = by_key.get(key)
        if t is None:
            meta = _item_meta(items, e)
            t = by_key[key] = {
                "key": key, "repo": e.get("repo", ""), "number": e.get("number"),
                "category": e.get("category", "pr"), "title": e.get("title", ""),
                "url": _item_url(e), "relation": meta["relation"],
                "state": meta["state"], "events": [], "unread": 0,
                "latest": e.get("timestamp", ""), "authors": [],
            }
        t["events"].append({
            "id": eid, "read": read, "timestamp": e.get("timestamp", ""),
            "event_type": e.get("event_type", ""), "author": e.get("author", ""),
            "details": (e.get("details") or "")[:400],
        })
        if not read:
            t["unread"] += 1
        if e.get("author") and e["author"] not in t["authors"]:
            t["authors"].append(e["author"])
        if e.get("timestamp", "") > t["latest"]:
            t["latest"] = e["timestamp"]
            t["title"] = e.get("title", t["title"])

    out = sorted(by_key.values(), key=lambda t: t["latest"], reverse=True)
    for t in out:
        t["events"].sort(key=lambda x: x["timestamp"], reverse=True)
        t["total"] = len(t["events"])
    return out, live


def _item_url(event):
    """The item's own GitHub URL.

    events.jsonl stores the repo root in `url`, which opens the wrong page, so
    build the real one from the fields that are reliable.
    """
    repo, number = event.get("repo", ""), event.get("number")
    if not repo or not number:
        return event.get("url", "")
    kind = "pull" if event.get("category", "pr") == "pr" else "issues"
    return f"https://github.com/{repo}/{kind}/{number}"


def _item_meta(items, event):
    """Why this thread is on your board, if it still is."""
    bucket = "prs" if event.get("category", "pr") == "pr" else "issues"
    key = f"{event.get('repo', '')}#{event.get('number', '')}"
    it = (items.get(bucket) or {}).get(key) or {}
    return {"relation": it.get("relation", ""), "state": it.get("state", "")}


def summary(events, state=None):
    """Counts for a badge: unread events and how many threads they touch."""
    state = load_state() if state is None else state
    read_set = state.get("read", {})
    seen, keys = set(), set()
    for e in events:
        eid = event_id(e)
        if eid in seen:
            continue        # same duplicate collapsing as threads()
        seen.add(eid)
        if eid not in read_set:
            keys.add(f"{e.get('repo', '')}#{e.get('number', '')}")
    unread = len([1 for eid in seen if eid not in read_set])
    return {"unread": unread, "threads": len(keys)}
