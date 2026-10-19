"""Notion feedback read-back: your Decision clicks → training signal for the next run.

Queries the Notion job-board DB for rows where you have set a Decision,
buckets them by signal type, and returns a DecisionHistory the rest of the
pipeline consumes.

Includes:
  - 90-day lookback — bounded query cost, implicit GC for stale preferences
  - Preflight schema check — bails clearly if the Auto-Dropped option or the
    Stop Reason property is missing instead of failing silently mid-run
  - Stale-cache fallback — if Notion is unreachable, the last good snapshot is used
  - auto_dropped_rescued bucket — rows that were Auto-Dropped, now Interesting
    (rescue signal: you un-blocked a token by changing the verdict)
"""
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from notion_client import APIResponseError, Client

DATA_DIR = Path(__file__).parent.parent / "data"
DECISION_CACHE_FILE = DATA_DIR / "decision_history.json"
LOOKBACK_DAYS_DEFAULT = 90

# One pipeline. The field stays in the data model so the stop-list and feature
# index keep their per-pipeline shape if a second pipeline is ever added.
PIPELINE = "main"

# Notion Decision values
DECISION_NEW = "New"
DECISION_INTERESTING = "Interesting"
DECISION_MAYBE = "Maybe"  # "better than nothing": recorded, but no training signal
DECISION_APPLY = "Apply"
DECISION_APPLIED = "Applied"
DECISION_NOT_INTERESTING = "Not interesting"
DECISION_DECLINED = "Declined"
DECISION_AUTO_DROPPED = "Auto-Dropped"


class ConfigError(RuntimeError):
    """Raised when the Notion DB schema is missing required properties."""


@dataclass
class Decision:
    title: str
    company: str
    decision: str  # Notion Decision value
    decided_at: datetime  # last_edited_time
    pipeline: str  # always PIPELINE today
    url: str = ""  # for rescue lookup in jobs_seen_urls.json sidecar
    stop_reason: str = ""  # populated for rescued rows
    query: str = ""  # search query that found the job (Query property)


@dataclass
class DecisionHistory:
    applied: list[Decision] = field(default_factory=list)
    interested: list[Decision] = field(default_factory=list)
    rejected: list[Decision] = field(default_factory=list)
    declined: list[Decision] = field(default_factory=list)
    auto_dropped_rescued: list[Decision] = field(default_factory=list)
    maybe: list[Decision] = field(default_factory=list)  # neutral, see DECISION_MAYBE
    source: str = "notion"  # "notion" or "stale_cache"

    def total(self) -> int:
        return (
            len(self.applied) + len(self.interested) + len(self.rejected)
            + len(self.declined) + len(self.auto_dropped_rescued)
        )

    def to_dict(self) -> dict:
        return {
            "applied": [_decision_to_dict(d) for d in self.applied],
            "interested": [_decision_to_dict(d) for d in self.interested],
            "rejected": [_decision_to_dict(d) for d in self.rejected],
            "declined": [_decision_to_dict(d) for d in self.declined],
            "auto_dropped_rescued": [_decision_to_dict(d) for d in self.auto_dropped_rescued],
            "maybe": [_decision_to_dict(d) for d in self.maybe],
        }

    @classmethod
    def from_dict(cls, raw: dict, source: str = "stale_cache") -> "DecisionHistory":
        return cls(
            applied=[_decision_from_dict(d) for d in raw.get("applied", [])],
            interested=[_decision_from_dict(d) for d in raw.get("interested", [])],
            rejected=[_decision_from_dict(d) for d in raw.get("rejected", [])],
            declined=[_decision_from_dict(d) for d in raw.get("declined", [])],
            auto_dropped_rescued=[_decision_from_dict(d) for d in raw.get("auto_dropped_rescued", [])],
            maybe=[_decision_from_dict(d) for d in raw.get("maybe", [])],
            source=source,
        )


def _decision_to_dict(d: Decision) -> dict:
    return {
        "title": d.title, "company": d.company, "decision": d.decision,
        "decided_at": d.decided_at.isoformat(), "pipeline": d.pipeline,
        "url": d.url, "stop_reason": d.stop_reason, "query": d.query,
    }


