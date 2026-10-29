"""The Monday email: the query scoreboard plus what you did with the board.

Built from the same numbers the run prints, so the email and the log never
disagree. Plain text and a small HTML table; no external assets.
"""
import html
import os
from datetime import date, datetime, timedelta, timezone

from utils.query_stats import UNATTRIBUTED, scoreboard

_COLUMNS = [("found", "found"), ("new", "new"), ("passed", "passed"), ("avg_score", "avg"), ("positives", "+")]


def _rows(board: list[dict]) -> list[dict]:
    return [r for r in board if not (r["query"] == UNATTRIBUTED and not r["found"])]


def _decisions_this_week(history, today: date) -> dict:
    if history is None:
        return {}
    since = datetime.combine(today - timedelta(days=7), datetime.min.time(), tzinfo=timezone.utc)

    def recent(ds):
        return [d for d in ds if d.decided_at.astimezone(timezone.utc) >= since]

    return {
        # "Apply" is the to-do list, "Applied" means sent; both are positive
        # signal for learning, but only one is an application.
        "applied": len([d for d in recent(history.applied) if d.decision == "Applied"]),
        "on the apply list": len([d for d in recent(history.applied) if d.decision == "Apply"]),
        "interesting": len(recent(history.interested)),
        "maybe": len(recent(getattr(history, "maybe", []))),
        "not interesting": len(recent(history.rejected)),
        "declined": len(recent(history.declined)),
    }


def _text_table(rows: list[dict]) -> str:
    head = f"{'query':<30} {'found':>6} {'new':>5} {'passed':>6} {'avg':>4} {'+':>3}"
    lines = [head, "-" * len(head)]
    for r in rows:
        avg = "-" if r["avg_score"] is None else str(r["avg_score"])
        name = r["query"] if r["active"] else f"{r['query']} (off)"
        lines.append(f"{name[:30]:<30} {r['found']:>6} {r['new']:>5} {r['passed']:>6} {avg:>4} {r['positives']:>3}")
    return "\n".join(lines)


def _html_table(rows: list[dict]) -> str:
    th = "".join(f"<th>{label}</th>" for _, label in _COLUMNS)
    body = []
    for r in rows:
        name = html.escape(r["query"]) + ("" if r["active"] else " <span class='off'>(off)</span>")
        cells = "".join(
            f"<td class='n'>{'-' if r[key] is None else r[key]}</td>" for key, _ in _COLUMNS
        )
        body.append(f"<tr><td>{name}</td>{cells}</tr>")
    return f"<table><tr><th>query</th>{th}</tr>{''.join(body)}</table>"


def build_weekly_report(
    stats: dict,
    history,
    queries: list[dict],
    stale: list[str],
    suggested: list[str],
    today: date | None = None,
) -> tuple[str, str, str]:
    """Returns (subject, plain_text, html)."""
    today = today or date.today()
    week = _rows(scoreboard(stats, history, queries, today=today, window_days=7))
    month = _rows(scoreboard(stats, history, queries, today=today, window_days=28))
    decisions = _decisions_this_week(history, today)
    found_week = sum(r["found"] for r in week)
    passed_week = sum(r["passed"] for r in week)

    subject = f"jobhunt week {today.isocalendar().week}: {passed_week} jobs past the filter, {decisions.get('applied', 0)} applied"

    db_id = os.environ.get("NOTION_JOB_BOARD_DB_ID", "").replace("-", "").strip()
    board_url = f"https://www.notion.so/{db_id}" if db_id else ""

    decision_line = ", ".join(f"{v} {k}" for k, v in decisions.items()) if decisions else "none on the board"
    text = [
        f"jobhunt weekly report, {today.isoformat()}",
        "",
        f"Last 7 days: {found_week} jobs found, {passed_week} past the prefilter.",
        f"Your decisions this week: {decision_line}.",
        "",
        "Queries, last 7 days (found / new / passed prefilter / avg score / positives):",
        _text_table(week),
        "",
        "Queries, last 28 days:",
        _text_table(month),
    ]
    if stale:
        text += ["", f"No yield after 21+ days, consider turning off: {', '.join(stale)}"]
    if suggested:
        text += ["", f"Suggested new queries: {', '.join(suggested)}",
                 "Add the ones you like to profile/queries.json."]
    if board_url:
        text += ["", f"Board: {board_url}"]
    plain = "\n".join(text) + "\n"

    parts = [
        f"<p>Last 7 days: <b>{found_week}</b> jobs found, <b>{passed_week}</b> past the prefilter.<br>"
        f"Your decisions this week: {html.escape(decision_line)}.</p>",
        "<h3>Queries, last 7 days</h3>", _html_table(week),
        "<h3>Queries, last 28 days</h3>", _html_table(month),
        "<p class='note'>found / new / passed prefilter / average score / positives on the board</p>",
    ]
    if stale:
        parts.append(f"<p><b>No yield after 21+ days</b>, consider turning off: {html.escape(', '.join(stale))}</p>")
    if suggested:
        parts.append(f"<p><b>Suggested new queries:</b> {html.escape(', '.join(suggested))}<br>"
                     "Add the ones you like to <code>profile/queries.json</code>.</p>")
    if board_url:
        parts.append(f"<p><a href='{board_url}'>Open the Job Board</a></p>")
    css = ("body{font-family:-apple-system,Helvetica,Arial,sans-serif;color:#1a1a1a;max-width:640px}"
           "table{border-collapse:collapse;font-size:14px}th,td{padding:4px 10px;border-bottom:1px solid #e5e5e5;text-align:left}"
           "td.n{text-align:right;font-variant-numeric:tabular-nums}.off,.note{color:#888}.note{font-size:12px}a{color:#2540F5}")
    page = (f"<!doctype html><html><head><meta charset='utf-8'><style>{css}</style></head><body>"
            f"<h2>jobhunt weekly report</h2>{''.join(parts)}</body></html>")
    return subject, plain, page
