"""Stop-list module: load/save, rescue, harden, apply.

Schema v2 (pipeline-namespaced per CEO review):

  {
    "version": 2,
    "launched_at": "2026-05-20",          # set on first save; drives shadow mode
    "main": {
      "title_tokens": [{"token": "polierer", "rejected_count": 4,
                         "first_seen": "...", "last_seen": "..."}],
      "companies":    [{"name": "...", "rejected_count": 3, ...}],
      "manual_overrides": {"allow_title_tokens": [...], "deny_title_tokens": [...]}
    }
  }

One pipeline ("main") today; the namespacing stays so a second pipeline
would not need a schema change.

Per-run ordering (design "Per-Run Pipeline Order"):
  load_stoplist → fetch_decisions → apply_rescue (mutates allow list)
  → persist → build features → apply_stoplist → score → cap → sync
  → harden_stoplist (respects fresh allow list, so a just-rescued token
  cannot re-harden the same run).
"""
import json
import os
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from notion_sync.feedback import PIPELINE, DecisionHistory
from utils.feedback_features import FeatureIndex
from utils.text_normalize import normalize_company

DATA_DIR = Path(__file__).parent.parent / "data"
STOPLIST_FILE = DATA_DIR / "learned_stoplist.json"
SEEN_URLS_FILE = DATA_DIR / "jobs_seen_urls.json"  # for rescue token sidecar

STOPLIST_SCHEMA_VERSION = 2
HARDEN_THRESHOLD_DEFAULT = 3
SHADOW_DAYS_DEFAULT = 14
SHADOW_ENV_FLAG = "JOB_HUNT_STOPLIST_ENFORCE"  # "1" to force-enable enforcement
PIPELINES = (PIPELINE,)


def _empty_pipeline_state() -> dict:
    return {
        "title_tokens": [],
        "companies": [],
        "manual_overrides": {"allow_title_tokens": [], "deny_title_tokens": []},
    }


def empty_stoplist() -> dict:
    return {
        "version": STOPLIST_SCHEMA_VERSION,
        "launched_at": date.today().isoformat(),
        **{pipe: _empty_pipeline_state() for pipe in PIPELINES},
    }


def _backup_corrupt(path: Path) -> None:
    ts = datetime.now().strftime("%Y%m%dT%H%M%S")
    backup = path.with_suffix(f".json.corrupt.{ts}")
    try:
        path.rename(backup)
        print(f"[WARN] learned_stoplist.json corrupt — backed up to {backup.name}")
    except OSError as e:
        print(f"[WARN] learned_stoplist.json corrupt — backup failed: {e}")


def load_stoplist(path: Path = STOPLIST_FILE) -> dict:
    """Load the stop-list. On JSONDecodeError, back up and rebuild empty."""
    if not path.exists():
        return empty_stoplist()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        _backup_corrupt(path)
        return empty_stoplist()

    # Schema upgrade: ensure every pipeline exists with full structure
    if raw.get("version") != STOPLIST_SCHEMA_VERSION or any(p not in raw for p in PIPELINES):
        empty = empty_stoplist()
        for pipe in PIPELINES:
            if pipe not in raw:
                raw[pipe] = empty[pipe]
            else:
                # Merge missing keys
                for k, v in empty[pipe].items():
                    raw[pipe].setdefault(k, v)
                raw[pipe]["manual_overrides"].setdefault("allow_title_tokens", [])
                raw[pipe]["manual_overrides"].setdefault("deny_title_tokens", [])
        raw["version"] = STOPLIST_SCHEMA_VERSION
        raw.setdefault("launched_at", date.today().isoformat())
    return _prune_generic(raw)


def _prune_generic(stoplist: dict) -> dict:
    """Drop learned title tokens the feedback loop may no longer learn from
    (generic title words, search vocabulary). Manual deny entries stay: those
    were typed on purpose."""
    from utils.profile import search_vocabulary
    from utils.text_normalize import GENERIC_TITLE_WORDS

    unlearnable = GENERIC_TITLE_WORDS | search_vocabulary()
    for pipe in PIPELINES:
        state = stoplist.get(pipe, {})
        kept = [e for e in state.get("title_tokens", []) if e.get("token") not in unlearnable]
        dropped = len(state.get("title_tokens", [])) - len(kept)
        if dropped:
            print(f"[STOPLIST] Removed {dropped} generic tokens learned earlier")
            state["title_tokens"] = kept
    return stoplist


def save_stoplist(stoplist: dict, path: Path = STOPLIST_FILE) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    path.write_text(json.dumps(stoplist, ensure_ascii=False, indent=2), encoding="utf-8")


def is_shadow_mode(stoplist: dict, today: date | None = None) -> bool:
    """True iff the stop-list is still in its 14-day shadow window AND the
    enforcement env flag is not set. Shadow mode logs would-have-dropped but
    does not actually drop."""
    if os.environ.get(SHADOW_ENV_FLAG, "") == "1":
        return False
    launched_str = stoplist.get("launched_at")
    if not launched_str:
        return True  # no launch date → still shadow
    try:
        launched = date.fromisoformat(launched_str)
    except ValueError:
        return True
    today = today or date.today()
    return (today - launched).days < SHADOW_DAYS_DEFAULT


# -------------------------------------------------------------------- rescue