def _decision_from_dict(raw: dict) -> Decision:
    return Decision(
        title=raw.get("title", ""), company=raw.get("company", ""),
        decision=raw.get("decision", ""),
        decided_at=datetime.fromisoformat(raw["decided_at"]),
        pipeline=raw.get("pipeline", PIPELINE),
        url=raw.get("url", ""), stop_reason=raw.get("stop_reason", ""),
        query=raw.get("query", ""),
    )


def derive_pipeline(source: str) -> str:
    """Map a job's source key to its pipeline. There is one pipeline."""
    return PIPELINE


def preflight_schema(notion: Client, ds_id: str) -> None:
    """Verify the Notion DB has the schema we need. Raise ConfigError otherwise.

    Required:
      - Decision select with options including 'Auto-Dropped'
      - Stop Reason rich_text property

    Bails early so a missing schema element does not become a silent mid-run
    failure when the first auto-drop tries to sync.
    """
    try:
        ds = notion.data_sources.retrieve(ds_id)
    except APIResponseError as e:
        raise ConfigError(f"Notion data source retrieve failed: {e}") from e

    properties = ds.get("properties", {})

    decision = properties.get("Decision", {})
    if decision.get("type") != "select":
        raise ConfigError(
            "Notion DB is missing the 'Decision' select property. See README → Notion setup."
        )
    options = [o.get("name") for o in decision.get("select", {}).get("options", [])]
    if DECISION_AUTO_DROPPED not in options:
        raise ConfigError(
            "Notion Decision select is missing the 'Auto-Dropped' option. See README → Notion setup."
        )

    stop_reason = properties.get("Stop Reason", {})
    if stop_reason.get("type") != "rich_text":
        raise ConfigError(
            "Notion DB is missing the 'Stop Reason' rich_text property. See README → Notion setup."
        )


def _extract_text(rich_text_prop: dict) -> str:
    items = rich_text_prop.get("rich_text", []) if rich_text_prop else []
    return "".join(item.get("plain_text", "") for item in items)


def _extract_title(title_prop: dict) -> str:
    items = title_prop.get("title", []) if title_prop else []
    return "".join(item.get("plain_text", "") for item in items)


