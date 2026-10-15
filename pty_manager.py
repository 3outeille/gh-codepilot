"""PTY lifecycle management — spawns Claude Code in a pseudo-terminal and bridges I/O to SocketIO."""

import errno
import fcntl
import os
import pty
import select
import shutil
import signal
import struct
import termios

import eventlet

# Active PTY sessions keyed by SocketIO sid
active_ptys = {}

READ_CHUNK = 65536


def spawn_claude(sid, socketio, cwd, resume_session_id=None, env_extra=None,
                 initial_prompt=None):
    """
    Spawn Claude Code in a PTY at the given CWD. Optionally resume a session.

    env_extra: extra environment for the session (e.g. RELORE_API / RELORE_REPO
    so `relore` works bare inside it).
    initial_prompt: opening message for a fresh session; ignored on resume.
    """
    pid, fd = pty.fork()

    if pid == 0:
        # Child process
        os.chdir(str(cwd))
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env.update(env_extra or {})

        cmd = [shutil.which("claude") or "claude"]
        if resume_session_id:
            cmd.extend(["--resume", resume_session_id])
        elif initial_prompt:
            cmd.append(initial_prompt)

        os.execvpe(cmd[0], cmd, env)
    else:
        # Parent: set fd non-blocking so eventlet-patched select yields cooperatively
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        active_ptys[sid] = {"fd": fd, "pid": pid}
        greenlet = eventlet.spawn(_pty_reader, sid, fd, socketio)
        active_ptys[sid]["greenlet"] = greenlet


def _pty_reader(sid, fd, socketio):
    """Background greenlet that reads PTY output and emits to the client.

    Uses non-blocking fd + select (monkey-patched by eventlet) to yield
    cooperatively instead of occupying a real OS thread from tpool.
    """
    while True:
        try:
            r, _, _ = select.select([fd], [], [], 1.0)
        except (OSError, ValueError):
            break
        if not r:
            continue
        try:
            data = os.read(fd, READ_CHUNK)
        except BlockingIOError:
            continue
        except OSError as e:
            if e.errno == errno.EIO:
                # PTY closed (child exited on Linux)
                break
            break
        if not data:
            break
        socketio.emit("pty_output", {"data": data.decode("utf-8", errors="replace")}, to=sid)

    socketio.emit("pty_exit", {}, to=sid)
    _cleanup(sid)


def write_to_pty(sid, data):
    """Write user input to the PTY. Retries on EAGAIN for large pastes."""
    session = active_ptys.get(sid)
    if not session:
        return
    # Keystrokes arrive as separate socket events, each on its own greenthread.
    # Two of them blocking on the same fd makes eventlet raise "Second
    # simultaneous write on fileno", dropping input — so serialise per session.
    lock = session.setdefault("write_lock", eventlet.semaphore.Semaphore())
    with lock:
        _write_all(session["fd"], data.encode("utf-8"))


def _write_all(fd, buf):
    written = 0
    while written < len(buf):
        try:
            n = os.write(fd, buf[written:])
            if n <= 0:
                break
            written += n
        except BlockingIOError:
            # Wait briefly for room in the PTY write buffer
            try:
                select.select([], [fd], [], 0.5)
            except (OSError, ValueError):
                break
        except (OSError, IOError):
            break


def resize_pty(sid, rows, cols):
    """Resize the PTY window."""
    session = active_ptys.get(sid)
    if session:
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(session["fd"], termios.TIOCSWINSZ, winsize)
        except (OSError, IOError):
            pass


def kill_pty(sid):
    """Kill the PTY process. Reader greenlet handles cleanup on OSError."""
    session = active_ptys.get(sid)
    if not session:
        return

    try:
        os.kill(session["pid"], signal.SIGHUP)
    except (OSError, ProcessLookupError):
        _cleanup(sid)


def _cleanup(sid):
    """Close fd and remove from active sessions."""
    session = active_ptys.pop(sid, None)
    if session:
        try:
            os.close(session["fd"])
        except OSError:
            pass
        try:
            os.waitpid(session["pid"], os.WNOHANG)
        except (OSError, ChildProcessError):
            pass


def reap_dead_sessions():
    """Remove sessions whose child process has exited. Call periodically."""
    dead = []
    for sid, session in active_ptys.items():
        try:
            pid, status = os.waitpid(session["pid"], os.WNOHANG)
            if pid != 0:
                dead.append(sid)
        except (OSError, ChildProcessError):
            dead.append(sid)
    for sid in dead:
        _cleanup(sid)
