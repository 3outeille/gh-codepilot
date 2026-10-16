#!/usr/bin/env python3
"""
Tracks all open PRs/issues and their activity for watched GitHub repos.

Data files:
  - open_items.json: all currently open PRs and issues (refreshed every run)
  - events.jsonl: activity log from repo events API (correct timestamps)
  - .notif_state.json: tracking state (seen event IDs, last check time)
"""

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
REPOS_FILE = SCRIPT_DIR / "repos.conf"
EVENTS_FILE = SCRIPT_DIR / "events.jsonl"
ITEMS_FILE = SCRIPT_DIR / "open_items.json"
STATE_FILE = SCRIPT_DIR / ".notif_state.json"


# ── Helpers ──────────────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists() and STATE_FILE.stat().st_size > 0:
        return json.loads(STATE_FILE.read_text())
    return {"seen_event_ids": {}}


def _atomic_write(path, content):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content)
    tmp.replace(path)


def save_state(state):
    _atomic_write(STATE_FILE, json.dumps(state, indent=2))


def load_repos():
    if not REPOS_FILE.exists():
        print(f"Error: {REPOS_FILE} not found. Copy repos.conf.example to repos.conf and add your repos.")
        return []
    repos = []
    for line in REPOS_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "/" in line:
            repos.append(line)
    return repos


def _find_gh():
    """cron runs with a bare PATH, so look where Homebrew and installers put gh."""
    import shutil
    found = shutil.which("gh")
    if found:
        return found
    for cand in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh", str(Path.home() / ".local/bin/gh")):
        if Path(cand).exists():
            return cand
    return "gh"


GH = _find_gh()


