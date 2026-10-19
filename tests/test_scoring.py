"""Tests for score_jobs (utils/pipeline.py): the Tier-1 role-term prefilter and
the Tier-2 Claude scoring against the fit profile."""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import utils.pipeline as pl
from utils.pipeline import JobScore, ScoreBatch, score_jobs
from utils.profile import matched_role_terms

ROLE_TERMS = ["ki", "ai", "data", "automatisierung", "digitalisierung", "machine learning"]


def _make_job(title: str, description: str = "", url: str = "https://example.com/1") -> dict:
    return {
        "title": title,
        "company": "Test GmbH",
        "location": "Hamburg, Deutschland",
        "url": url,
        "description": description,
        "source": "test",
    }


def _score(jobs, profile_text=""):
    return score_jobs(jobs, role_terms=ROLE_TERMS, profile_text=profile_text)


# ---------------------------------------------------------------- role terms

def test_role_terms_match_on_word_boundaries():
    assert matched_role_terms("KI Manager (m/w/d)", ROLE_TERMS) == ["ki"]
    assert matched_role_terms("Senior AI Engineer", ROLE_TERMS) == ["ai"]
    # "ai" inside "detail", "ki" inside "Kiel" must not count
    assert matched_role_terms("Detailplaner in Kiel", ROLE_TERMS) == []


def test_role_terms_match_multi_word_and_umlauts():
    hits = matched_role_terms("Machine Learning und Automatisierung", ROLE_TERMS)
    assert set(hits) == {"machine learning", "automatisierung"}


# ---------------------------------------------------------------- Tier 1

def test_role_term_in_title_scores_at_least_40():
    result = _score([_make_job("KI Projektleiter (m/w/d)")])
    assert result[0]["relevance_score"] >= 40


def test_title_hit_with_empty_description_scores_at_least_30():
    result = _score([_make_job("Digitalisierung Referent", description="")])
    assert result[0]["relevance_score"] >= 30


def test_irrelevant_job_scores_zero():
    result = _score([_make_job("Kfz-Mechatroniker (m/w/d)")])
    assert result[0]["relevance_score"] == 0
    assert result[0]["matched_keywords"] == []


def test_description_hits_add_points():
    plain = _score([_make_job("AI Engineer", url="https://example.com/1")])[0]["relevance_score"]
    rich = _score([_make_job("AI Engineer", description="data and Automatisierung", url="https://example.com/2")])[0]
    assert rich["relevance_score"] > plain
    assert {"ai", "data", "automatisierung"} <= set(rich["matched_keywords"])


def test_higher_score_comes_first():
    jobs = [
        _make_job("Hausmeister", url="https://example.com/low"),
        _make_job("AI Data Engineer", url="https://example.com/high"),
    ]
    result = _score(jobs)
    assert result[0]["url"] == "https://example.com/high"


def test_score_capped_at_100():
    job = _make_job("KI AI Data Automatisierung Digitalisierung", description="ki ai data machine learning")
    assert _score([job])[0]["relevance_score"] <= 100


def test_prefilter_score_is_kept_for_the_scoreboard():
    job = _score([_make_job("AI Engineer")])[0]
    assert job["prefilter_score"] == job["relevance_score"]


# ---------------------------------------------------------------- Tier 2

def test_no_profile_skips_claude(capsys):
    with patch.object(pl, "_score_jobs_claude") as claude:
        result = _score([_make_job("AI Engineer")], profile_text="")
    claude.assert_not_called()
    assert result[0]["scoring_method"] == "keyword_fallback"
    assert "[WARN]" in capsys.readouterr().out


def test_below_threshold_skips_claude():
    with patch.object(pl, "_score_jobs_claude") as claude:
        _score([_make_job("Hausmeister")], profile_text="profile")
    claude.assert_not_called()


def _fake_client(results_for):
    """A client whose messages.parse echoes back one JobScore per posting id."""
    import json

    def parse(**kwargs):
        postings = json.loads(kwargs["messages"][0]["content"])
        return SimpleNamespace(
            parsed_output=ScoreBatch(results=[results_for(p) for p in postings]),
            stop_reason="end_turn",
        )

    client = MagicMock()
    client.messages.parse.side_effect = parse
    return client


