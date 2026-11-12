"""Aggregate HTML report for a regression run.

Renders a standalone, self-contained HTML file summarising every case: overall
pass/fail, each required check (sequence, category, root cause, KB article,
report, response time) with expected vs. actual values, and timing. Written at
the end of the pytest session by a conftest hook.
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path
from typing import List

from .runner import RegressionResult

_CSS = """
body{font-family:Segoe UI,Arial,sans-serif;margin:0;background:#f4f6f8;color:#1f2933}
header{background:#102a43;color:#fff;padding:20px 28px}
header h1{margin:0;font-size:22px}
.meta{color:#bcccdc;font-size:13px;margin-top:6px}
.summary{display:flex;gap:16px;padding:20px 28px;flex-wrap:wrap}
.card{background:#fff;border-radius:8px;padding:14px 20px;box-shadow:0 1px 3px rgba(0,0,0,.1);min-width:120px}
.card .n{font-size:26px;font-weight:700}
.pass{color:#0b7a3b}.fail{color:#b91c1c}
.case{background:#fff;margin:14px 28px;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,.1);overflow:hidden}
.case h2{margin:0;padding:12px 18px;font-size:16px;display:flex;justify-content:space-between;align-items:center}
.badge{font-size:12px;font-weight:700;padding:3px 10px;border-radius:12px;color:#fff}
.badge.pass{background:#0b7a3b}.badge.fail{background:#b91c1c}
.desc{padding:0 18px 10px;color:#52606d;font-size:13px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 18px;border-top:1px solid #e4e7eb;vertical-align:top}
th{background:#f0f4f8;color:#486581;font-weight:600}
td.status-pass{color:#0b7a3b;font-weight:600}
td.status-fail{color:#b91c1c;font-weight:600}
code{background:#f0f4f8;padding:1px 5px;border-radius:4px;font-size:12px;white-space:pre-wrap}
"""


def _fmt(value) -> str:
    if isinstance(value, (dict, list)):
        return html.escape(json.dumps(value, ensure_ascii=False))
    return html.escape(str(value))


def render_report(results: List[RegressionResult], output_path: Path) -> Path:
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = total - passed

    parts: List[str] = []
    parts.append("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>")
    parts.append("<title>Validation Agent — Regression Report</title>")
    parts.append(f"<style>{_CSS}</style></head><body>")
    parts.append("<header><h1>Validation Agent — Regression Report</h1>")
    parts.append(
        f"<div class='meta'>Generated {html.escape(datetime.now().isoformat(timespec='seconds'))} "
        f"&middot; {total} case(s)</div></header>"
    )

    parts.append("<div class='summary'>")
    parts.append(f"<div class='card'><div class='n'>{total}</div>Total</div>")
    parts.append(f"<div class='card'><div class='n pass'>{passed}</div>Passed</div>")
    parts.append(f"<div class='card'><div class='n fail'>{failed}</div>Failed</div>")
    parts.append("</div>")

    for r in results:
        badge = "pass" if r.passed else "fail"
        label = "PASS" if r.passed else "FAIL"
        parts.append("<div class='case'>")
        parts.append(
            f"<h2>{html.escape(r.case_id)} "
            f"<span class='badge {badge}'>{label}</span></h2>"
        )
        parts.append(f"<div class='desc'>{html.escape(r.description)} "
                     f"&middot; {r.wall_ms:.1f} ms</div>")
        parts.append("<table><thead><tr><th>Check</th><th>Result</th>"
                     "<th>Expected</th><th>Actual</th><th>Detail</th></tr></thead><tbody>")
        for c in r.checks:
            cls = "status-pass" if c.passed else "status-fail"
            res = "PASS" if c.passed else "FAIL"
            parts.append(
                f"<tr><td>{html.escape(c.name)}</td>"
                f"<td class='{cls}'>{res}</td>"
                f"<td><code>{_fmt(c.expected)}</code></td>"
                f"<td><code>{_fmt(c.actual)}</code></td>"
                f"<td>{html.escape(c.detail)}</td></tr>"
            )
        parts.append("</tbody></table></div>")

    parts.append("</body></html>")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("".join(parts), encoding="utf-8")
    return output_path
