"""Holdover queue: jobs that scored well but didn't make the top-N daily cut.

Each entry carries a `holdover_runs` counter, incremented each run a job
does not get promoted. At 3, the entry is dropped (and logged) — bounds queue
growth so it doesn't accumulate forever.
"""
import json
import os
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
HOLDOVER_FILE = DATA_DIR / "holdover_queue.json"

TOP_N_DEFAULT = 30
TOP_N_ENV = "JOB_HUNT_TOP_N"
HOLDOVER_MAX_RUNS = 3


def top_n_from_env() -> int:
    raw = os.environ.get(TOP_N_ENV, "").strip()
    if not raw:
        return TOP_N_DEFAULT
    try:
        n = int(raw)
        return max(1, n)
    except ValueError:
        return TOP_N_DEFAULT


def load_holdover(path: Path = HOLDOVER_FILE) -> list:
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Holdover corruption is recoverable — just lose this batch
        print("[WARN] holdover_queue.json corrupt — resetting")
        return []
    if not isinstance(raw, list):
        return []
    return raw


def save_holdover(jobs: list, path: Path = HOLDOVER_FILE) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")


def split_top_n_and_holdover(
    scored: list,
    top_n: int | None = None,
) -> tuple[list, list, list]:
    """Given a relevance-sorted list, return:
      (top_n_jobs, new_holdover, dropped_after_max_runs)

    new_holdover contains both:
      - existing holdover entries that still didn't make top (run counter ++)
      - fresh entries that scored too low this run (run counter = 1)

    Entries whose holdover_runs would reach HOLDOVER_MAX_RUNS are surfaced
    in the third return value (caller logs them and excludes them).
    """
    if top_n is None:
        top_n = top_n_from_env()

    top = scored[:top_n]
    leftover = scored[top_n:]

    new_holdover: list = []
    expired: list = []
    for job in leftover:
        next_runs = job.get("holdover_runs", 0) + 1
        if next_runs >= HOLDOVER_MAX_RUNS:
            expired.append(job)
            continue
        out = dict(job)
        out["holdover_runs"] = next_runs
        new_holdover.append(out)

    return top, new_holdover, expired


def merge_with_fresh(holdover: list, fresh_jobs: list) -> list:
    """Combine previously-held jobs with freshly-scraped jobs for the next
    dedup/score cycle. Deduped by URL — fresh wins (newer scrape may have
    updated description/location)."""
    fresh_urls = {j.get("url") for j in fresh_jobs if j.get("url")}
    surviving_holdover = [j for j in holdover if j.get("url") and j.get("url") not in fresh_urls]
    return fresh_jobs + surviving_holdover
