"""Per-run observability: one structured JSONL line per cron run.

Critical for debugging silent learning failures. Cron runs at 9 AM with nobody
watching; if the Notion query 429s, the stop-list corrupts, or the feature
extractor returns empty Counters because of a schema change, the only sign
something broke is seeing identical-feeling lists day after day.

`tail logs/feedback_run_*.jsonl | jq .` instantly shows what each run did.
"""
import json
from datetime import date, datetime, timezone
from pathlib import Path

LOGS_DIR = Path(__file__).parent.parent / "logs"


def write_run_summary(summary: dict, today: date | None = None) -> Path:
    """Append one JSONL line to logs/feedback_run_YYYYMMDD.jsonl.

    `summary` is dict-shaped per the design Part 6 schema:
      ts, decisions_loaded, feature_index, stoplist, jobs_in,
      jobs_dropped_by_stoplist, jobs_synced_to_notion, holdover_size, errors.
    Caller fills these in. `ts` is auto-added if missing.
    """
    LOGS_DIR.mkdir(exist_ok=True)
    today = today or date.today()
    out_file = LOGS_DIR / f"feedback_run_{today.isoformat().replace('-', '')}.jsonl"

    if "ts" not in summary:
        summary["ts"] = datetime.now(timezone.utc).isoformat()

    line = json.dumps(summary, ensure_ascii=False, default=str)
    with out_file.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    return out_file


def build_summary(
    *,
    history,
    features,
    stoplist,
    jobs_in: int,
    jobs_dropped: int,
    jobs_synced_to_notion: int,
    holdover_size: int,
    auto_dropped_synced: int = 0,
    query_scoreboard: list | None = None,
    stale_queries: list | None = None,
    suggested_queries: list | None = None,
    errors: list | None = None,
) -> dict:
    """Helper: assemble the summary dict from the per-run state."""
    from notion_sync.feedback import PIPELINE
    from utils.stoplist import is_shadow_mode

    state = stoplist.get(PIPELINE, {})
    return {
        "decisions_loaded": {
            "applied": len(history.applied),
            "interested": len(history.interested),
            "rejected": len(history.rejected),
            "declined": len(history.declined),
            "auto_dropped_rescued": len(history.auto_dropped_rescued),
            "source": history.source,
        },
        "feature_index": features.summary() if features else {},
        "stoplist": {
            "size": len(state.get("title_tokens", [])),
            "companies": len(state.get("companies", [])),
            "allow_list": len(state.get("manual_overrides", {}).get("allow_title_tokens", [])),
            "shadow_mode": is_shadow_mode(stoplist),
            "launched_at": stoplist.get("launched_at"),
        },
        "jobs_in": jobs_in,
        "jobs_dropped_by_stoplist": jobs_dropped,
        "jobs_synced_to_notion": jobs_synced_to_notion,
        "auto_dropped_synced": auto_dropped_synced,
        "holdover_size": holdover_size,
        "query_scoreboard": query_scoreboard or [],
        "stale_queries": stale_queries or [],
        "suggested_queries": suggested_queries or [],
        "errors": errors or [],
    }
