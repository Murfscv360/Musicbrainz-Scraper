"""Self-heal launcher: (re)start the PicardWatch supervisor if it isn't running.

Run periodically by a Windows scheduled task (windowless, via pythonw) plus at logon and
on resume-from-sleep. If a supervisor is already alive it holds picardwatch-supervisor.lock,
so we detect that and do nothing; otherwise we launch one. This keeps the watcher running
across crashes, sleeps, and resume-from-hibernate without spawning duplicates (the
supervisor also self-locks). No console window is ever shown.
"""

import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _supervisor_running() -> bool:
    """True if a live supervisor holds the lock (we can't acquire it)."""
    try:
        import msvcrt
    except ImportError:
        return False
    try:
        fh = open(os.path.join(HERE, "picardwatch-supervisor.lock"), "a+")
    except OSError:
        return False
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        return False           # acquired -> nothing is supervising
    except OSError:
        return True            # locked by a live supervisor
    finally:
        fh.close()


if not _supervisor_running():
    pyw = os.path.join(HERE, ".venv", "Scripts", "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = os.path.join(HERE, ".venv", "Scripts", "python.exe")
    subprocess.Popen([pyw, os.path.join(HERE, "run.py"), "--supervise"],
                     creationflags=NO_WINDOW)
