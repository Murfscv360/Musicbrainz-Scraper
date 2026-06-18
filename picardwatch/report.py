"""Write an HTML review report of albums that were NOT imported (left in place)."""

from __future__ import annotations

import html
import logging
from pathlib import Path

log = logging.getLogger("picardwatch.report")

_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>PicardWatch — review queue</title>
<style>
 body{{font-family:Segoe UI,Arial,sans-serif;margin:2rem;color:#222}}
 h1{{font-size:1.3rem}} .meta{{color:#666;font-size:.85rem;margin-bottom:1rem}}
 table{{border-collapse:collapse;width:100%}}
 th,td{{border:1px solid #ddd;padding:.4rem .6rem;font-size:.85rem;text-align:left;vertical-align:top}}
 th{{background:#f4f4f4}} tr:nth-child(even){{background:#fafafa}}
 .status-failed{{color:#b00}} .reason{{color:#a40}}
 code{{background:#f0f0f0;padding:.05rem .3rem;border-radius:3px}}
</style></head><body>
<h1>PicardWatch — albums left in the input folder</h1>
<div class="meta">{count} album(s) need attention. Fix the folder contents and they
re-evaluate automatically on the next scan.</div>
<table>
 <tr><th>Folder</th><th>Status</th><th>Best guess</th><th>Why not imported</th>
     <th>Files / Tracks</th><th>Coverage</th></tr>
 {rows}
</table></body></html>
"""


def write(path: str | Path, reviews: list[dict]) -> None:
    rows = []
    for r in reviews:
        folder = html.escape(Path(r["folder"]).name)
        guess = html.escape(f'{r.get("artist") or "?"} — {r.get("title") or "?"}')
        reason = html.escape(r.get("reason") or "")
        cov = r.get("coverage")
        cov_s = f"{cov:.0%}" if isinstance(cov, (int, float)) else "—"
        status = html.escape(r.get("status") or "")
        rows.append(
            f'<tr><td><code>{folder}</code></td>'
            f'<td class="status-{status}">{status}</td>'
            f'<td>{guess}</td><td class="reason">{reason}</td>'
            f'<td>{r.get("file_count")} / {r.get("track_count")}</td>'
            f'<td>{cov_s}</td></tr>'
        )
    out = _TEMPLATE.format(count=len(reviews), rows="\n ".join(rows))
    Path(path).write_text(out, encoding="utf-8")
    log.debug("Wrote review report: %s (%d rows)", path, len(reviews))
