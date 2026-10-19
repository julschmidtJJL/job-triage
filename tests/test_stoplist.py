"""Tests for utils/stoplist.py — load (with corruption recovery), rescue,
harden (respecting allow-list), apply (shadow + enforce modes)."""
import json
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from notion_sync.feedback import Decision, DecisionHistory
from utils.feedback_features import FeatureIndex
from utils.stoplist import (
    SHADOW_DAYS_DEFAULT,
    SHADOW_ENV_FLAG,
    apply_rescue,
    apply_stoplist,
    empty_stoplist,
    harden_stoplist,
    is_shadow_mode,
    load_stoplist,
    save_stoplist,
)


# ---------------------------------------------------------------- load_stoplist

def test_load_missing_returns_empty(tmp_path):
    sl = load_stoplist(tmp_path / "x.json")
    assert sl["version"] == 2
    assert "main" in sl
    assert sl["main"]["title_tokens"] == []


def test_load_valid_json(tmp_path):
    p = tmp_path / "x.json"
    save_stoplist(empty_stoplist(), p)
    sl = load_stoplist(p)
    assert sl["version"] == 2


def test_load_corrupt_backs_up_and_rebuilds(tmp_path):
    """CRITICAL: silent failure mode if not handled."""
    p = tmp_path / "learned_stoplist.json"
    p.write_text("{not valid json", encoding="utf-8")
    sl = load_stoplist(p)
    assert sl["version"] == 2
    # backup file should exist
    backups = list(tmp_path.glob("learned_stoplist.json.corrupt.*"))
    assert len(backups) == 1


def test_load_upgrades_partial_schema(tmp_path):
    """Old v1 file without pipeline keys gets upgraded transparently."""
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"version": 1, "title_tokens": [], "companies": []}), encoding="utf-8")
    sl = load_stoplist(p)
    assert "main" in sl


# ---------------------------------------------------------------- shadow mode

def test_shadow_mode_active_within_window(tmp_path, monkeypatch):
    monkeypatch.delenv(SHADOW_ENV_FLAG, raising=False)
    sl = empty_stoplist()
    sl["launched_at"] = date.today().isoformat()
    assert is_shadow_mode(sl) is True


def test_shadow_mode_inactive_after_window(monkeypatch):
    monkeypatch.delenv(SHADOW_ENV_FLAG, raising=False)
    sl = empty_stoplist()
    sl["launched_at"] = (date.today() - timedelta(days=SHADOW_DAYS_DEFAULT + 1)).isoformat()
    assert is_shadow_mode(sl) is False


def test_shadow_mode_overridden_by_env_flag(monkeypatch):
    monkeypatch.setenv(SHADOW_ENV_FLAG, "1")
    sl = empty_stoplist()
    sl["launched_at"] = date.today().isoformat()
    assert is_shadow_mode(sl) is False


# ---------------------------------------------------------------- harden

def _features_with(neg_tokens=None, neg_companies=None, pipeline="main"):
    fi = FeatureIndex(pipeline=pipeline)
    fi.negative_title_tokens = Counter(neg_tokens or {})
    fi.negative_companies = Counter(neg_companies or {})
    return fi


def test_harden_promotes_token_above_threshold():
    sl = empty_stoplist()
    fi = _features_with(neg_tokens={"polierer": 3})
    harden_stoplist(fi, sl, k=3)
    tokens = [e["token"] for e in sl["main"]["title_tokens"]]
    assert "polierer" in tokens


def test_harden_skips_token_below_threshold():
    sl = empty_stoplist()
    fi = _features_with(neg_tokens={"polierer": 2})
    harden_stoplist(fi, sl, k=3)
    assert sl["main"]["title_tokens"] == []


def test_harden_respects_allow_list_REGRESSION():
    """REGRESSION RISK ★★★: a rescued token must NEVER re-harden, even if
    it crosses the threshold again. This is the central invariant of rescue."""
    sl = empty_stoplist()
    sl["main"]["manual_overrides"]["allow_title_tokens"] = ["polierer"]
    fi = _features_with(neg_tokens={"polierer": 10})  # way above threshold
    harden_stoplist(fi, sl, k=3)
    tokens = [e["token"] for e in sl["main"]["title_tokens"]]
    assert "polierer" not in tokens


def test_harden_preserves_first_seen():
    sl = empty_stoplist()
    sl["main"]["title_tokens"] = [
        {"token": "polierer", "rejected_count": 3, "first_seen": "2026-01-01", "last_seen": "2026-01-05"}
    ]
    fi = _features_with(neg_tokens={"polierer": 5})
    harden_stoplist(fi, sl, k=3)
    entry = next(e for e in sl["main"]["title_tokens"] if e["token"] == "polierer")
    assert entry["first_seen"] == "2026-01-01"
    assert entry["rejected_count"] == 5
    assert entry["last_seen"] == date.today().isoformat()


def test_harden_promotes_companies():
    sl = empty_stoplist()
    fi = _features_with(neg_companies={"zeitarbeit xyz": 3})
    harden_stoplist(fi, sl, k=3)
    names = [e["name"] for e in sl["main"]["companies"]]
    assert "zeitarbeit xyz" in names


# ---------------------------------------------------------------- rescue