def gh_api(endpoint):
    result = subprocess.run([GH, "api", endpoint], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def gh_cli(args):
    result = subprocess.run([GH] + args, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


import platform

def desktop_notify(title, body):
    if platform.system() == "Darwin":
        # macOS
        script = f'display notification "{body}" with title "{title}"'
        subprocess.run(["osascript", "-e", script], capture_output=True)
    else:
        # Linux
        subprocess.run(
            ["notify-send", "--app-name=GitHub", "--icon=dialog-information",
             "--urgency=normal", title, body],
            capture_output=True,
        )


def log_event(event_type, category, repo, number, title, author, url, details, timestamp):
    entry = json.dumps({
        "timestamp": timestamp, "event_type": event_type, "category": category,
        "repo": repo, "number": number, "title": title, "author": author,
        "url": url, "details": details,
    })
    with open(EVENTS_FILE, "a") as f:
        f.write(entry + "\n")


# ── Open Items Sync ──────────────────────────────────────────────────────────

def load_open_items():
    if ITEMS_FILE.exists() and ITEMS_FILE.stat().st_size > 0:
        return json.loads(ITEMS_FILE.read_text())
    return {"prs": {}, "issues": {}}


def save_open_items(items):
    _atomic_write(ITEMS_FILE, json.dumps(items, indent=2))


def _compute_ci_status(checks):
    """Summarize CI check results into a single status string."""
    if not checks:
        return ""
    statuses = set()
    for check in checks:
        conclusion = (check.get("conclusion") or "").upper()
        status = (check.get("status") or "").upper()
        if status != "COMPLETED":
            statuses.add("PENDING")
        elif conclusion == "SUCCESS" or conclusion == "NEUTRAL" or conclusion == "SKIPPED":
            statuses.add("SUCCESS")
        elif conclusion == "FAILURE" or conclusion == "TIMED_OUT":
            statuses.add("FAILURE")
        else:
            statuses.add("PENDING")
    if "FAILURE" in statuses:
        return "failure"
    if "PENDING" in statuses:
        return "pending"
    return "success"


def gh_user():
    """Current authenticated GitHub login, cached per process."""
    global _GH_USER
    if _GH_USER is None:
        result = subprocess.run([GH, "api", "user", "-q", ".login"],
                                capture_output=True, text=True)
        _GH_USER = result.stdout.strip() if result.returncode == 0 else ""
    return _GH_USER


_GH_USER = None

# Relation precedence: an item you authored outranks a review request,
# which outranks a plain mention.
_RELATION_RANK = {"mine": 4, "review_requested": 3, "reviewed": 2, "mentioned": 1}

# isDraft exists on `gh search prs` only; passing it to `gh search issues`
# makes the whole query fail.
_SEARCH_FIELDS = {
    "prs": "number,title,url,author,createdAt,repository,isDraft",
    "issues": "number,title,url,author,createdAt,repository",
}


def _search(kind, qualifier, repos):
    """Run `gh search <kind> --<qualifier> @me` scoped to the watched repos."""
    args = ["search", kind, f"--{qualifier}", "@me", "--state", "open",
            "--json", _SEARCH_FIELDS[kind], "--limit", "100"]
    for repo in repos:
        args += ["--repo", repo]
    return gh_cli(args)


def _enrich_pr(repo, number):
    """Fetch review decision + CI rollup for a single PR."""
    data = gh_cli(["pr", "view", str(number), "--repo", repo,
                   "--json", "reviewDecision,isDraft,statusCheckRollup,author,"
                             "additions,deletions,changedFiles,latestReviews,"
                             "reviews,comments,commits"])
    if data is None:
        return {}
    author = (data.get("author") or {}).get("login", "")
    # One entry per reviewer, latest state only.
    latest = data.get("latestReviews") or []
    approvals = sum(1 for r in latest if r.get("state") == "APPROVED")

    # "Who spoke last": a reviewer's comment after the author's last reply or
    # push means the ball is in the author's court, whatever reviewDecision says.
    def is_human(login):
        return login and not login.endswith("[bot]") and login not in ("github-actions", "HuggingFaceDocBuilderDev")

    me = gh_user()
    my_state = next((r.get("state") for r in latest if (r.get("author") or {}).get("login") == me), "")
    my_at = ""
    reviewer_at, author_at, reviewers = "", "", set()
    for r in data.get("reviews") or []:
        who, at = (r.get("author") or {}).get("login", ""), r.get("submittedAt") or ""
        if who == me:
            my_at = max(my_at, at)
        if who == author:
            author_at = max(author_at, at)
        elif is_human(who):
            reviewer_at = max(reviewer_at, at); reviewers.add(who)
    for c in data.get("comments") or []:
        who, at = (c.get("author") or {}).get("login", ""), c.get("createdAt") or ""
        if who == author:
            author_at = max(author_at, at)
        elif is_human(who):
            reviewer_at = max(reviewer_at, at)
    for c in data.get("commits") or []:
        author_at = max(author_at, c.get("committedDate") or "")

    rollup = data.get("statusCheckRollup") or []
    failing = []
    for c in rollup:
        state = (c.get("conclusion") or c.get("state") or "").upper()
        if state in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"):
            failing.append({
                "name": c.get("name") or c.get("context") or "",
                "workflow": c.get("workflowName") or "",
                "url": c.get("detailsUrl") or c.get("targetUrl") or "",
            })
    # The umbrella "status" job just mirrors the others; list real jobs first.
    failing.sort(key=lambda f: ("status" in f["name"].lower(), f["name"]))

    return {
        "review_decision": data.get("reviewDecision") or "",
        "is_draft": data.get("isDraft", False),
        "ci_status": _compute_ci_status(rollup),
        "failing_checks": failing[:8],
        "lines_changed": (data.get("additions") or 0) + (data.get("deletions") or 0),
        "changed_files": data.get("changedFiles") or 0,
        "reviewer_count": len(latest),
        "approval_count": approvals,
        "reviewed_by": sorted(reviewers),
        "last_reviewer_at": reviewer_at,
        "last_author_at": author_at,
        # Your own latest review (APPROVED / CHANGES_REQUESTED / COMMENTED) and when.
        "my_review_state": my_state,
        "my_review_at": my_at,
    }


def _mention_snippet(repo, number):
    """Latest comment that @-mentions you on this item: {snippet, author, at}."""
    login = gh_user()
    if not login:
        return {}
    needle = f"@{login}".lower()
    best = None

    def consider(body, user, at):
        nonlocal best
        if body and needle in body.lower() and (best is None or at > best["mention_at"]):
            best = {
                "mention_snippet": " ".join(body.split())[:160],
                "mention_by": (user or {}).get("login", ""),
                "mention_at": at,
            }

    # The opening description counts as a mention too (issues endpoint covers PRs).
    item = gh_api(f"/repos/{repo}/issues/{number}") or {}
    consider(item.get("body"), item.get("user"), item.get("created_at", ""))

    for endpoint in (f"/repos/{repo}/issues/{number}/comments?per_page=100",
                     f"/repos/{repo}/pulls/{number}/comments?per_page=100",
                     f"/repos/{repo}/pulls/{number}/reviews?per_page=100"):
        for c in gh_api(endpoint) or []:
            consider(c.get("body"), c.get("user"),
                     c.get("created_at") or c.get("submitted_at", ""))
    return best or {}


def _collect(kind, qualifiers, repos):
    """Merge several searches into {key: item}, keeping the strongest relation."""
    merged = {}
    fetched_any = False
    for qualifier, relation in qualifiers:
        results = _search(kind, qualifier, repos)
        if results is None:
            continue
        fetched_any = True
        for r in results:
            repo = r.get("repository", {}).get("nameWithOwner", "")
            if not repo:
                continue
            key = f"{repo}#{r['number']}"
            existing = merged.get(key)
            if existing and _RELATION_RANK[existing["relation"]] >= _RELATION_RANK[relation]:
                continue
            merged[key] = {
                "repo": repo, "number": r["number"], "title": r["title"],
                "url": r["url"], "author": r.get("author", {}).get("login", ""),
                "created_at": r.get("createdAt", ""),
                "is_draft": r.get("isDraft", False),
                "relation": relation,
            }
    return merged, fetched_any


def sync_open_items(repos):
    """
    Fetch the current user's open PRs and issues across all of GitHub.

    Three PR buckets, merged by strongest relation:
      mine              — you opened it
      review_requested  — your review is requested
      mentioned         — you were @-mentioned

    Scoped to the repos listed in repos.conf.
    """
    prev = load_open_items()
    prev_pr_keys = set(prev.get("prs", {}).keys())
    prev_issue_keys = set(prev.get("issues", {}).keys())
    is_first_run = not ITEMS_FILE.exists() or ITEMS_FILE.stat().st_size == 0

    prs, prs_ok = _collect("prs", [
        ("mentions", "mentioned"),
        ("reviewed-by", "reviewed"),            # you reviewed it: waiting on the author, or you approved
        ("review-requested", "review_requested"),
        ("author", "mine"),
    ], repos)
    issues, issues_ok = _collect("issues", [
        ("mentions", "mentioned"),
        ("author", "mine"),
    ], repos)

    # Review decision, CI, and mention snippets each need per-item calls;
    # run them concurrently.
    jobs = []
    for key, item in prs.items():
        jobs.append((prs, key, _enrich_pr, (item["repo"], item["number"])))
        if item["relation"] == "mentioned":
            jobs.append((prs, key, _mention_snippet, (item["repo"], item["number"])))
    for key, item in issues.items():
        if item["relation"] == "mentioned":
            jobs.append((issues, key, _mention_snippet, (item["repo"], item["number"])))

    if jobs:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(fn, *args): (bucket, key)
                       for bucket, key, fn, args in jobs}
            for future in as_completed(futures):
                bucket, key = futures[future]
                try:
                    bucket[key].update(future.result())
                except Exception:
                    pass

    current = {"prs": prs, "issues": issues}

    if not is_first_run:
        for key, pr in prs.items():
            if key not in prev_pr_keys:
                desktop_notify(
                    f"PR ({pr['relation'].replace('_', ' ')}) in {pr['repo']}",
                    f"#{pr['number']}: {pr['title']}\nby {pr['author']}\n{pr['url']}",
                )
        for key, issue in issues.items():
            if key not in prev_issue_keys:
                desktop_notify(
                    f"Issue ({issue['relation']}) in {issue['repo']}",
                    f"#{issue['number']}: {issue['title']}\nby {issue['author']}\n{issue['url']}",
                )

    for key, pr in prs.items():
        if key not in prev_pr_keys:
            log_event("new_pr", "pr", pr["repo"], pr["number"], pr["title"],
                      pr["author"], pr["url"], "Pull request opened",
                      timestamp=pr.get("created_at", ""))
    for key, issue in issues.items():
        if key not in prev_issue_keys:
            log_event("new_issue", "issue", issue["repo"], issue["number"],
                      issue["title"], issue["author"], issue["url"], "Issue opened",
                      timestamp=issue.get("created_at", ""))

    if prs_ok or issues_ok:
        save_open_items(current)
    else:
        current = prev
    return current


def repos_from_items(open_items):
    """Repos that actually contain one of your items — used to scope the event feed."""
    repos = set()
    for section in ("prs", "issues"):
        for item in open_items.get(section, {}).values():
            repos.add(item["repo"])
    return sorted(repos)



# ── Repo Events API ──────────────────────────────────────────────────────────

def process_repo_events(repos, open_items, state, is_first_run=False):
    """Fetch repo events for all watched repos. Correct timestamps, all users."""
    seen = state.get("seen_event_ids", {})
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Build lookup for open item titles
    all_items = {}
    for key, item in open_items.get("prs", {}).items():
        all_items[(item["repo"], item["number"])] = item
    for key, item in open_items.get("issues", {}).items():
        all_items[(item["repo"], item["number"])] = item

    for repo in repos:
        events = gh_api(f"/repos/{repo}/events?per_page=100")
        if not events:
            continue

        for event in events:
            event_id = str(event.get("id", ""))
            if event_id in seen:
                continue

            created_at = event.get("created_at", "")
            if created_at < cutoff:
                seen[event_id] = created_at
                continue

            actor = event.get("actor", {}).get("login", "")
            event_type_gh = event.get("type", "")
            payload = event.get("payload", {})

            # ── PR Comments ──
            if event_type_gh == "IssueCommentEvent":
                issue = payload.get("issue", {})
                number = issue.get("number", 0)
                is_pr = "pull_request" in issue
                category = "pr" if is_pr else "issue"
                evt_type = "new_comment" if is_pr else "issue_comment"
                title = issue.get("title", "")
                html_url = issue.get("html_url", "")
                body = payload.get("comment", {}).get("body", "")[:120].replace("\n", " ")
                details = f"{actor}: {body}" if body else f"{actor} commented"

            # ── PR Reviews ──
            elif event_type_gh == "PullRequestReviewEvent":
                pr = payload.get("pull_request", {})
                number = pr.get("number", 0)
                title = pr.get("title", "")
                category = "pr"
                evt_type = "new_comment"
                html_url = pr.get("html_url", "")
                review = payload.get("review", {})
                review_body = review.get("body", "")
                review_state = review.get("state", "")
                if review_body:
                    details = f"{actor}: {review_body[:120].replace(chr(10), ' ')}"
                else:
                    labels = {"approved": "Approved", "changes_requested": "Changes requested",
                              "commented": "Reviewed", "dismissed": "Review dismissed"}
                    details = f"{actor}: {labels.get(review_state, 'Reviewed')}"

            # ── PR Review Comments (inline code comments) ──
            elif event_type_gh == "PullRequestReviewCommentEvent":
                pr = payload.get("pull_request", {})
                number = pr.get("number", 0)
                title = pr.get("title", "")
                category = "pr"
                evt_type = "new_comment"
                html_url = pr.get("html_url", "")
                body = payload.get("comment", {}).get("body", "")[:120].replace("\n", " ")
                details = f"{actor}: {body}" if body else f"{actor} commented on code"

            # ── Issue Events (opened, closed, reopened, labeled, etc.) ──
            elif event_type_gh == "IssuesEvent":
                issue = payload.get("issue", {})
                number = issue.get("number", 0)
                title = issue.get("title", "")
                category = "issue"
                action = payload.get("action", "")
                if action in ("opened", "closed", "reopened"):
                    evt_type = "new_issue"
                    html_url = issue.get("html_url", "")
                    details = f"{actor}: Issue {action}"
                else:
                    seen[event_id] = created_at
                    continue

            # ── PR Events (opened, closed, merged, review_requested, etc.) ──
            elif event_type_gh == "PullRequestEvent":
                pr = payload.get("pull_request", {})
                number = pr.get("number", 0)
                title = pr.get("title", "")
                category = "pr"
                action = payload.get("action", "")
                html_url = pr.get("html_url", "")
                if action == "review_requested":
                    evt_type = "new_comment"
                    requested = payload.get("requested_reviewer", {}).get("login", "")
                    details = f"Review requested from {requested}" if requested else "Review requested"
                elif action in ("closed", "reopened"):
                    merged = pr.get("merged", False)
                    evt_type = "new_pr"
                    details = f"{actor}: PR {'merged' if merged else action}"
                else:
                    seen[event_id] = created_at
                    continue

            else:
                seen[event_id] = created_at
                continue

            # Only surface activity on items that are yours
            if (repo, number) not in all_items:
                seen[event_id] = created_at
                continue

            # Look up title from open items if missing
            if not title:
                item = all_items.get((repo, number))
                if item:
                    title = item["title"]

            if not is_first_run:
                desktop_notify(
                    f"Activity in {repo}",
                    f"#{number}: {title}\n{details}",
                )
            log_event(evt_type, category, repo, number, title, actor,
                      html_url or f"https://github.com/{repo}", details,
                      timestamp=created_at)

            seen[event_id] = created_at

    # Prune seen IDs older than the cutoff
    state["seen_event_ids"] = {
        eid: ts for eid, ts in seen.items()
        if isinstance(ts, str) and ts >= cutoff
    }


# ── Events Pruning ───────────────────────────────────────────────────────────

def prune_events():
    if not EVENTS_FILE.exists():
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = EVENTS_FILE.read_text().splitlines()
    kept = []
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            ts = event.get("timestamp", "")
            if not ts or ts >= cutoff:
                kept.append(line)
        except (json.JSONDecodeError, KeyError):
            continue
    EVENTS_FILE.write_text("\n".join(kept) + "\n" if kept else "")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not gh_user():
        print("Error: could not resolve GitHub user. Run: gh auth login")
        return

    repos = load_repos()
    if not repos:
        print(f"Error: no repos in {REPOS_FILE}. Add one `owner/repo` per line.")
        return

    state = load_state()
    is_first_run = not ITEMS_FILE.exists() or ITEMS_FILE.stat().st_size == 0

    # 1. Sync your open PRs/issues (authored, mentioned, review-requested)
    open_items = sync_open_items(repos)

    # 2. Activity feed, scoped to the repos your items live in
    process_repo_events(repos_from_items(open_items), open_items, state,
                        is_first_run=is_first_run)
    save_state(state)

    # 3. Prune old events
    prune_events()


if __name__ == "__main__":
    main()
