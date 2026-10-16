"""Run a PR's pytest target locally, in the worktree, with a per-PR venv.

One venv per worktree (repos/<owner>--<repo>--pr-<N>.venv), built with
`uv pip install -e <worktree>[testing]`, so the checkout is a real editable
install: new commits are picked up immediately, and if the PR changes
setup.py / pyproject.toml the install is redone automatically. uv's cache
hard-links packages, so extra venvs cost seconds, not gigabytes.
"""
import hashlib
import os
import re
import secrets
import signal
import subprocess
import threading
import time
from pathlib import Path

import repo_manager

RUNS_DIR = repo_manager.REPOS_DIR / ".runs"
PYTHON_VERSION = "3.11"
_runs = {}          # id -> dict(status, repo, number, target, log, pid, started, ended, rc)
_lock = threading.Lock()


def venv_dir(repo, number):
    return repo_manager.REPOS_DIR / f"{repo_manager._repo_dir_name(repo)}--pr-{number}.venv"


def _deps_fingerprint(wt):
    """Hash of the files that define dependencies; a change means reinstall."""
    h = hashlib.sha1()
    for name in ("setup.py", "pyproject.toml", "setup.cfg"):
        f = wt / name
        if f.exists():
            h.update(name.encode()); h.update(f.read_bytes())
    return h.hexdigest()


def env_ready(repo, number):
    """True when the venv exists and was installed for the current dependency files."""
    vd = venv_dir(repo, number); ok = vd / ".codepilot-ok"
    wt = repo_manager.get_worktree_path(repo, number, "pr")
    return (vd / "bin" / "python").exists() and ok.exists() and wt.exists() \
        and ok.read_text().strip() == _deps_fingerprint(wt)


def env_info(repo, number):
    """What a run for this PR would use: venv, python, and which transformers/torch it imports."""
    vd = venv_dir(repo, number)
    wt = repo_manager.get_worktree_path(repo, number, "pr")
    info = {"venv": str(vd), "env_ready": env_ready(repo, number), "worktree": str(wt),
            "worktree_exists": wt.exists(), "deps_installed_at": None, "python": None,
            "transformers_from": None, "torch": None, "head": None}
    ok = vd / ".codepilot-ok"
    if ok.exists():
        info["deps_installed_at"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(ok.stat().st_mtime))
        info["deps_stale"] = wt.exists() and ok.read_text().strip() != _deps_fingerprint(wt)
    if wt.exists():
        r = subprocess.run(["git", "-C", str(wt), "log", "--oneline", "-1"], capture_output=True, text=True)
        info["head"] = r.stdout.strip()
    if info["env_ready"] and wt.exists():
        code = ("import sys, transformers, torch; "
                "print(sys.version.split()[0]); print(transformers.__file__); print(torch.__version__)")
        r = subprocess.run([str(vd / "bin" / "python"), "-c", code], capture_output=True, text=True,
                           cwd=str(wt), timeout=120)
        if r.returncode == 0:
            info["python"], info["transformers_from"], info["torch"] = r.stdout.strip().split("\n")
        else:
            info["import_error"] = r.stderr.strip()[-600:]
    info["uses_pr_code"] = bool(info["transformers_from"] and info["transformers_from"].startswith(str(wt)))
    return info


def _setup_env(repo, number, wt, log):
    """Create the PR's venv and `uv pip install -e <worktree>[testing]`. Returns rc."""
    vd = venv_dir(repo, number)
    steps = [
        ["uv", "venv", "--python", PYTHON_VERSION, "--allow-existing", str(vd)],
        ["uv", "pip", "install", "--python", str(vd / "bin" / "python"), "-q",
         "-e", f"{wt}[testing]", "torch", "torchvision", "torchaudio"],
    ]
    for cmd in steps:
        log.write(f"$ {' '.join(cmd)}\n"); log.flush()
        rc = subprocess.call(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=str(wt))
        if rc != 0:
            log.write(f"[setup failed rc={rc}]\n"); log.flush()
            return rc
    (vd / ".codepilot-ok").write_text(_deps_fingerprint(wt))
    log.write("[env ready: editable install of the checkout]\n"); log.flush()
    return 0


