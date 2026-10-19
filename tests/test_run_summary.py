"""Tests for utils/run_summary.py — JSONL summary line writing."""
import json
import sys
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from notion_sync.feedback import DecisionHistory
from utils.feedback_features import FeatureIndex
from utils.run_summary import build_summary, write_run_summary
from utils.stoplist import empty_stoplist


def test_write_run_summary_appends_jsonl(tmp_path, monkeypatch):
    monkeypatch.setattr("utils.run_summary.LOGS_DIR", tmp_path)
    summary = {"foo": "bar", "n": 1}
    out = write_run_summary(summary, today=date(2026, 5, 19))
    assert out.name == "feedback_run_20260519.jsonl"
    line = out.read_text(encoding="utf-8").strip()
    parsed = json.loads(line)
    assert parsed["foo"] == "bar"
    assert "ts" in parsed  # auto-added


def test_write_run_summary_appends_not_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr("utils.run_summary.LOGS_DIR", tmp_path)
    write_run_summary({"n": 1}, today=date(2026, 5, 19))
    write_run_summary({"n": 2}, today=date(2026, 5, 19))
    lines = (tmp_path / "feedback_run_20260519.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2


def test_build_summary_shape():
    history = DecisionHistory(source="notion")
    fi = FeatureIndex(pipeline="main")
    fi.positive_title_tokens = Counter({"engineer": 3})
    fi.negative_title_tokens = Counter({"polierer": 4})
    sl = empty_stoplist()

    board = [{"query": "AI Engineer", "found": 12}]
    s = build_summary(
        history=history, features=fi, stoplist=sl,
        jobs_in=400, jobs_dropped=5,
        jobs_synced_to_notion=30, holdover_size=87,
        auto_dropped_synced=5, query_scoreboard=board,
        stale_queries=["Midijob"], suggested_queries=["KI Referent"], errors=[],
    )
    assert s["jobs_in"] == 400
    assert s["jobs_dropped_by_stoplist"] == 5
    assert s["feature_index"]["positive_tokens"] == 1
    assert "shadow_mode" in s["stoplist"]
    assert s["holdover_size"] == 87
    assert s["query_scoreboard"] == board
    assert s["stale_queries"] == ["Midijob"]
    assert s["suggested_queries"] == ["KI Referent"]
