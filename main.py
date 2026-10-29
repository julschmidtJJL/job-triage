import argparse
import json
import logging
import os
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from job_board import board_label, fetch_decisions_from_env, sync_auto_dropped, sync_jobs
from notion_sync.feedback import PIPELINE
from scrapers.arbeitsagentur import scrape_arbeitsagentur
from scrapers.digital_rapids import scrape_digital_rapids
from scrapers.good_jobs import scrape_good_jobs
from scrapers.google_jobs import scrape_google_jobs
from scrapers.green_jobs import scrape_green_jobs
from scrapers.hamburg_karriere import scrape_hamburg_karriere
from utils.feedback_features import build_feature_index
from utils.holdover import load_holdover, merge_with_fresh, save_holdover, split_top_n_and_holdover, top_n_from_env
from utils.pipeline import deduplicate, min_notion_score, score_jobs, score_threshold, scoring_model, _deduplicate_cross_platform
from utils.profile import active_queries, load_profile_text, load_search_config
from utils.query_stats import (
    format_scoreboard, load_stats, record_run, save_stats, scoreboard, stale_queries, suggest_queries, tag_jobs,
)
from utils.mailer import mail_configured, send_mail
from utils.run_summary import build_summary, write_run_summary
from utils.weekly_report import build_weekly_report
from utils.text_normalize import clean_title
from utils.stoplist import apply_rescue, apply_stoplist, harden_stoplist, is_shadow_mode, load_stoplist, save_stoplist

DATA_DIR = Path(__file__).parent / "data"
LOGS_DIR = Path(__file__).parent / "logs"

REMOTE_WORDS = ["remote", "homeoffice", "home office", "hybrid"]

HAMBURG_AREA = [
    "hamburg", "altona", "wandsbek", "harburg", "bergedorf", "eimsbüttel",
    "pinneberg", "stade", "buxtehude", "reinbek", "glinde", "ahrensburg",
    "norderstedt", "geesthacht", "lüneburg",
] + REMOTE_WORDS

GERMAN_PLACES = [
    "deutschland", "germany",
    "hamburg", "berlin", "münchen", "munich", "frankfurt",
    "köln", "cologne", "düsseldorf", "stuttgart", "leipzig",
    "bremen", "hannover", "dortmund", "essen", "nürnberg",
    "lübeck", "kiel", "schwerin", "lüneburg", "heide",
    "schleswig", "flensburg", "rostock", "pinneberg",
    "stade", "buxtehude", "harburg", "altona", "wandsbek",
]
GERMANY = GERMAN_PLACES + REMOTE_WORDS

# Real places only: a remote job must name a DACH country or city. "Remote" alone
# or "European Union (Remote)" says nothing about where the employer hires.
DACH_PLACES = GERMAN_PLACES + [
    "österreich", "austria", "wien", "vienna", "graz", "linz",
    "salzburg", "innsbruck", "klagenfurt", "villach", "wels", "st. pölten",
    "schweiz", "switzerland", "zürich", "zurich", "bern", "basel",
    "genf", "geneva", "lausanne", "winterthur", "luzern", "lucerne",
    "dach",
]

_AGGREGATOR_COMPANIES = {"xing", "xing jobs", "linkedin", "stepstone", "indeed", "google jobs"}
_AGGREGATOR_TITLE_PATTERNS = ("jobs & stellen", "stellenangebote - ", " jobs in ", " stellen in ")


def _matches(location: str, places: list[str]) -> bool:
    loc = location.lower().strip()
    if not loc:
        return True  # no location = don't filter out
    return any(place in loc for place in places)


def is_hamburg_location(location: str) -> bool:
    return _matches(location, HAMBURG_AREA)


def is_german_location(location: str) -> bool:
    return _matches(location, GERMANY)


def is_dach_location(location: str) -> bool:
    """True only if the location names a DACH country or city. Unlike the
    other checks, an empty location or a bare "Remote" does not count."""
    loc = location.lower().strip()
    return any(place in loc for place in DACH_PLACES)


def keep_location(job: dict) -> bool:
    """Hamburg area, or remote within DACH. Sources that already searched a
    radius around Hamburg set job["within_radius"]. Jobs from a remote-only
    search (job["remote"] is set) carry no location constraint from the search
    itself, so they must name a DACH place to stay."""
    location = job.get("location", "")
    if job.get("within_radius"):
        return True
    if job.get("remote"):
        return is_dach_location(location)
    return is_hamburg_location(location)


class PipelineFailed(RuntimeError):
    """Raised at the end of a run that scraped but could not score or deliver."""


