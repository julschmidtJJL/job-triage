"""Tests for utils/feedback_features.py — FeatureIndex construction with
per-pipeline filtering and signal weighting."""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from notion_sync.feedback import Decision, DecisionHistory
from utils.feedback_features import build_feature_index


def _dec(title, decision, pipeline="main", company="Test GmbH"):
    return Decision(
        title=title, company=company, decision=decision,
        decided_at=datetime.now(timezone.utc), pipeline=pipeline,
    )


def _history(**kwargs) -> DecisionHistory:
    h = DecisionHistory()
    for bucket, items in kwargs.items():
        getattr(h, bucket).extend(items)
    return h


def test_empty_history_returns_empty_index():
    fi = build_feature_index(DecisionHistory(), "main")
    assert fi.is_empty()
    assert fi.pipeline == "main"


def test_applied_populates_positive_tokens_and_companies():
    h = _history(applied=[
        _dec("Senior Logistics Data Engineer", "Apply", company="BMW AG"),
        _dec("Logistics Engineer", "Applied", company="Siemens GmbH"),
    ])
    fi = build_feature_index(h, "main")
    assert fi.positive_title_tokens["logistics"] == 2
    # generic title words are never learned
    assert "data" not in fi.positive_title_tokens
    assert "engineer" not in fi.positive_title_tokens
    assert "bmw" in fi.positive_companies
    assert "siemens" in fi.positive_companies


def test_interested_only_adds_tokens_not_companies():
    """Interesting is a weaker positive — flagged but not applied."""
    h = _history(interested=[_dec("Finance Manager", "Interesting", company="ACME Ltd")])
    fi = build_feature_index(h, "main")
    assert "finance" in fi.positive_title_tokens
    assert "manager" not in fi.positive_title_tokens
    assert "acme" not in fi.positive_companies


def test_rejected_populates_negative_tokens_and_companies():
    h = _history(rejected=[
        _dec("Polierer", "Not interesting", company="Zeitarbeit XYZ GmbH"),
        _dec("Polierer (m/w/d)", "Not interesting", company="Zeitarbeit XYZ GmbH"),
    ])
    fi = build_feature_index(h, "main")
    assert fi.negative_title_tokens["polierer"] == 2
    assert fi.negative_companies["zeitarbeit xyz"] == 2


def test_declined_weighted_at_half():
    """Absagen weight = 0.5 — two Absagen = 1 effective count."""
    h = _history(declined=[
        _dec("Polierer", "Declined"),
        _dec("Polierer", "Declined"),
    ])
    fi = build_feature_index(h, "main")
    # 2 raw declines × 0.5 = 1 effective count (rounded)
    assert fi.negative_title_tokens["polierer"] == 1


def test_single_declined_still_counts_as_one():
    """Edge case: 1 raw decline shouldn't round down to zero — at least 1."""
    h = _history(declined=[_dec("Polierer", "Declined")])
    fi = build_feature_index(h, "main")
    assert fi.negative_title_tokens["polierer"] >= 1


def test_pipeline_filter_isolates_pipelines():
    """REGRESSION RISK: Tokens from one pipeline must NEVER influence another."""
    h = _history(
        rejected=[
            _dec("Pflegehelfer", "Not interesting", pipeline="other"),
            _dec("Polierer", "Not interesting", pipeline="main"),
        ]
    )
    main_fi = build_feature_index(h, "main")
    other_fi = build_feature_index(h, "other")
    # The main pipeline sees polierer but NOT pflegehelfer
    assert "polierer" in main_fi.negative_title_tokens
    assert "pflegehelfer" not in main_fi.negative_title_tokens
    # The other pipeline sees pflegehelfer but NOT polierer
    assert "pflegehelfer" in other_fi.negative_title_tokens
    assert "polierer" not in other_fi.negative_title_tokens


def test_summary_dict_shape():
    h = _history(applied=[_dec("Treasury Engineer", "Apply", company="BMW")])
    fi = build_feature_index(h, "main")
    s = fi.summary()
    assert s["positive_tokens"] > 0
    assert s["positive_companies"] == 1
    assert s["negative_tokens"] == 0



def test_generic_words_never_become_negative_signal():
    """The 16 Oct 2026 failure: 19 rejections taught manager / consultant /
    data / hamburg, which sit in most titles."""
    rejected = [_dec(t, "Not interesting", company=f"C{i}") for i, t in enumerate([
        "Senior Consultant Data Hamburg", "Data Manager Hamburg", "Consultant SAP Hamburg",
        "Manager Steuerberatung", "Consultant Risk Management",
    ])]
    fi = build_feature_index(_history(rejected=rejected), "main", vocabulary=frozenset({"data"}))
    for word in ("manager", "consultant", "data", "hamburg", "management", "senior"):
        assert word not in fi.negative_title_tokens
    assert fi.negative_title_tokens["steuerberatung"] == 1


def test_search_vocabulary_is_not_learned():
    h = _history(rejected=[_dec("Digitalisierung Prozessmanager", "Not interesting")] * 3)
    fi = build_feature_index(h, "main", vocabulary=frozenset({"digitalisierung", "prozessmanager"}))
    assert fi.negative_title_tokens == {}