def _row_to_decision(page: dict) -> Decision | None:
    props = page.get("properties", {})
    decision = props.get("Decision", {}).get("select")
    if not decision:
        return None
    decision_value = decision.get("name", "")

    title = _extract_title(props.get("Job Title", {}))
    company = _extract_text(props.get("Company", {}))
    url = (props.get("URL", {}) or {}).get("url", "") or ""
    stop_reason = _extract_text(props.get("Stop Reason", {}))
    query_sel = (props.get("Query", {}) or {}).get("select") or {}
    query = query_sel.get("name", "") if query_sel else ""

    last_edited = page.get("last_edited_time", "")
    try:
        decided_at = datetime.fromisoformat(last_edited.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        decided_at = datetime.now(timezone.utc)

    return Decision(
        title=title, company=company, decision=decision_value,
        decided_at=decided_at, pipeline=PIPELINE,
        url=url, stop_reason=stop_reason, query=query,
    )


def _bucket(history: DecisionHistory, d: Decision) -> None:
    """Drop a decision into the right bucket. Detects rescue: a non-Auto-Dropped
    verdict on a row that still has a Stop Reason means you changed it from
    Auto-Dropped to something else (most likely Interesting). Treat it as a
    rescue AND as the new verdict's signal."""
    if d.decision in (DECISION_APPLY, DECISION_APPLIED):
        history.applied.append(d)
    elif d.decision == DECISION_INTERESTING:
        history.interested.append(d)
    elif d.decision == DECISION_NOT_INTERESTING:
        history.rejected.append(d)
    elif d.decision == DECISION_DECLINED:
        history.declined.append(d)
    elif d.decision == DECISION_MAYBE:
        # Deliberately neutral: a "better than nothing" job must not pull
        # more jobs like it onto the board, nor push them off.
        history.maybe.append(d)
    # Auto-Dropped rows are not training signal themselves — they are the
    # output of a prior run's stop-list and would create a feedback loop.
    # But if Stop Reason is set AND the verdict is no longer Auto-Dropped,
    # that's a rescue: you explicitly un-blocked this token.
    if d.stop_reason and d.decision != DECISION_AUTO_DROPPED:
        history.auto_dropped_rescued.append(d)


def load_stale_cache() -> DecisionHistory | None:
    if not DECISION_CACHE_FILE.exists():
        return None
    try:
        raw = json.loads(DECISION_CACHE_FILE.read_text(encoding="utf-8"))
        return DecisionHistory.from_dict(raw, source="stale_cache")
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        print(f"[WARN] decision_history.json corrupt — ignored: {e}")
        return None


def _persist_cache(history: DecisionHistory) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    try:
        DECISION_CACHE_FILE.write_text(
            json.dumps(history.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as e:
        print(f"[WARN] could not write decision_history.json: {e}")


def fetch_decisions(
    notion: Client,
    ds_id: str,
    lookback_days: int = LOOKBACK_DAYS_DEFAULT,
) -> DecisionHistory:
    """Pull recent decisions from Notion. Falls back to stale cache on API failure."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    cutoff_iso = cutoff.isoformat()

    history = DecisionHistory(source="notion")
    try:
        cursor = None
        # Filter: any Decision value other than 'New' AND edited within lookback.
        # Notion does not support "is not New" cleanly across the data_sources.query
        # filter shape, so we fetch all non-empty Decision rows in the window
        # and bucket client-side.
        page_count = 0
        while True:
            kwargs: dict = {
                "page_size": 100,
                "filter": {
                    "and": [
                        {"property": "Decision", "select": {"is_not_empty": True}},
                        {"timestamp": "last_edited_time", "last_edited_time": {"on_or_after": cutoff_iso}},
                    ]
                },
            }
            if cursor:
                kwargs["start_cursor"] = cursor
            data = notion.data_sources.query(ds_id, **kwargs)
            for page in data.get("results", []):
                decision = _row_to_decision(page)
                if decision is None or decision.decision == DECISION_NEW:
                    continue
                _bucket(history, decision)
            page_count += 1
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if page_count > 50:  # safety: 5000 rows is way more than the lookback window should produce
                print("[WARN] feedback: pagination > 50 pages — stopping early")
                break
        _persist_cache(history)
        return history
    except APIResponseError as e:
        print(f"[WARN] Notion API failed — using stale cache: {e}")
        cached = load_stale_cache()
        return cached if cached else DecisionHistory(source="stale_cache")
    except Exception as e:
        print(f"[WARN] Notion fetch_decisions exception — using stale cache: {e}")
        cached = load_stale_cache()
        return cached if cached else DecisionHistory(source="stale_cache")


def fetch_decisions_from_env(lookback_days: int = LOOKBACK_DAYS_DEFAULT) -> DecisionHistory:
    """Convenience wrapper. Reads NOTION_API_KEY + NOTION_DATA_SOURCE_ID from env.

    Returns an empty DecisionHistory if env is incomplete (cold start safe).
    """
    api_key = os.environ.get("NOTION_API_KEY", "").strip()
    ds_id = os.environ.get("NOTION_DATA_SOURCE_ID", "").replace("-", "").strip()
    if not api_key or not ds_id:
        print("[INFO] Notion env incomplete — feedback skips the Notion read")
        return DecisionHistory(source="notion")
    notion = Client(auth=api_key)
    try:
        preflight_schema(notion, ds_id)
    except ConfigError as e:
        print(f"[ERROR] Notion schema: {e}")
        # Soft-fail: continue with no decisions rather than crashing the cron.
        # The stop-list will not harden new tokens; existing entries still apply.
        return DecisionHistory(source="notion")
    return fetch_decisions(notion, ds_id, lookback_days=lookback_days)
