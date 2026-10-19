"""Tests for the feedback-adjustment logic in score_jobs (utils/pipeline.py).

Covers the +/- delta application driven by a FeatureIndex, score clamp, and
feedback_adjustments string population."""
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.feedback_features import FeatureIndex
from utils.pipeline import score_jobs

ROLE_TERMS = ["ai", "data", "engineer", "manager"]


def _job(title, company="Test GmbH", source="linkedin"):
    return {
        "title": title, "company": company, "location": "Hamburg",
        "url": f"https://example.com/{title}", "description": "", "source": source,
    }


def _score(jobs, features=None):
    # profile_text="" keeps Tier 2 (Claude) out of these tests
    return score_jobs(jobs, features, role_terms=ROLE_TERMS, profile_text="")


def _fi(pipeline="main", pos_tokens=None, neg_tokens=None, pos_companies=None, neg_companies=None):
    fi = FeatureIndex(pipeline=pipeline)
    fi.positive_title_tokens = Counter(pos_tokens or {})
    fi.negative_title_tokens = Counter(neg_tokens or {})
    fi.positive_companies = set(pos_companies or [])
    fi.negative_companies = Counter(neg_companies or {})
    return fi


# ---------------------------------------------------------------- backward compat

def test_score_jobs_works_without_features():
    """Existing callers passing only `jobs` must still work."""
    result = _score([_job("AI Engineer (m/w/d)")])
    assert "relevance_score" in result[0]


# ---------------------------------------------------------------- positive deltas

def test_positive_company_match_adds_30():
    base = _score([_job("Random Job", company="BMW AG")])[0]["relevance_score"]
    fi = _fi(pos_companies=["bmw"])
    boosted = _score([_job("Random Job", company="BMW AG")], features=fi)[0]
    assert boosted["relevance_score"] == min(100, base + 30)
    assert any("BMW" in r.lower() or "bmw" in r for r in boosted["feedback_adjustments"])


def test_positive_token_match_adds_up_to_cap():
    # 4 positive tokens × 10 = 40, capped at 30
    fi = _fi(pos_tokens={"data": 5, "engineer": 5, "manager": 5, "platform": 5})
    base = _score([_job("Random title")])[0]["relevance_score"]
    boosted = _score([_job("Data Platform Manager Engineer")], features=fi)[0]
    # the base for this title includes role-term matches too,
    # so we compare delta on the feedback string instead of absolute score
    assert any("+30" in r and "positive tokens" in r for r in boosted["feedback_adjustments"])


def test_positive_token_below_min_count_ignored():
    """A token with count<2 doesn't fire — single click isn't policy."""
    fi = _fi(pos_tokens={"polierer": 1})
    result = _score([_job("Polierer Senior")], features=fi)[0]
    assert not any("positive tokens" in r for r in result.get("feedback_adjustments", []))


# ---------------------------------------------------------------- negative deltas

def test_negative_token_subtracts_score():
    base = _score([_job("Polierer Senior")])[0]["relevance_score"]
    fi = _fi(neg_tokens={"polierer": 5})
    penalized = _score([_job("Polierer Senior")], features=fi)[0]
    assert penalized["relevance_score"] == max(0, base - 15)
    assert any("-15" in r for r in penalized["feedback_adjustments"])


def test_negative_company_subtracts_20():
    base = _score([_job("Random", company="Zeitarbeit XYZ GmbH")])[0]["relevance_score"]
    fi = _fi(neg_companies={"zeitarbeit xyz": 3})
    penalized = _score([_job("Random", company="Zeitarbeit XYZ GmbH")], features=fi)[0]
    assert penalized["relevance_score"] == max(0, base - 20)


def test_negative_cap_at_45():
    fi = _fi(neg_tokens={"a": 5, "bb": 5, "ccc": 5, "polierer": 5})  # 4 tokens × 15 = 60, cap 45
    # Use real tokens (≥4 chars) since tokenize_title drops short ones
    fi = _fi(neg_tokens={"foo1": 5, "bar2": 5, "baz3": 5, "polierer": 5})
    job = _job("Foo1 Bar2 Baz3 Polierer Engineer")
    base = _score([job])[0]["relevance_score"]
    result = _score([job], features=fi)[0]
    assert any("-45" in r for r in result["feedback_adjustments"])


def test_score_clamped_to_zero():
    """A heavily-penalized job can't go below 0."""
    fi = _fi(neg_tokens={"polierer": 10})
    result = _score([_job("Polierer")], features=fi)[0]
    assert result["relevance_score"] >= 0


def test_score_clamped_to_100():
    """Positive boosts can't push above 100."""
    fi = _fi(pos_companies=["bmw"], pos_tokens={"data": 5, "engineer": 5, "manager": 5})
    high_base_job = _job("AI Data Engineer Manager", company="BMW AG")
    result = _score([high_base_job], features=fi)[0]
    assert result["relevance_score"] <= 100


def test_empty_feature_index_no_adjustments():
    fi = _fi()
    job = _job("AI Data Engineer Manager")
    base = _score([job])[0]["relevance_score"]
    with_empty = _score([job], features=fi)[0]
    assert with_empty["relevance_score"] == base
    assert with_empty.get("feedback_adjustments", []) == []