def _load_sidecar() -> dict:
    """Read jobs_seen_urls.json. May be dict (current) or list (legacy)."""
    if not SEEN_URLS_FILE.exists():
        return {}
    try:
        raw = json.loads(SEEN_URLS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    # Legacy: list of URLs (handled in pipeline._load_seen_cache); treat as no sidecar
    if not isinstance(raw, dict):
        return {}
    return raw


def _sidecar_lookup(url: str) -> tuple[str, str]:
    """Return (dropped_token, dropped_pipeline) from the sidecar for the URL,
    or ('', '') if not found / not a structured sidecar entry."""
    sidecar = _load_sidecar()
    entry = sidecar.get(url)
    if isinstance(entry, dict):
        return entry.get("dropped_token", ""), entry.get("dropped_pipeline", "")
    return "", ""


def apply_rescue(history: DecisionHistory, stoplist: dict) -> dict:
    """Mutate stoplist in place: for each auto_dropped_rescued decision, look up
    the dropped token from the jobs_seen_urls.json sidecar and add it to the
    matching pipeline's manual_overrides.allow_title_tokens.

    Idempotent. Missing sidecar entries log a warning and skip (best-effort
    rescue, never raises)."""
    for d in history.auto_dropped_rescued:
        if not d.url:
            continue
        token, pipeline = _sidecar_lookup(d.url)
        if not token or not pipeline:
            print(f"[WARN] Rescue: no sidecar entry for {d.url} — skipped")
            continue
        if pipeline not in stoplist:
            continue
        allow = stoplist[pipeline]["manual_overrides"].setdefault("allow_title_tokens", [])
        if token not in allow:
            allow.append(token)
            print(f"[OK] Rescue: '{token}' allowed again ({pipeline})")
    return stoplist


# -------------------------------------------------------------------- harden


def harden_stoplist(
    features: FeatureIndex,
    stoplist: dict,
    k: int = HARDEN_THRESHOLD_DEFAULT,
) -> dict:
    """Promote tokens/companies with rejected_count >= K into the stop-list.

    Respects manual_overrides.allow_title_tokens: a rescued token is NEVER
    re-hardened, even if it crosses the threshold again (per design Premise 7
    + eng-review D1 ordering)."""
    pipeline = features.pipeline
    if pipeline not in stoplist:
        return stoplist
    state = stoplist[pipeline]
    allow = set(state["manual_overrides"].get("allow_title_tokens", []))
    today = date.today().isoformat()

    # Tokens
    existing_tokens = {e["token"]: e for e in state["title_tokens"]}
    for token, count in features.negative_title_tokens.items():
        if count < k or token in allow:
            continue
        if token in existing_tokens:
            existing_tokens[token]["rejected_count"] = count
            existing_tokens[token]["last_seen"] = today
        else:
            existing_tokens[token] = {
                "token": token, "rejected_count": count,
                "first_seen": today, "last_seen": today,
            }
    state["title_tokens"] = list(existing_tokens.values())

    # Companies
    existing_companies = {e["name"]: e for e in state["companies"]}
    for name, count in features.negative_companies.items():
        if count < k:
            continue
        if name in existing_companies:
            existing_companies[name]["rejected_count"] = count
            existing_companies[name]["last_seen"] = today
        else:
            existing_companies[name] = {
                "name": name, "rejected_count": count,
                "first_seen": today, "last_seen": today,
            }
    state["companies"] = list(existing_companies.values())

    return stoplist


# -------------------------------------------------------------------- apply


def _token_blocked(title: str, pipeline_state: dict) -> tuple[bool, str]:
    """Return (is_blocked, matched_token). manual deny wins over allow.
    Allow list rescues a token that would otherwise be in title_tokens."""
    from utils.text_normalize import tokenize_title

    overrides = pipeline_state.get("manual_overrides", {})
    deny = set(overrides.get("deny_title_tokens", []))
    allow = set(overrides.get("allow_title_tokens", []))

    title_tokens = tokenize_title(title)

    # deny wins
    for tok in title_tokens:
        if tok in deny:
            return True, tok

    blocked_tokens = {e["token"] for e in pipeline_state.get("title_tokens", [])}
    for tok in title_tokens:
        if tok in blocked_tokens and tok not in allow:
            return True, tok
    return False, ""


def _company_blocked(company: str, pipeline_state: dict) -> tuple[bool, str]:
    if not company:
        return False, ""
    normalized = normalize_company(company)
    blocked_companies = {e["name"] for e in pipeline_state.get("companies", [])}
    if normalized in blocked_companies:
        return True, normalized
    return False, ""


def apply_stoplist(
    jobs: list,
    stoplist: dict,
    pipeline_of: callable = None,
) -> tuple[list, list]:
    """Split jobs into (kept, would_drop_or_dropped).

    In shadow mode, returns (all_jobs, would_drop_log) — kept includes everything,
    but the second list is still populated so the caller can log would-have-drops.

    In enforce mode, returns (jobs_minus_dropped, dropped_with_reasons).

    `pipeline_of(job) -> str` defaults to deriving from job["source"].
    """
    if pipeline_of is None:
        from notion_sync.feedback import derive_pipeline
        pipeline_of = lambda j: derive_pipeline(j.get("source", ""))

    shadow = is_shadow_mode(stoplist)
    kept: list = []
    drop_log: list = []

    for job in jobs:
        pipeline = pipeline_of(job)
        state = stoplist.get(pipeline)
        if not state:
            kept.append(job)
            continue

        blocked, token = _token_blocked(job.get("title", ""), state)
        reason = ""
        if blocked:
            reason = f"matched title token '{token}'"
        else:
            blocked, company = _company_blocked(job.get("company", ""), state)
            if blocked:
                reason = f"matched company '{company}'"

        if blocked:
            entry = dict(job)
            entry["stop_reason"] = reason
            entry["stop_token"] = token if reason.startswith("matched title") else ""
            entry["stop_pipeline"] = pipeline
            drop_log.append(entry)
            if not shadow:
                continue  # actually drop
        kept.append(job)

    return kept, drop_log