def _spawn(run):
    repo, number = run["repo"], run["number"]
    wt = repo_manager.get_worktree_path(repo, number, "pr")
    vd = venv_dir(repo, number)
    with open(run["log"], "a", buffering=1) as log:
        if not wt.exists():
            log.write(f"worktree {wt} missing: open the PR session once first\n")
            _finish(run, 2); return
        if run.get("refresh") or not env_ready(repo, number):
            why = "requested" if run.get("refresh") else ("dependency files changed" if (vd / ".codepilot-ok").exists() else "first run for this PR")
            log.write(f"== installing env for this PR ({why}): uv pip install -e .[testing] ==\n")
            if _setup_env(repo, number, wt, log) != 0:
                _finish(run, 2); return
        env = {**os.environ,
               "PATH": f"{vd / 'bin'}:{os.environ.get('PATH', '')}",
               "VIRTUAL_ENV": str(vd),
               "PYTHONUNBUFFERED": "1",
               "HF_HUB_DISABLE_PROGRESS_BARS": "1",
               "TRANSFORMERS_IS_CI": "1"}
        for k in ("RUN_SLOW",):
            if run.get("env", {}).get(k):
                env[k] = run["env"][k]
        cmd = [str(vd / "bin" / "python"), "-m", "pytest", "-v", "-rA", "--durations=20",
               *run["pytest_args"].split(), *run["target"].split()]
        log.write(f"$ (cd {wt}) {' '.join(cmd)}\n")
        proc = subprocess.Popen(cmd, cwd=str(wt), env=env, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
        run["pid"] = proc.pid
        rc = proc.wait()
        log.write(f"\n[exit {rc}]\n")
        _finish(run, rc)


def _finish(run, rc):
    run["rc"] = rc
    run["status"] = "passed" if rc == 0 else ("cancelled" if rc == -9 or run.get("cancelled") else "failed")
    run["ended"] = time.time()


def start(repo, number, target, pytest_args="", refresh=False, run_slow=False):
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    rid = secrets.token_hex(6)
    run = {"id": rid, "repo": repo, "number": int(number), "target": target, "pytest_args": pytest_args,
           "log": str(RUNS_DIR / f"{rid}.log"), "status": "running", "pid": None,
           "started": time.time(), "ended": None, "rc": None, "refresh": refresh,
           "env": {"RUN_SLOW": "1"} if run_slow else {}}
    Path(run["log"]).write_text("")
    with _lock:
        _runs[rid] = run
    # A real OS thread: pytest can run for minutes and must not sit on the eventlet hub.
    threading.Thread(target=_spawn, args=(run,), daemon=True).start()
    return public(run)


def _spawn_ssh(run):
    """Run the PR's tests on a warm HF Jobs box over ssh (cloudflared tcp tunnel)."""
    with open(run["log"], "a", buffering=1) as log:
        cf = run["cloudflared"]
        number, head = run["number"], run.get("head") or "FETCH_HEAD"
        script = (
            "source /root/.ssh/environment.sh 2>/dev/null; cd /work || exit 2; "
            f"git fetch -q origin pull/{number}/head && git checkout -q {head} && "
            "git log --oneline -1 && "
            f"python -m pytest -v -rA --durations=20 {run['pytest_args']} {run['target']}"
        )
        cmd = ["ssh", "-p", "2222", "-o", f"ProxyCommand={cf} access tcp --hostname {run['host']}",
               "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
               "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=40", "-o", "ServerAliveInterval=30",
               f"root@{run['host']}", script]
        log.write(f"$ [box {run['box']} @ {run['host']}] {script}\n")
        env = {**os.environ, "PATH": f"{Path(cf).parent}:{os.environ.get('PATH', '')}"}
        proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        run["pid"] = proc.pid
        rc = proc.wait()
        log.write(f"\n[exit {rc}]\n")
        _finish(run, rc)


def start_ssh(repo, number, target, pytest_args, box, host, cloudflared, head=""):
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    rid = secrets.token_hex(6)
    run = {"id": rid, "repo": repo, "number": int(number), "target": target, "pytest_args": pytest_args,
           "log": str(RUNS_DIR / f"{rid}.log"), "status": "running", "pid": None,
           "started": time.time(), "ended": None, "rc": None, "box": box, "host": host,
           "cloudflared": cloudflared, "head": head}
    Path(run["log"]).write_text("")
    with _lock:
        _runs[rid] = run
    threading.Thread(target=_spawn_ssh, args=(run,), daemon=True).start()
    return public(run)


def public(run):
    d = {k: run[k] for k in ("id", "repo", "number", "target", "pytest_args", "status",
                             "started", "ended", "rc")}
    d["box"] = run.get("box")
    return d


def list_runs(repo, number):
    with _lock:
        rs = [public(r) for r in _runs.values() if r["repo"] == repo and r["number"] == int(number)]
    return sorted(rs, key=lambda r: r["started"], reverse=True)


def read_log(rid, offset=0):
    run = _runs.get(rid)
    if not run:
        return None
    try:
        with open(run["log"], "rb") as f:
            f.seek(offset)
            data = f.read()
    except FileNotFoundError:
        data = b""
    return {"id": rid, "status": run["status"], "offset": offset + len(data),
            "text": data.decode("utf-8", "replace")}


def cancel(rid):
    run = _runs.get(rid)
    if not run or run["status"] != "running":
        return False
    run["cancelled"] = True
    if run.get("pid"):
        try:
            os.killpg(run["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass
    return True
