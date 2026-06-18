"""Load config.yaml into a dot-accessible namespace."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import yaml


def _ns(obj):
    if isinstance(obj, dict):
        return SimpleNamespace(**{k: _ns(v) for k, v in obj.items()})
    if isinstance(obj, list):
        return [_ns(x) for x in obj]
    return obj


def _bootstrap_fpcalc() -> None:
    """If the installer placed a bundled fpcalc in <project>/bin, point pyacoustid at
    it via the FPCALC env var so the user never has to touch the system PATH."""
    if os.environ.get("FPCALC"):
        return
    root = Path(__file__).resolve().parent.parent  # project root (contains bin/)
    name = "fpcalc.exe" if os.name == "nt" else "fpcalc"
    bundled = root / "bin" / name
    if bundled.exists():
        os.environ["FPCALC"] = str(bundled)


def load_config(path: str | Path) -> SimpleNamespace:
    _bootstrap_fpcalc()
    path = Path(path)
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    cfg = _ns(raw)
    # Make logs dir exist so logging never fails on first run.
    Path(cfg.paths.log_dir).mkdir(parents=True, exist_ok=True)
    return cfg
