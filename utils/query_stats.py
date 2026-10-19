"""Per-query scoreboard: which search queries actually find jobs worth applying to.

Queries are the experimental variable (profile/queries.json). Every job keeps
the query that found it; each run records, per query:

  found   — raw postings scraped
  new     — left after dedup against earlier runs
  passed  — cleared the Tier-1 prefilter into Claude scoring
  scored  — got a Claude score (score_sum / scored = average, 0-100)

Positives (Interesting / Apply / Applied in Notion) come from the decision
history, which already spans 90 days.

State lives in data/query_stats.json as one entry per run day, trimmed to
RETENTION_DAYS, and survives between GitHub Actions runs through
actions/cache, the same way jobs_seen_urls.json does.

On Mondays, suggest_queries() makes one cheap Claude call that proposes new
queries from the titles of jobs you liked. It only prints suggestions; you
edit queries.json yourself.
"""
import json
from datetime import date, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
STATS_FILE = DATA_DIR / "query_stats.json"
RETENTION_DAYS = 56
WINDOW_DAYS = 28
STALE_DAYS = 21
UNATTRIBUTED = "(unattributed)"

_COUNTERS = ("found", "new", "passed", "scored", "score_sum")


def attribute_query(job: dict, queries: list[str]) -> str:
    """The query that found this job. Scrapers that search per query set
    job["query"] themselves; for the rest (single fetch, filtered locally)
    the first query whose text appears in the title, then the description."""
    if job.get("query") in queries:
        return job["query"]
    title = job.get("title", "").lower()
    for q in queries:
        if q.lower() in title:
            return q
    description = job.get("description", "").lower()
    for q in queries:
        if q.lower() in description:
            return q
    return UNATTRIBUTED


def tag_jobs(jobs: list, queries: list[str]) -> None:
    for job in jobs:
        job["query"] = attribute_query(job, queries)


