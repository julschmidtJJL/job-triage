"""Tests for utils/query_stats.py — the per-query scoreboard."""
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from notion_sync.feedback import Decision, DecisionHistory
from utils import query_stats as qs

QUERIES = ["AI Engineer", "KI Manager"]
CONFIG = [{"q": "AI Engineer", "active": True}, {"q": "KI Manager", "active": True},
          {"q": "Midijob", "active": False}]
TODAY = date(2026, 10, 13)


def _decision(title, query):
    return Decision(title=title, company="X", decision="Interesting",
                    decided_at=datetime.now(timezone.utc), pipeline="main", query=query)


def test_attribute_keeps_scraper_query():
    assert qs.attribute_query({"query": "KI Manager", "title": "AI Engineer"}, QUERIES) == "KI Manager"


def test_attribute_falls_back_to_title_then_description():
    assert qs.attribute_query({"title": "Senior AI Engineer (m/w/d)"}, QUERIES) == "AI Engineer"
    assert qs.attribute_query({"title": "Referent", "description": "als KI Manager"}, QUERIES) == "KI Manager"
    assert qs.attribute_query({"title": "Hausmeister"}, QUERIES) == qs.UNATTRIBUTED


def test_record_run_counts_each_stage():
    raw = [{"query": "AI Engineer"}] * 3 + [{"query": "KI Manager"}]
    new = [{"query": "AI Engineer"}] * 2
    scored = [
        {"query": "AI Engineer", "prefilter_score": 40, "relevance_score": 80, "scoring_method": "claude"},
        {"query": "AI Engineer", "prefilter_score": 10, "relevance_score": 10, "scoring_method": "keyword_fallback"},
    ]
    stats = qs.record_run({"days": {}}, raw_jobs=raw, new_jobs=new, scored_jobs=scored, threshold=20, today=TODAY)
    day = stats["days"]["2026-10-13"]
    assert day["AI Engineer"] == {"found": 3, "new": 2, "passed": 1, "scored": 1, "score_sum": 80}
    assert day["KI Manager"]["found"] == 1


def test_two_runs_on_one_day_add_up():
    stats = {"days": {}}
    for _ in range(2):
        qs.record_run(stats, raw_jobs=[{"query": "AI Engineer"}], new_jobs=[], scored_jobs=[],
                      threshold=20, today=TODAY)
    assert stats["days"]["2026-10-13"]["AI Engineer"]["found"] == 2


def test_save_trims_old_days(tmp_path):
    stats = {"days": {"2026-01-01": {"AI Engineer": {"found": 1}}, "2026-10-12": {}}}
    qs.save_stats(stats, path=tmp_path / "s.json", today=TODAY)
    assert list(qs.load_stats(tmp_path / "s.json")["days"]) == ["2026-10-12"]


def test_load_corrupt_starts_fresh(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{nope", encoding="utf-8")
    assert qs.load_stats(p) == {"days": {}}


def test_scoreboard_windows_and_positives():
    stats = {"days": {
        "2026-08-01": {"AI Engineer": {"found": 99, "new": 99, "passed": 99, "scored": 0, "score_sum": 0}},
        "2026-10-10": {"AI Engineer": {"found": 5, "new": 4, "passed": 2, "scored": 2, "score_sum": 130}},
    }}
    history = DecisionHistory(interested=[_decision("AI Engineer", "AI Engineer")])
    rows = {r["query"]: r for r in qs.scoreboard(stats, history, CONFIG, today=TODAY)}
    assert rows["AI Engineer"]["found"] == 5  # August is outside the 28-day window
    assert rows["AI Engineer"]["avg_score"] == 65
    assert rows["AI Engineer"]["positives"] == 1
    assert rows["AI Engineer"]["first_seen"] == "2026-08-01"
    assert rows["Midijob"]["active"] is False
    assert rows["KI Manager"]["avg_score"] is None


def test_stale_queries_need_age_and_no_yield():
    rows = [
        {"query": "Old dud", "active": True, "first_seen": "2026-09-01", "passed": 0, "positives": 0},
        {"query": "Young", "active": True, "first_seen": "2026-10-10", "passed": 0, "positives": 0},
        {"query": "Works", "active": True, "first_seen": "2026-09-01", "passed": 3, "positives": 0},
        {"query": "Off", "active": False, "first_seen": "2026-09-01", "passed": 0, "positives": 0},
    ]
    assert qs.stale_queries(rows, today=TODAY) == ["Old dud"]


def test_format_scoreboard_marks_inactive_and_hides_empty_unattributed():
    rows = qs.scoreboard({"days": {}}, None, CONFIG, today=TODAY)
    text = qs.format_scoreboard(rows)
    assert "Midijob (off)" in text
    assert qs.UNATTRIBUTED not in text


def test_suggest_queries_filters_existing(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    client = MagicMock()
    client.messages.parse.return_value = SimpleNamespace(
        parsed_output=SimpleNamespace(queries=["ai engineer", "KI Referent", "Head of AI"]))
    with patch("anthropic.Anthropic", return_value=client):
        out = qs.suggest_queries(["KI Referent Mittelstand"], QUERIES, model="claude-haiku-5-5")
    assert out == ["KI Referent", "Head of AI"]


def test_suggest_queries_without_key_or_titles_returns_empty(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert qs.suggest_queries(["x"], QUERIES, model="m") == []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    assert qs.suggest_queries([], QUERIES, model="m") == []
