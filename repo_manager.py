"""Git clone and worktree management for watched repos."""

import os
import signal
import subprocess
from pathlib import Path

REPOS_DIR = Path(__file__).parent / "repos"

# Existing local clones to borrow objects from on first clone, keyed by
# 'owner/repo'. Cuts the network transfer to just what's new since that
# clone's last fetch. --dissociate copies the borrowed objects so our clone
# stays independent if the source is later moved or deleted.
REFERENCE_CLONES = {
    "huggingface/transformers": Path.home() / "Documents/dev/transformers",
}


# Network git operations (fetch/pull/clone) can hang indefinitely when SSH to
# GitHub is unreachable. Bound them so a dead network degrades to "use what is
# already cloned" instead of blocking the spawn forever.
NETWORK_TIMEOUT = 30
CLONE_TIMEOUT = 1800

# Set when a network op times out so later ops in the same call skip the
# network instead of each paying the full timeout.
_offline = False


# Make ssh give up quickly instead of hanging in the banner exchange.
_GIT_ENV = {**os.environ,
            "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh")
            + " -o ConnectTimeout=15 -o BatchMode=yes"}


def _run(cmd, cwd=None, check=True, timeout=NETWORK_TIMEOUT, network=False):
    global _offline
    if network and _offline:
        return subprocess.CompletedProcess(cmd, returncode=124, stdout="",
                                           stderr="skipped: network unreachable")
    # New session so a timeout can kill git *and* its ssh child.
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=_GIT_ENV,
                            start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except Exception as e:
        # Under eventlet monkey-patching the raised TimeoutExpired is a
        # different class object than subprocess.TimeoutExpired, so match by name.
        if type(e).__name__ != "TimeoutExpired":
            raise
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate()
        if network:
            _offline = True
        if check:
            raise RuntimeError(f"timed out after {timeout}s: {' '.join(cmd)}")
        return subprocess.CompletedProcess(cmd, returncode=124, stdout="",
                                           stderr=f"timed out after {timeout}s")
    res = subprocess.CompletedProcess(cmd, proc.returncode, out, err)
    if check and proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, out, err)
    return res


def _repo_dir_name(repo):
    """Convert 'owner/repo' to 'owner--repo'."""
    return repo.replace("/", "--")


def ensure_repo(repo):
    """Clone repo if missing, fetch if exists. Returns clone path."""
    global _offline
    _offline = False
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    repo_dir = REPOS_DIR / _repo_dir_name(repo)

    if not repo_dir.exists():
        cmd = ["git", "clone"]
        ref = REFERENCE_CLONES.get(repo)
        if ref and (ref / ".git").exists():
            cmd += ["--reference-if-able", str(ref), "--dissociate"]
        cmd += [f"git@github.com:{repo}.git", str(repo_dir)]
        _run(cmd, timeout=CLONE_TIMEOUT, network=True)
    else:
        _run(["git", "-C", str(repo_dir), "fetch", "--all", "--prune"], check=False, network=True)

    return repo_dir


def _get_pr_branch(repo, number):
    """Get the head branch name for a PR."""
    result = _run(
        ["gh", "pr", "view", str(number), "--repo", repo,
         "--json", "headRefName", "-q", ".headRefName"],
        check=False, network=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None


def ensure_worktree(repo, number, category):
    """
    Prepare a working directory for Claude Code.

    For PRs: creates a git worktree checked out to the PR branch.
    For issues: uses the main clone on the default branch.

    Returns the working directory path.
    """
    repo_dir = REPOS_DIR / _repo_dir_name(repo)
    wt_dir = REPOS_DIR / f"{_repo_dir_name(repo)}--pr-{number}"
    if category == "pr" and wt_dir.exists() and (wt_dir / ".git").exists():
        # Fast path: one network round-trip. `pull --ff-only` fetches just the
        # checked-out branch; no `fetch --all`, no `gh pr view` (each 5–30 s here).
        _run(["git", "-C", str(wt_dir), "pull", "--ff-only"], check=False, network=True)
        return wt_dir

    repo_dir = ensure_repo(repo)

    if category != "pr":
        # Issues use the main clone directory; bring its checked-out branch
        # up to date (ensure_repo only fetched).
        _run(["git", "-C", str(repo_dir), "pull", "--ff-only"], check=False, network=True)
        return repo_dir

    # PRs get their own worktree
    branch = _get_pr_branch(repo, number)
    if not branch:
        # Fallback: fetch the PR ref directly
        _run(
            ["git", "-C", str(repo_dir), "fetch", "origin",
             f"pull/{number}/head:pr-{number}"],
            check=False, network=True,
        )
        branch = f"pr-{number}"

    if wt_dir.exists():
        # Worktree exists — update it
        _run(["git", "-C", str(wt_dir), "fetch", "origin"], check=False, network=True)
        _run(["git", "-C", str(wt_dir), "checkout", branch], check=False)
        _run(["git", "-C", str(wt_dir), "pull", "--ff-only"], check=False, network=True)
    else:
        # Fetch the branch in the main clone first
        _run(
            ["git", "-C", str(repo_dir), "fetch", "origin", f"{branch}:{branch}"],
            check=False, network=True,
        )
        result = _run(
            ["git", "-C", str(repo_dir), "worktree", "add", str(wt_dir), branch],
            check=False,
        )
        if result.returncode != 0:
            # Branch may already exist locally, try checkout approach
            _run(
                ["git", "-C", str(repo_dir), "worktree", "add", str(wt_dir)],
                check=False,
            )
            _run(["git", "-C", str(wt_dir), "checkout", branch], check=False)

    return wt_dir


def get_worktree_path(repo, number, category):
    """Get the expected working directory path without creating it."""
    if category != "pr":
        return REPOS_DIR / _repo_dir_name(repo)
    return REPOS_DIR / f"{_repo_dir_name(repo)}--pr-{number}"