def load_stats(path: Path = STATS_FILE) -> dict:
    if not path.exists():
        return {"days": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print("[WARN] query_stats.json corrupt — starting fresh")
        return {"days": {}}
    raw.setdefault("days", {})
    return raw


def save_stats(stats: dict, path: Path = STATS_FILE, today: date | None = None) -> None:
    today = today or date.today()
    cutoff = (today - timedelta(days=RETENTION_DAYS)).isoformat()
    stats["days"] = {d: v for d, v in stats["days"].items() if d >= cutoff}
    DATA_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _bump(day: dict, query: str, field: str, by: int = 1) -> None:
    entry = day.setdefault(query, {c: 0 for c in _COUNTERS})
    entry[field] = entry.get(field, 0) + by


def record_run(
    stats: dict,
    *,
    raw_jobs: list,
    new_jobs: list,
    scored_jobs: list,
    threshold: int,
    today: date | None = None,
) -> dict:
    """Add this run's counts to today's entry. Several runs on one day add up."""
    today = today or date.today()
    day = stats["days"].setdefault(today.isoformat(), {})
    for job in raw_jobs:
        _bump(day, job.get("query", UNATTRIBUTED), "found")
    for job in new_jobs:
        _bump(day, job.get("query", UNATTRIBUTED), "new")
    for job in scored_jobs:
        q = job.get("query", UNATTRIBUTED)
        if job.get("prefilter_score", job.get("relevance_score", 0)) >= threshold:
            _bump(day, q, "passed")
        if job.get("scoring_method") == "claude":
            _bump(day, q, "scored")
            _bump(day, q, "score_sum", job.get("relevance_score", 0))
    return stats


def scoreboard(
    stats: dict,
    history,
    queries: list[dict],
    today: date | None = None,
    window_days: int = WINDOW_DAYS,
) -> list[dict]:
    """One row per configured query (active or not) plus unattributed, over
    the last `window_days`. Positives come from the Notion decision history."""
    today = today or date.today()
    cutoff = (today - timedelta(days=window_days)).isoformat()

    totals: dict[str, dict] = {}
    first_seen: dict[str, str] = {}
    for day, per_query in sorted(stats.get("days", {}).items()):
        for q, counts in per_query.items():
            first_seen.setdefault(q, day)
            if day < cutoff:
                continue
            t = totals.setdefault(q, {c: 0 for c in _COUNTERS})
            for c in _COUNTERS:
                t[c] += counts.get(c, 0)

    positives: dict[str, int] = {}
    if history is not None:
        for d in list(history.applied) + list(history.interested):
            if getattr(d, "query", ""):
                positives[d.query] = positives.get(d.query, 0) + 1

    names = [q["q"] for q in queries] + [UNATTRIBUTED]
    rows = []
    for name in names:
        t = totals.get(name, {c: 0 for c in _COUNTERS})
        active = next((q.get("active", True) for q in queries if q["q"] == name), True)
        rows.append({
            "query": name,
            "active": active,
            "found": t["found"],
            "new": t["new"],
            "passed": t["passed"],
            "avg_score": round(t["score_sum"] / t["scored"]) if t["scored"] else None,
            "positives": positives.get(name, 0),
            "first_seen": first_seen.get(name),
        })
    return rows


def stale_queries(rows: list[dict], today: date | None = None, stale_days: int = STALE_DAYS) -> list[str]:
    """Active queries that have run for at least `stale_days` and produced no
    job past the prefilter and no positive decision in the window."""
    today = today or date.today()
    out = []
    for r in rows:
        if r["query"] == UNATTRIBUTED or not r["active"] or not r["first_seen"]:
            continue
        age = (today - date.fromisoformat(r["first_seen"])).days
        if age >= stale_days and r["passed"] == 0 and r["positives"] == 0:
            out.append(r["query"])
    return out


def format_scoreboard(rows: list[dict]) -> str:
    header = f"{'query':<32} {'found':>6} {'new':>5} {'passed':>6} {'avg':>4} {'+':>3}"
    lines = [header, "-" * len(header)]
    for r in rows:
        if r["query"] == UNATTRIBUTED and not r["found"]:
            continue
        avg = "-" if r["avg_score"] is None else str(r["avg_score"])
        name = r["query"] if r["active"] else f"{r['query']} (off)"
        lines.append(
            f"{name[:32]:<32} {r['found']:>6} {r['new']:>5} {r['passed']:>6} {avg:>4} {r['positives']:>3}"
        )
    return "\n".join(lines)


def suggest_queries(
    liked_titles: list[str],
    current_queries: list[str],
    model: str,
    max_suggestions: int = 5,
) -> list[str]:
    """One Claude call: propose new search queries from titles you liked.
    Returns [] on any failure — suggestions are a convenience, never a blocker."""
    import os

    import anthropic
    from pydantic import BaseModel

    if not liked_titles or not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return []

    class Suggestions(BaseModel):
        queries: list[str]

    prompt = (
        "These are job titles a candidate liked or that scored high for them:\n"
        + "\n".join(f"- {t}" for t in liked_titles[:60])
        + "\n\nCurrent search queries:\n"
        + "\n".join(f"- {q}" for q in current_queries)
        + f"\n\nPropose up to {max_suggestions} NEW search queries (German or English, "
        "2-4 words each, the way people type them into a job board) that would find "
        "more postings like the liked ones. Do not repeat or trivially rephrase a "
        "current query."
    )
    try:
        client = anthropic.Anthropic()
        response = client.messages.parse(
            model=model,
            max_tokens=4000,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": prompt}],
            output_format=Suggestions,
        )
        parsed = response.parsed_output
        if parsed is None:
            return []
        existing = {q.lower() for q in current_queries}
        return [q for q in parsed.queries if q.lower() not in existing][:max_suggestions]
    except Exception as e:
        print(f"[WARN] Query suggestions failed: {e}")
        return []