def _is_aggregator_page(job: dict) -> bool:
    title = job.get("title", "").lower()
    company = job.get("company", "").lower().strip()
    if company in _AGGREGATOR_COMPANIES:
        return True
    return any(pat in title for pat in _AGGREGATOR_TITLE_PATTERNS)


def _log_stoplist_drops(drop_log: list, today: date) -> None:
    """Append one JSONL line per dropped (or would-have-dropped) job to
    logs/stoplist_drops_YYYYMMDD.jsonl, so a week of drops can be audited with
    `tail logs/stoplist_drops_*.jsonl | jq .`.
    """
    LOGS_DIR.mkdir(exist_ok=True)
    out = LOGS_DIR / f"stoplist_drops_{today.isoformat().replace('-', '')}.jsonl"
    with out.open("a", encoding="utf-8") as f:
        for job in drop_log:
            f.write(json.dumps({
                "ts": date.today().isoformat(),
                "url": job.get("url", ""),
                "title": job.get("title", ""),
                "company": job.get("company", ""),
                "source": job.get("source", ""),
                "query": job.get("query", ""),
                "stop_reason": job.get("stop_reason", ""),
                "stop_token": job.get("stop_token", ""),
                "stop_pipeline": job.get("stop_pipeline", ""),
            }, ensure_ascii=False) + "\n")


def _send_weekly_report(stats: dict, history, config: dict, stale: list, suggested: list, dry_run: bool) -> None:
    """At most one report per day (a manual re-run on a Monday must not mail
    twice). A mail failure is a warning, never a failed run: the jobs already
    reached Notion, and a missing Monday email is noticed anyway."""
    today = date.today().isoformat()
    if stats.get("last_report") == today:
        print("[REPORT] Already sent today, skipped")
        return
    subject, text, html_page = build_weekly_report(stats, history, config["queries"], stale, suggested)
    if dry_run or not mail_configured():
        print(f"[REPORT] Not sent ({'dry run' if dry_run else 'MAIL_USER / MAIL_APP_PASSWORD not set'}): {subject}")
        return
    sent, info = send_mail(subject, text, html_page)
    if sent:
        stats["last_report"] = today
        save_stats(stats)
        print(f"[REPORT] Weekly report {info}")
    else:
        print(f"[WARN] Weekly report not sent: {info}")


def run_failures(scored_jobs: list, profile_text: str, threshold: int, sync_result: dict | None) -> list[str]:
    """Reasons this run must count as failed. A run that scraped fine but
    could not score or deliver anything is not a success: it would mark every
    job as seen without you ever seeing it. A failed run exits non-zero, and
    the workflow then does not save state, so the next run retries those jobs."""
    reasons = []
    tier2 = [j for j in scored_jobs if j.get("prefilter_score", 0) >= threshold]
    if profile_text and tier2 and not any(j.get("scoring_method") == "claude" for j in scored_jobs):
        reasons.append(f"Claude scored none of the {len(tier2)} jobs that passed the prefilter")
    if sync_result and sync_result.get("failed") and not sync_result.get("added"):
        reasons.append(f"{board_label()} rejected all {sync_result['failed']} uploads")
    return reasons


def _setup_logging() -> logging.Logger:
    LOGS_DIR.mkdir(exist_ok=True)
    log_file = LOGS_DIR / f"run_{date.today()}.log"
    logger = logging.getLogger("job_hunt")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S"))
        logger.addHandler(fh)
    return logger


def _scrape_all(queries: list[str], log: logging.Logger) -> tuple[list, dict]:
    """Run every scraper. A failing scraper is logged and contributes nothing."""
    radius = int(os.getenv("JOB_RADIUS_KM", "50"))
    scrapers = [
        ("Google Jobs", lambda: scrape_google_jobs(queries, location="Hamburg, Deutschland", radius=radius)),
        ("Hamburg Karriere", lambda: scrape_hamburg_karriere(queries)),
        ("Good Jobs", lambda: scrape_good_jobs(queries, location="Hamburg", radius=radius)),
        ("Green Jobs", lambda: scrape_green_jobs(queries, location="Hamburg", radius=radius)),
        ("Digital Rapids", lambda: scrape_digital_rapids(queries)),
        # 30 km: Hamburg plus what is reachable by train without a car.
        ("Arbeitsagentur", lambda: scrape_arbeitsagentur(queries, location="Hamburg", radius=30)),
    ]
    jobs: list = []
    counts: dict = {}
    for name, run in scrapers:
        try:
            found = run()
        except Exception as e:
            print(f"[ERROR] {name} scraper: {e}")
            log.error("[ERROR] %s scraper: %s", name, e)
            found = []
        counts[name] = len(found)
        jobs += found
    return jobs, counts


