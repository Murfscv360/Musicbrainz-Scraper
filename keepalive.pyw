"""Self-heal launcher: keep the PicardWatch supervisor AND the enrichment worker alive.

Run periodically by the Windows scheduled task (windowless, via pythonw) plus at logon
and on resume-from-sleep. Each component is relaunched only if it isn't already running
(detected via its single-instance lock), so duplicates can't pile up:

  * supervisor (watch loop) — relaunched whenever it isn't holding its lock.
  * enrichment worker (`--enrich --analyze`) — relaunched only if it isn't running AND
    isn't finished (no `picardwatch-enrich.done`) AND wasn't stopped by the user (no
    `picardwatch-enrich.stop`). So it survives an antivirus kill mid-run, but quietly
    stays down once the library is fully enriched or you ran `run.py --stop-enrich`.

No console window is ever shown.
"""

import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def _pyw() -> str:
    p = os.path.join(HERE, ".venv", "Scripts", "pythonw.exe")
    return p if os.path.exists(p) else os.path.join(HERE, ".venv", "Scripts", "python.exe")


def _locked(name: str) -> bool:
    """True if a live process holds <name>'s lock (we cannot acquire it)."""
    try:
        import msvcrt
    except ImportError:
        return False
    try:
        fh = open(os.path.join(HERE, name), "a+")
    except OSError:
        return False
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        return False           # acquired -> nothing holds it
    except OSError:
        return True            # locked by a live process
    finally:
        fh.close()


def _launch(args) -> None:
    subprocess.Popen([_pyw(), os.path.join(HERE, "run.py")] + list(args), creationflags=NO_WINDOW)


def _exists(name: str) -> bool:
    return os.path.exists(os.path.join(HERE, name))


# 1) watcher supervisor — always keep it alive
if not _locked("picardwatch-supervisor.lock"):
    _launch(["--supervise"])

# 2) enrichment worker — relaunch only if not running, not done, and not stopped
if (not _exists("picardwatch-enrich.done")
        and not _exists("picardwatch-enrich.stop")
        and not _locked("picardwatch-enrich.lock")):
    _launch(["--enrich", "--analyze"])
