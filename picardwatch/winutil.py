"""Windows: stop child processes from flashing console windows.

A windowless/background daemon (pythonw) that spawns a console executable — fpcalc for
every track, taskkill from the supervisor — pops a brief black console window for each
call. On a busy import that's constant flashing. Defaulting every subprocess to
CREATE_NO_WINDOW keeps the daemon truly silent. Call suppress_child_windows() once at
startup, before anything spawns a subprocess (notably the AcoustID fingerprinter).
"""

from __future__ import annotations

import os
import subprocess

_PATCHED = False


def suppress_child_windows() -> None:
    global _PATCHED
    if os.name != "nt" or _PATCHED:
        return
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    _orig_init = subprocess.Popen.__init__

    def _init(self, *args, **kwargs):
        # only set it when the caller hasn't asked for specific creation flags
        if not kwargs.get("creationflags"):
            kwargs["creationflags"] = no_window
        _orig_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _init  # type: ignore[method-assign]
    _PATCHED = True