def run_pipeline(dry_run: bool = False):
    log = _setup_logging()
    load_dotenv(Path(__file__).parent / ".env")
    log.info("[START] Pipeline started (dry_run=%s)", dry_run)

    config = load_search_config()
    queries = active_queries(config)
    if not queries:
        print("[ERROR] No active queries in profile/queries.json.")
        log.error("[ERROR] No active queries in profile/queries.json.")
        return
    profile_text = load_profile_text()
    if not profile_text:
        print("[WARN] No fit profile found (FIT_PROFILE or profile/profile.md) — scoring falls back to role terms")

    print(f"[INFO] Starting pipeline with {len(queries)} queries...")
    log.info("[INFO] %d queries loaded", len(queries))

    raw_jobs, per_source = _scrape_all(queries, log)
    for job in raw_jobs:
        job["title"] = clean_title(job.get("title", ""))
    tag_jobs(raw_jobs, queries)

    located = [j for j in raw_jobs if keep_location(j)]
    removed_location = len(raw_jobs) - len(located)
    all_jobs = [j for j in located if not _is_aggregator_page(j)]
    removed_aggregators = len(located) - len(all_jobs)

    # ============================================================
    # Per-run order
    # ============================================================
    # Rescues are applied before the stop-list so a just-rescued token is
    # already allowed when the stop-list runs; hardening comes last.

    # Step 1: load stoplist (auto-recovers from JSON corruption)
    stoplist = load_stoplist()
    # Step 2: read decisions from the board (90d lookback; Notion falls back to a stale cache)
    history = fetch_decisions_from_env()
    log.info(
        "[FEEDBACK] %d decisions loaded (applied=%d interested=%d rejected=%d declined=%d rescued=%d source=%s)",
        history.total(), len(history.applied), len(history.interested),
        len(history.rejected), len(history.declined),
        len(history.auto_dropped_rescued), history.source,
    )
    # Step 3: apply rescues (mutate allow_title_tokens)
    stoplist = apply_rescue(history, stoplist)
    # Step 4: persist with rescues
    save_stoplist(stoplist)
    # Step 5: build the feature index
    features = build_feature_index(history, PIPELINE)

    # Step 6: dedup fresh jobs against earlier runs, THEN add the holdover.
    # Held jobs were seen (and cached) in the run that held them back, so
    # merging them before deduplicate() would throw every one of them away.
    all_jobs = _deduplicate_cross_platform(all_jobs)
    fresh_new_jobs = deduplicate(all_jobs)
    new_jobs = fresh_new_jobs
    held = load_holdover()
    if held:
        print(f"[INFO] Holdover queue: re-scoring {len(held)} jobs from earlier runs")
        log.info("[FEEDBACK] holdover merge: %d jobs", len(held))
        new_jobs = merge_with_fresh(held, fresh_new_jobs)

    # Step 7: apply stop-list (shadow-aware)
    kept_jobs, drop_log = apply_stoplist(new_jobs, stoplist)
    shadow = is_shadow_mode(stoplist)
    if drop_log:
        mode_label = "SHADOW (would-have-dropped)" if shadow else "ENFORCE (dropped)"
        print(f"[STOPLIST] {mode_label}: {len(drop_log)} jobs")
        log.info("[FEEDBACK] stoplist %s: %d jobs", mode_label, len(drop_log))
        _log_stoplist_drops(drop_log, date.today())

    # Step 8: score (role-term prefilter + feedback deltas + Claude against the profile)
    scored_jobs = score_jobs(kept_jobs, features, role_terms=config["role_terms"], profile_text=profile_text)

    # Step 9: minimum score, then top-N cap + holdover update. Jobs below the
    # minimum are dropped here; they would only fill the board (or the queue).
    min_score = min_notion_score()
    eligible = [j for j in scored_jobs if j.get("relevance_score", 0) >= min_score]
    print(f"[CAP] {len(scored_jobs) - len(eligible)} jobs below the minimum score of {min_score} dropped")
    top_n = top_n_from_env()
    top_jobs, new_holdover, expired = split_top_n_and_holdover(eligible, top_n=top_n)
    save_holdover(new_holdover)
    if expired:
        print(f"[INFO] {len(expired)} jobs dropped after 3 runs without making the top-N cut")
        log.info("[FEEDBACK] holdover expired: %d jobs", len(expired))
    print(f"[CAP] Top-N={top_n}: {len(top_jobs)} jobs for the board, {len(new_holdover)} in holdover")

    output_file = DATA_DIR / f"new_jobs_{date.today()}.json"
    DATA_DIR.mkdir(exist_ok=True)
    output_file.write_text(json.dumps(scored_jobs, ensure_ascii=False, indent=2), encoding="utf-8")

    top = scored_jobs[0] if scored_jobs else None
    top_str = f"\"{top['title']}\" @ \"{top['company']}\" (score: {top['relevance_score']})" if top else "—"
    print(f"\n[STATS] Scraped: {len(raw_jobs)} | " + " | ".join(f"{k}: {v}" for k, v in per_source.items()))
    print(f"   Removed: {removed_location} outside Hamburg/remote DACH, {removed_aggregators} aggregator pages")
    print(f"[NEW] New (deduplicated): {len(new_jobs)} | [TOP] Top match: {top_str}")
    print(f"[INFO] Saved: {output_file}")

    auto_drop_added = 0
    sync_added = 0
    sync_result = None
    target = board_label()
    if dry_run:
        print(f"[DRY-RUN] {target} upload skipped")
        log.info("[INFO] Dry run: %s upload skipped", target)
    else:
        # Step 10: sync top-N to the board as New
        sync_result = sync_jobs(top_jobs)
        sync_added = sync_result["added"]
        print(f"[SYNC] {target}: {sync_result['added']} added, {sync_result['skipped']} skipped (duplicates)")
        if sync_result.get("failed_jobs"):
            # Failed uploads go back into the holdover queue so the next run
            # retries them; they count toward the same 3-run expiry.
            retry = [{**j, "holdover_runs": j.get("holdover_runs", 0) + 1} for j in sync_result["failed_jobs"]]
            new_holdover = retry + new_holdover
            save_holdover(new_holdover)
            print(f"[SYNC] {len(retry)} failed uploads moved to the holdover queue for the next run")
        log.info("[OK] %s sync: %d added, %d skipped", target, sync_result["added"], sync_result["skipped"])
        # Step 11: sync auto-dropped (NOT in shadow mode — only real drops)
        if not shadow and drop_log:
            auto_drop_added = sync_auto_dropped(drop_log)["added"]
            print(f"[AUTO-DROP] {auto_drop_added} Auto-Dropped rows created in {target}")
            log.info("[FEEDBACK] auto-dropped synced: %d", auto_drop_added)

    # Step 12: harden stop-list (respects the allow-list from step 3, so a
    # just-rescued token cannot re-harden in the same run)
    stoplist = harden_stoplist(features, stoplist)
    save_stoplist(stoplist)

    # Step 13: query scoreboard — the experiment's readout
    stats = record_run(
        load_stats(), raw_jobs=raw_jobs, new_jobs=fresh_new_jobs, scored_jobs=scored_jobs,
        threshold=score_threshold(),
    )
    save_stats(stats)
    board = scoreboard(stats, history, config["queries"])
    stale = stale_queries(board)
    print("\n[QUERIES] Last 28 days (found / new / passed prefilter / avg score / positives on the board)")
    print(format_scoreboard(board))
    if stale:
        print(f"[QUERIES] No yield after 21+ days, consider turning off: {', '.join(stale)}")

    suggested: list = []
    report_day = date.today().weekday() == 0 or os.getenv("SEND_REPORT", "").lower() == "true"
    if report_day:  # Mondays, or on request
        liked = [d.title for d in list(history.applied) + list(history.interested)]
        liked += [j["title"] for j in scored_jobs if j.get("relevance_score", 0) >= 70]
        suggested = suggest_queries(liked, [q["q"] for q in config["queries"]], model=scoring_model())
        if suggested:
            print(f"[QUERIES] Suggested new queries (add to profile/queries.json if you like them): {', '.join(suggested)}")
        _send_weekly_report(stats, history, config, stale, suggested, dry_run)

    failures = run_failures(scored_jobs, profile_text, score_threshold(), sync_result)

    # Step 14: per-run observability summary
    summary = build_summary(
        history=history,
        features=features,
        stoplist=stoplist,
        jobs_in=len(new_jobs),
        jobs_dropped=len(drop_log),
        jobs_synced_to_notion=sync_added,
        holdover_size=len(new_holdover),
        auto_dropped_synced=auto_drop_added,
        query_scoreboard=board,
        stale_queries=stale,
        suggested_queries=suggested,
        errors=failures,
    )
    summary_path = write_run_summary(summary)
    log.info("[FEEDBACK] run summary written: %s", summary_path)
    log.info("[END] Pipeline done: %d total, %d new, %d scored", len(all_jobs), len(new_jobs), len(scored_jobs))
    if failures and not dry_run:
        for reason in failures:
            print(f"[FAILED] {reason}")
            log.error("[FAILED] %s", reason)
        raise PipelineFailed("; ".join(failures))
    return scored_jobs


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Job hunt pipeline")
    parser.add_argument("--dry-run", action="store_true", help="Run the pipeline without writing to the board")
    args = parser.parse_args()
    try:
        run_pipeline(dry_run=args.dry_run)
    except PipelineFailed:
        sys.exit(1)
