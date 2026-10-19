"""Tests for utils/holdover.py — top-N cap, holdover_runs counter, 3-run expiry."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.holdover import (
    HOLDOVER_MAX_RUNS,
    TOP_N_DEFAULT,
    TOP_N_ENV,
    load_holdover,
    merge_with_fresh,
    save_holdover,
    split_top_n_and_holdover,
    top_n_from_env,
)


def _j(url, score):
    return {"url": url, "title": f"t{url}", "relevance_score": score}


# ---------------------------------------------------------------- top_n_from_env

def test_top_n_default(monkeypatch):
    monkeypatch.delenv(TOP_N_ENV, raising=False)
    assert top_n_from_env() == TOP_N_DEFAULT


def test_top_n_override(monkeypatch):
    monkeypatch.setenv(TOP_N_ENV, "5")
    assert top_n_from_env() == 5


def test_top_n_invalid_falls_back(monkeypatch):
    monkeypatch.setenv(TOP_N_ENV, "not-a-number")
    assert top_n_from_env() == TOP_N_DEFAULT


def test_top_n_negative_floored_to_one(monkeypatch):
    monkeypatch.setenv(TOP_N_ENV, "-5")
    assert top_n_from_env() == 1


# ---------------------------------------------------------------- load/save

def test_load_missing_returns_empty(tmp_path):
    assert load_holdover(tmp_path / "x.json") == []


def test_save_load_round_trip(tmp_path):
    p = tmp_path / "h.json"
    jobs = [_j("a", 70), _j("b", 60)]
    save_holdover(jobs, p)
    assert load_holdover(p) == jobs


def test_load_corrupt_returns_empty(tmp_path):
    p = tmp_path / "h.json"
    p.write_text("{not json", encoding="utf-8")
    assert load_holdover(p) == []


# ---------------------------------------------------------------- split

def test_split_top_n_and_holdover_basic():
    scored = [_j(str(i), 100 - i) for i in range(5)]  # already sorted desc
    top, holdover, expired = split_top_n_and_holdover(scored, top_n=2)
    assert len(top) == 2
    assert len(holdover) == 3
    assert expired == []
    # holdover entries gain runs=1
    for j in holdover:
        assert j["holdover_runs"] == 1


def test_holdover_runs_increments():
    """Job already at runs=1 should go to runs=2 next pass."""
    scored = [{"url": "a", "title": "t", "relevance_score": 50, "holdover_runs": 1}]
    top, holdover, expired = split_top_n_and_holdover(scored, top_n=0)
    assert len(holdover) == 1
    assert holdover[0]["holdover_runs"] == 2
    assert expired == []


def test_holdover_expires_at_max_runs():
    """Job at runs=HOLDOVER_MAX_RUNS-1 (=2 by default) → next pass would
    be runs=3 → expires."""
    scored = [{"url": "a", "title": "t", "relevance_score": 50,
               "holdover_runs": HOLDOVER_MAX_RUNS - 1}]
    top, holdover, expired = split_top_n_and_holdover(scored, top_n=0)
    assert holdover == []
    assert len(expired) == 1


def test_top_n_zero_keeps_everything_in_holdover():
    scored = [_j(str(i), 50) for i in range(3)]
    top, holdover, expired = split_top_n_and_holdover(scored, top_n=0)
    assert top == []
    assert len(holdover) == 3


# ---------------------------------------------------------------- merge

def test_merge_keeps_holdover_not_in_fresh():
    fresh = [_j("a", 70)]
    held = [_j("b", 60)]
    out = merge_with_fresh(held, fresh)
    urls = {j["url"] for j in out}
    assert urls == {"a", "b"}


def test_merge_dedupes_fresh_wins():
    """If a URL is in both fresh and holdover, fresh wins (newer scrape)."""
    fresh = [{"url": "a", "title": "fresh-title", "relevance_score": 80}]
    held = [{"url": "a", "title": "old-title", "relevance_score": 50, "holdover_runs": 2}]
    out = merge_with_fresh(held, fresh)
    assert len(out) == 1
    assert out[0]["title"] == "fresh-title"