def test_claude_scores_against_the_profile(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    client = _fake_client(lambda p: JobScore(
        id=p["id"], score=8, reason="Fits.", situation_fit="First AI hire, reports to the CEO.",
        skills=["Python", "LLMs", "Automation", "extra"],
    ))
    with patch.object(pl.anthropic, "Anthropic", return_value=client):
        job = _score([_make_job("AI Engineer")], profile_text="PROFILE TEXT")[0]

    assert job["relevance_score"] == 80
    assert job["scoring_method"] == "claude"
    assert job["situation_fit"] == "First AI hire, reports to the CEO."
    assert job["ai_matched_skills"] == ["Python", "LLMs", "Automation"]
    kwargs = client.messages.parse.call_args.kwargs
    assert kwargs["model"] == pl.DEFAULT_SCORING_MODEL
    assert "PROFILE TEXT" in kwargs["system"]
    assert kwargs["output_format"] is ScoreBatch


def test_claude_failure_keeps_tier1_score(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    client = MagicMock()
    client.messages.parse.side_effect = RuntimeError("boom")
    with patch.object(pl.anthropic, "Anthropic", return_value=client):
        job = _score([_make_job("AI Engineer")], profile_text="PROFILE")[0]
    assert job["relevance_score"] >= 40
    assert job["scoring_method"] == "keyword_fallback"
    assert "[WARN]" in capsys.readouterr().out


def test_claude_refusal_keeps_tier1_score(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    client = MagicMock()
    client.messages.parse.return_value = SimpleNamespace(parsed_output=None, stop_reason="refusal")
    with patch.object(pl.anthropic, "Anthropic", return_value=client):
        job = _score([_make_job("AI Engineer")], profile_text="PROFILE")[0]
    assert job["scoring_method"] == "keyword_fallback"


def test_missing_api_key_skips_claude(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    job = _score([_make_job("AI Engineer")], profile_text="PROFILE")[0]
    assert job["scoring_method"] == "keyword_fallback"


# ---------------------------------------------------------------- settings

def test_settings_are_read_at_call_time(monkeypatch):
    """.env is loaded after import, so the settings must not be frozen at import."""
    monkeypatch.setenv("SCORING_MODEL", "claude-sonnet-5-5")
    monkeypatch.setenv("CLAUDE_SCORE_THRESHOLD", "90")
    assert pl.scoring_model() == "claude-sonnet-5-5"
    assert pl.score_threshold() == 90
    with patch.object(pl, "_score_jobs_claude") as claude:
        _score([_make_job("AI Engineer")], profile_text="PROFILE")  # Tier 1 = 40 < 90
    claude.assert_not_called()


def test_settings_defaults(monkeypatch):
    monkeypatch.delenv("SCORING_MODEL", raising=False)
    monkeypatch.delenv("CLAUDE_SCORE_THRESHOLD", raising=False)
    assert pl.scoring_model() == "claude-haiku-5-5"
    assert pl.score_threshold() == 20


def test_long_role_terms_match_inside_german_compounds():
    terms = ["ki", "digitalisierung", "automatisierung", "geschäftsführung", "projektleit"]
    assert matched_role_terms("Digitalisierungsmanager (m/w/d)", terms) == ["digitalisierung"]
    assert matched_role_terms("Prozessautomatisierung Specialist", terms) == ["automatisierung"]
    assert matched_role_terms("Referent (m/w/d) der Geschäftsführung", terms) == ["geschäftsführung"]
    assert matched_role_terms("IT-Projektleiter (m/w/d)", terms) == ["projektleit"]
    # short terms keep their word boundaries
    assert matched_role_terms("Kieler Kinderbetreuung", terms) == []



def test_sentence_long_skill_tags_are_dropped(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    client = _fake_client(lambda p: JobScore(
        id=p["id"], score=7, reason="r", situation_fit="s",
        skills=["Prestige for its own sake is an anti-signal for this candidate", "Python", "LLM integration"],
    ))
    with patch.object(pl.anthropic, "Anthropic", return_value=client):
        job = _score([_make_job("AI Engineer")], profile_text="PROFILE")[0]
    assert job["ai_matched_skills"] == ["Python", "LLM integration"]
