"""Publish a scanner status snapshot to the repo (STATUS.md + scanner-status.json) and
git commit + push. Built to run every ~15 min from a scheduled task: fast, LOCAL-only
(SQLite + log files + lock files -- never touches the slow NAS), wrapped so it can never
hang or crash the schedule. Another chat/session reads STATUS.md from the repo on GitHub.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone

PROJ = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(PROJ, "logs")
DB = os.path.join(PROJ, "state.sqlite3")


def _db():
    try:
        uri = "file:" + DB.replace("\\", "/") + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=8)
        c = {r[0]: r[1] for r in con.execute("select status,count(*) from albums group by status")}
        now = time.time()
        last = con.execute("select max(decided_at) from albums").fetchone()[0] or 0
        d15 = con.execute("select count(*) from albums where decided_at>?", (now - 900,)).fetchone()[0]
        d60 = con.execute("select count(*) from albums where decided_at>?", (now - 3600,)).fetchone()[0]
        d24 = con.execute("select count(*) from albums where decided_at>?", (now - 86400,)).fetchone()[0]
        con.close()
        return {
            "imported": c.get("imported", 0), "review": c.get("review", 0),
            "duplicate": c.get("duplicate", 0), "failed": c.get("failed", 0),
            "decided_total": sum(c.values()),
            "decided_15m": d15, "decided_60m": d60, "decided_24h": d24,
            "last_decision_utc": (datetime.fromtimestamp(last, timezone.utc).isoformat() if last else None),
            "mins_since_last_decision": (round((time.time() - last) / 60, 1) if last else None),
        }
    except Exception as e:  # never let a DB hiccup break the schedule
        return {"error": repr(e)}


def _log_tail(name):
    try:
        p = os.path.join(LOGS, name)
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            last = ""
            for last in f:
                pass
        mtime = os.path.getmtime(p)
        mins = round((time.time() - mtime) / 60, 1)
        return last.strip(), mins
    except Exception:
        return None, None


def _alive(lock):  # a held single-instance lock => that daemon is running
    try:
        import msvcrt
        fh = open(os.path.join(PROJ, lock), "a+")
        try:
            fh.seek(0)   # lock byte 0 (where the holder locks) so the probe is reliable
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            return False
        except OSError:
            return True
        finally:
            fh.close()
    except Exception:
        return None


def main():
    now = datetime.now(timezone.utc)
    s = _db()
    pw_line, pw_age = _log_tail("picardwatch.log")
    en_line, en_age = _log_tail("enrich.log")
    health = {
        "supervisor_running": _alive("picardwatch-supervisor.lock"),
        "watcher_running": _alive("picardwatch.lock"),
        "enrich_running": _alive("picardwatch-enrich.lock"),
        "watcher_log_age_min": pw_age,
        "enrich_log_age_min": en_age,
        "watcher_active": (pw_age is not None and pw_age < 30),
        "enrich_active": (en_age is not None and en_age < 60),
    }
    status = {"generatedAtUTC": now.isoformat(), "scanner": s, "health": health,
              "current_watcher_log": pw_line, "current_enrich_log": en_line}
    try:
        with open(os.path.join(PROJ, "scanner-status.json"), "w", encoding="utf-8") as f:
            json.dump(status, f, indent=2)
    except Exception:
        pass

    L = ["# PicardWatch — scanner status", "",
         f"_Auto-updated {now.strftime('%Y-%m-%d %H:%M UTC')} (every ~15 min). Machine-readable: scanner-status.json_", ""]
    if "error" in s:
        L += [f"> DB read error: `{s['error']}`", ""]
    else:
        L += ["## Library (cumulative decisions)",
              f"| Imported | Review | Duplicate | Failed | Total |",
              f"|---:|---:|---:|---:|---:|",
              f"| **{s['imported']}** | {s['review']} | {s['duplicate']} | {s['failed']} | {s['decided_total']} |",
              "",
              "## Pace",
              f"- Decided — last 15 min: **{s['decided_15m']}**, last hour: **{s['decided_60m']}**, last 24h: **{s['decided_24h']}**",
              f"- Last decision: {s['last_decision_utc']} ({s['mins_since_last_decision']} min ago)", ""]
    L += ["## Daemons",
          f"- supervisor: {_fmt(health['supervisor_running'])} · watcher: {_fmt(health['watcher_running'])} · enrich: {_fmt(health['enrich_running'])}",
          f"- watcher log age: {pw_age} min ({'ACTIVE' if health['watcher_active'] else 'stale/idle'}) · enrich log age: {en_age} min ({'ACTIVE' if health['enrich_active'] else 'stale/idle'})",
          "", "## Currently",
          f"- watcher: `{pw_line or '—'}`",
          f"- enrich: `{en_line or '—'}`", ""]
    try:
        with open(os.path.join(PROJ, "STATUS.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(L) + "\n")
    except Exception:
        pass

    # commit + push (best-effort, time-limited so the schedule never wedges)
    try:
        subprocess.run(["git", "-C", PROJ, "add", "STATUS.md", "scanner-status.json"],
                       capture_output=True, text=True, timeout=30)
        subprocess.run(["git", "-C", PROJ, "commit", "-m", f"status: {now.strftime('%Y-%m-%d %H:%M UTC')}"],
                       capture_output=True, text=True, timeout=30)
        p = subprocess.run(["git", "-C", PROJ, "push"], capture_output=True, text=True, timeout=120)
        with open(os.path.join(LOGS, "status-publish.log"), "a", encoding="utf-8") as f:
            f.write(f"{now.isoformat()} push rc={p.returncode} {(p.stderr or '').strip()[:160]}\n")
    except Exception as e:
        try:
            with open(os.path.join(LOGS, "status-publish.log"), "a", encoding="utf-8") as f:
                f.write(f"{now.isoformat()} publish error {e!r}\n")
        except Exception:
            pass


def _fmt(v):
    return "UP" if v else ("DOWN" if v is False else "?")


if __name__ == "__main__":
    main()