def test_rescue_moves_token_to_allow_list(tmp_path, monkeypatch):
    # sidecar with rescued URL
    sidecar = tmp_path / "jobs_seen_urls.json"
    sidecar.write_text(json.dumps({
        "https://x.de/1": {
            "dropped_token": "polierer", "dropped_pipeline": "main",
            "dropped_at": "2026-05-15"
        }
    }), encoding="utf-8")
    monkeypatch.setattr("utils.stoplist.SEEN_URLS_FILE", sidecar)

    sl = empty_stoplist()
    history = DecisionHistory(auto_dropped_rescued=[
        Decision(title="Polierer", company="X GmbH", decision="Interesting",
                 decided_at=datetime.now(timezone.utc), pipeline="main",
                 url="https://x.de/1", stop_reason="matched 'polierer'")
    ])
    apply_rescue(history, sl)
    assert "polierer" in sl["main"]["manual_overrides"]["allow_title_tokens"]


def test_rescue_missing_sidecar_skips_gracefully(tmp_path, monkeypatch):
    monkeypatch.setattr("utils.stoplist.SEEN_URLS_FILE", tmp_path / "missing.json")
    sl = empty_stoplist()
    history = DecisionHistory(auto_dropped_rescued=[
        Decision(title="X", company="Y", decision="Interesting",
                 decided_at=datetime.now(timezone.utc), pipeline="main",
                 url="https://x.de/1", stop_reason="...")
    ])
    apply_rescue(history, sl)  # should not raise
    assert sl["main"]["manual_overrides"]["allow_title_tokens"] == []


def test_rescue_idempotent(tmp_path, monkeypatch):
    sidecar = tmp_path / "jobs_seen_urls.json"
    sidecar.write_text(json.dumps({
        "https://x.de/1": {"dropped_token": "polierer", "dropped_pipeline": "main"}
    }), encoding="utf-8")
    monkeypatch.setattr("utils.stoplist.SEEN_URLS_FILE", sidecar)

    sl = empty_stoplist()
    sl["main"]["manual_overrides"]["allow_title_tokens"] = ["polierer"]
    history = DecisionHistory(auto_dropped_rescued=[
        Decision(title="Polierer", company="X", decision="Interesting",
                 decided_at=datetime.now(timezone.utc), pipeline="main",
                 url="https://x.de/1", stop_reason="...")
    ])
    apply_rescue(history, sl)
    # token appears exactly once
    assert sl["main"]["manual_overrides"]["allow_title_tokens"].count("polierer") == 1


# ---------------------------------------------------------------- apply_stoplist

def _make_job(title, source="linkedin", company="X GmbH"):
    return {"title": title, "company": company, "source": source, "url": f"https://x.de/{title}"}


def test_apply_enforce_drops_matching_token(monkeypatch):
    monkeypatch.setenv(SHADOW_ENV_FLAG, "1")  # force enforce
    sl = empty_stoplist()
    sl["main"]["title_tokens"] = [
        {"token": "polierer", "rejected_count": 5, "first_seen": "2026-01-01", "last_seen": "2026-01-01"}
    ]
    jobs = [_make_job("Polierer Senior"), _make_job("Finance Manager")]
    kept, dropped = apply_stoplist(jobs, sl)
    assert len(kept) == 1
    assert kept[0]["title"] == "Finance Manager"
    assert len(dropped) == 1
    assert dropped[0]["stop_token"] == "polierer"
    assert dropped[0]["stop_pipeline"] == "main"


def test_apply_shadow_keeps_all_but_logs(monkeypatch):
    monkeypatch.delenv(SHADOW_ENV_FLAG, raising=False)
    sl = empty_stoplist()
    sl["launched_at"] = date.today().isoformat()  # in shadow window
    sl["main"]["title_tokens"] = [
        {"token": "polierer", "rejected_count": 5, "first_seen": "x", "last_seen": "x"}
    ]
    jobs = [_make_job("Polierer Senior"), _make_job("Finance Manager")]
    kept, dropped = apply_stoplist(jobs, sl)
    assert len(kept) == 2  # nothing actually dropped
    assert len(dropped) == 1  # but logged what WOULD have dropped
    assert dropped[0]["stop_reason"].startswith("matched title token")


def test_apply_respects_allow_list(monkeypatch):
    monkeypatch.setenv(SHADOW_ENV_FLAG, "1")
    sl = empty_stoplist()
    sl["main"]["title_tokens"] = [{"token": "polierer", "rejected_count": 5,
                                       "first_seen": "x", "last_seen": "x"}]
    sl["main"]["manual_overrides"]["allow_title_tokens"] = ["polierer"]
    jobs = [_make_job("Polierer Senior")]
    kept, dropped = apply_stoplist(jobs, sl)
    assert len(kept) == 1
    assert len(dropped) == 0


def test_apply_deny_list_forces_drop(monkeypatch):
    monkeypatch.setenv(SHADOW_ENV_FLAG, "1")
    sl = empty_stoplist()
    sl["main"]["manual_overrides"]["deny_title_tokens"] = ["zeitarbeit"]
    jobs = [_make_job("Zeitarbeit Engineer")]
    kept, dropped = apply_stoplist(jobs, sl)
    assert len(kept) == 0
    assert len(dropped) == 1



def test_load_prunes_generic_tokens_learned_earlier(tmp_path):
    p = tmp_path / "x.json"
    sl = empty_stoplist()
    sl["main"]["title_tokens"] = [
        {"token": t, "rejected_count": 5, "first_seen": "x", "last_seen": "x"}
        for t in ("manager", "consultant", "hamburg", "steuerberatung")
    ]
    sl["main"]["manual_overrides"]["deny_title_tokens"] = ["manager"]  # typed on purpose: kept
    save_stoplist(sl, p)
    loaded = load_stoplist(p)
    assert [e["token"] for e in loaded["main"]["title_tokens"]] == ["steuerberatung"]
    assert loaded["main"]["manual_overrides"]["deny_title_tokens"] == ["manager"]
