"""Tests for notion_sync/feedback.py — Notion read-back, schema preflight,
stale-cache fallback, decision bucketing, pipeline derivation."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from notion_client import APIResponseError

sys.path.insert(0, str(Path(__file__).parent.parent))

from notion_sync.feedback import (
    ConfigError,
    Decision,
    DecisionHistory,
    derive_pipeline,
    fetch_decisions,
    load_stale_cache,
    preflight_schema,
)


def _make_page(
    decision: str | None,
    title: str = "Test Job",
    company: str = "Test GmbH",
    url: str = "https://example.com/1",
    portal: str = "LinkedIn",
    stop_reason: str = "",
    last_edited: str | None = None,
    query: str = "",
) -> dict:
    last_edited = last_edited or datetime.now(timezone.utc).isoformat()
    return {
        "last_edited_time": last_edited,
        "properties": {
            "Job Title": {"title": [{"plain_text": title}]},
            "Company": {"rich_text": [{"plain_text": company}]},
            "URL": {"url": url},
            "Portal": {"select": {"name": portal}},
            "Decision": {"select": {"name": decision}} if decision else {"select": None},
            "Query": {"select": {"name": query}} if query else {"select": None},
            "Stop Reason": {"rich_text": [{"plain_text": stop_reason}]} if stop_reason else {"rich_text": []},
        },
    }


# ---------------------------------------------------------------- derive_pipeline

def test_derive_pipeline_is_single_pipeline():
    assert derive_pipeline("linkedin") == "main"
    assert derive_pipeline("google_jobs") == "main"
    assert derive_pipeline("") == "main"


# ---------------------------------------------------------------- preflight_schema

def test_preflight_schema_valid():
    notion = MagicMock()
    notion.data_sources.retrieve.return_value = {
        "properties": {
            "Decision": {
                "type": "select",
                "select": {
                    "options": [
                        {"name": "New"}, {"name": "Interesting"}, {"name": "Apply"},
                        {"name": "Applied"}, {"name": "Not interesting"}, {"name": "Declined"},
                        {"name": "Auto-Dropped"},
                    ]
                },
            },
            "Stop Reason": {"type": "rich_text"},
        }
    }
    preflight_schema(notion, "ds-id")  # should not raise


def test_preflight_schema_missing_entscheidung():
    notion = MagicMock()
    notion.data_sources.retrieve.return_value = {"properties": {}}
    with pytest.raises(ConfigError, match="Decision"):
        preflight_schema(notion, "ds-id")


def test_preflight_schema_missing_auto_dropped_option():
    notion = MagicMock()
    notion.data_sources.retrieve.return_value = {
        "properties": {
            "Decision": {
                "type": "select",
                "select": {"options": [{"name": "New"}, {"name": "Apply"}]},
            },
            "Stop Reason": {"type": "rich_text"},
        }
    }
    with pytest.raises(ConfigError, match="Auto-Dropped"):
        preflight_schema(notion, "ds-id")


def test_preflight_schema_missing_stop_reason():
    notion = MagicMock()
    notion.data_sources.retrieve.return_value = {
        "properties": {
            "Decision": {
                "type": "select",
                "select": {"options": [{"name": "Auto-Dropped"}, {"name": "New"}]},
            },
        }
    }
    with pytest.raises(ConfigError, match="Stop Reason"):
        preflight_schema(notion, "ds-id")


# ---------------------------------------------------------------- fetch_decisions

def test_fetch_decisions_happy_buckets_all_five():
    notion = MagicMock()
    notion.data_sources.query.return_value = {
        "results": [
            _make_page("Apply", title="AI Engineer", portal="LinkedIn", query="AI Engineer"),
            _make_page("Applied", title="KI Manager"),
            _make_page("Interesting", title="Data Engineer"),
            _make_page("Not interesting", title="Polierer"),
            _make_page("Declined", title="Buchhalter"),
            _make_page("Interesting", title="Digitalisierung Referent",
                       stop_reason="matched title token 'referent' (rejected 3x)"),
        ],
        "has_more": False,
    }
    history = fetch_decisions(notion, "ds-id")
    assert len(history.applied) == 2  # Apply + Applied
    assert len(history.interested) == 2  # 2x Interesting
    assert len(history.rejected) == 1
    assert len(history.declined) == 1
    # rescued: has Stop Reason AND verdict != Auto-Dropped
    assert len(history.auto_dropped_rescued) == 1
    assert history.auto_dropped_rescued[0].pipeline == "main"
    assert history.applied[0].query == "AI Engineer"
    assert history.source == "notion"


def test_fetch_decisions_empty_cold_start(tmp_path):
    notion = MagicMock()
    notion.data_sources.query.return_value = {"results": [], "has_more": False}
    with patch("notion_sync.feedback.DECISION_CACHE_FILE", tmp_path / "cache.json"):
        history = fetch_decisions(notion, "ds-id")
    assert history.total() == 0
    assert history.source == "notion"


def test_fetch_decisions_skips_neu():
    notion = MagicMock()
    notion.data_sources.query.return_value = {
        "results": [
            _make_page("New"),
            _make_page("Apply"),
        ],
        "has_more": False,
    }
    history = fetch_decisions(notion, "ds-id")
    assert history.total() == 1
    assert len(history.applied) == 1


def test_fetch_decisions_paginates():
    notion = MagicMock()
    notion.data_sources.query.side_effect = [
        {"results": [_make_page("Apply")], "has_more": True, "next_cursor": "cur1"},
        {"results": [_make_page("Not interesting")], "has_more": False},
    ]
    history = fetch_decisions(notion, "ds-id")
    assert len(history.applied) == 1
    assert len(history.rejected) == 1
    assert notion.data_sources.query.call_count == 2


def test_fetch_decisions_api_error_falls_back_to_stale_cache(tmp_path):
    notion = MagicMock()
    err = APIResponseError(response=MagicMock(status_code=503, json=lambda: {}),
                           message="server error", code="internal_server_error")
    notion.data_sources.query.side_effect = err

    cache_file = tmp_path / "decision_history.json"
    cache_file.write_text(
        '{"applied": [{"title":"CFO","company":"BMW","decision":"Apply",'
        '"decided_at":"2026-05-01T00:00:00+00:00","pipeline":"finance","url":"","stop_reason":""}],'
        '"interested":[],"rejected":[],"declined":[],"auto_dropped_rescued":[]}',
        encoding="utf-8",
    )

    with patch("notion_sync.feedback.DECISION_CACHE_FILE", cache_file):
        history = fetch_decisions(notion, "ds-id")
    assert history.source == "stale_cache"
    assert len(history.applied) == 1


def test_fetch_decisions_api_error_no_cache_returns_empty(tmp_path):
    notion = MagicMock()
    err = APIResponseError(response=MagicMock(status_code=503, json=lambda: {}),
                           message="server error", code="internal_server_error")
    notion.data_sources.query.side_effect = err
    with patch("notion_sync.feedback.DECISION_CACHE_FILE", tmp_path / "missing.json"):
        history = fetch_decisions(notion, "ds-id")
    assert history.source == "stale_cache"
    assert history.total() == 0


def test_fetch_decisions_filter_uses_90d_lookback():
    """Spec: the Notion filter MUST include a last_edited_time >= now-90d
    bound so the query is bounded as decision history grows."""
    notion = MagicMock()
    notion.data_sources.query.return_value = {"results": [], "has_more": False}
    fetch_decisions(notion, "ds-id", lookback_days=90)
    call_args = notion.data_sources.query.call_args
    filter_arg = call_args.kwargs["filter"]
    and_filters = filter_arg["and"]
    last_edited_filter = next(f for f in and_filters if f.get("timestamp") == "last_edited_time")
    assert "on_or_after" in last_edited_filter["last_edited_time"]


# ---------------------------------------------------------------- bucket: rescue detection

def test_rescue_detected_when_stop_reason_present_and_verdict_not_auto_dropped():
    """Rescue signal: row was once Auto-Dropped (so it has Stop Reason),
    but you changed the verdict to something else (most likely Interesting)."""
    notion = MagicMock()
    notion.data_sources.query.return_value = {
        "results": [
            _make_page("Interesting", title="Polierer Senior",
                       stop_reason="matched title token 'polierer' (rejected 3x)"),
        ],
        "has_more": False,
    }
    history = fetch_decisions(notion, "ds-id")
    # Goes into BOTH interested (new verdict signal) AND auto_dropped_rescued
    assert len(history.interested) == 1
    assert len(history.auto_dropped_rescued) == 1


def test_auto_dropped_verdict_itself_is_not_signal():
    """A row that's STILL Auto-Dropped is the output of a prior run's stop-list;
    bucketing it would create a feedback loop. Verify it lands nowhere."""
    notion = MagicMock()
    notion.data_sources.query.return_value = {
        "results": [
            _make_page("Auto-Dropped", title="Polierer",
                       stop_reason="matched title token 'polierer' (rejected 3x)"),
        ],
        "has_more": False,
    }
    history = fetch_decisions(notion, "ds-id")
    assert history.total() == 0


# ---------------------------------------------------------------- cache round-trip

def test_load_stale_cache_missing(tmp_path):
    with patch("notion_sync.feedback.DECISION_CACHE_FILE", tmp_path / "x.json"):
        assert load_stale_cache() is None


def test_load_stale_cache_corrupt(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("{not json", encoding="utf-8")
    with patch("notion_sync.feedback.DECISION_CACHE_FILE", p):
        assert load_stale_cache() is None


def test_load_stale_cache_round_trip(tmp_path):
    p = tmp_path / "x.json"
    src = DecisionHistory(
        applied=[Decision(title="CFO", company="BMW", decision="Apply",
                          decided_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
                          pipeline="finance")]
    )
    p.write_text(__import__("json").dumps(src.to_dict()), encoding="utf-8")
    with patch("notion_sync.feedback.DECISION_CACHE_FILE", p):
        loaded = load_stale_cache()
    assert loaded is not None
    assert len(loaded.applied) == 1
    assert loaded.applied[0].title == "CFO"



def test_maybe_is_recorded_but_neutral():
    from notion_sync.feedback import _bucket
    from utils.feedback_features import build_feature_index
    history = DecisionHistory()
    d = Decision(title="Prozessmanager Logistik", company="Acme", decision="Maybe",
                 decided_at=datetime.now(timezone.utc), pipeline="main")
    _bucket(history, d)
    assert history.maybe == [d]
    assert not (history.applied or history.interested or history.rejected or history.declined)
    assert build_feature_index(history, "main").is_empty()
    # survives the stale-cache round trip
    assert DecisionHistory.from_dict(history.to_dict()).maybe[0].title == "Prozessmanager Logistik"
