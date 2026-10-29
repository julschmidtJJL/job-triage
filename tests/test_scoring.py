"""Tests for score_jobs (utils/pipeline.py): the Tier-1 role-term prefilter and
the Tier-2 Claude scoring against the fit profile."""
import sys
from pathlib import Path
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import utils.pipeline as pl
from utils.pipeline import JobScore, ScoreBatch, UnmetRequirement, score_jobs
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


def _result(**fields) -> JobScore:
    """A JobScore with empty ad facts unless a test sets them."""
    defaults = dict(
        reason="r", situation_fit="s", skills=[], ad_language="", contact_person="", seniority="",
        required_years=None, candidate_years=None, hard_requirements=[], requirements_flexible=False, unmet_requirements=[],
    )
    return JobScore(**{**defaults, **fields})


def _unmet(requirement: str, kind: str, evidence: str = "") -> UnmetRequirement:
    return UnmetRequirement(requirement=requirement, kind=kind, profile_evidence=evidence)


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
    client = _fake_client(lambda p: _result(
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
    client = _fake_client(lambda p: _result(
        id=p["id"], score=7, reason="r", situation_fit="s",
        skills=["Prestige for its own sake is an anti-signal for this candidate", "Python", "LLM integration"],
    ))
    with patch.object(pl.anthropic, "Anthropic", return_value=client):
        job = _score([_make_job("AI Engineer")], profile_text="PROFILE")[0]
    assert job["ai_matched_skills"] == ["Python", "LLM integration"]


# ---------------------------------------------------------------- full ad

CONSTRUCTION_AD = (
    "Projektmanager Unternehmensentwicklung (m/w/d). Wir sind ein Bauunternehmen. "
    "Das bringst du mit: mindestens 3 Jahre Berufserfahrung auf der Baustelle, zwingend erforderlich."
)


def test_scorer_reads_the_full_ad_and_the_board_keeps_it(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    client = _fake_client(lambda p: _result(id=p["id"], score=7))
    job = _make_job("KI Projektmanager", description="Snippet.")
    job["source"] = "good_jobs"
    with patch.object(pl.anthropic, "Anthropic", return_value=client), \
         patch("utils.job_ad.fetch_ad_page", return_value=CONSTRUCTION_AD):
        job = _score([job], profile_text="profile")[0]

    posting = json.loads(client.messages.parse.call_args.kwargs["messages"][0]["content"])[0]
    assert posting["description"] == CONSTRUCTION_AD
    assert posting["description_kind"] == "full ad"
    assert job["description"] == CONSTRUCTION_AD
    assert job["description_snippet"] == "Snippet."
    assert job["fetch_method"] == "full_jd"


def test_failed_fetch_tells_the_scorer_it_is_a_snippet(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    client = _fake_client(lambda p: _result(id=p["id"], score=7))
    job = _make_job("KI Manager", description="Snippet.")
    with patch.object(pl.anthropic, "Anthropic", return_value=client), \
         patch.object(pl, "fetch_full_description", return_value=("", "snippet")):
        job = _score([job], profile_text="profile")[0]
    posting = json.loads(client.messages.parse.call_args.kwargs["messages"][0]["content"])[0]
    assert posting["description_kind"] == "snippet" and posting["description"] == "Snippet."
    assert job["fetch_method"] == "snippet" and "description_snippet" not in job


def test_ad_facts_reach_the_job(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    client = _fake_client(lambda p: _result(
        id=p["id"], score=6, ad_language="German", contact_person="Anna Beispiel, Leiterin Personal",
        seniority="Mid", required_years=3, hard_requirements=["construction-site experience", "Bauingenieur degree"],
        requirements_flexible=False, unmet_requirements=[],
    ))
    with patch.object(pl.anthropic, "Anthropic", return_value=client), \
         patch.object(pl, "fetch_full_description", return_value=(CONSTRUCTION_AD, "full_jd")):
        job = _score([_make_job("KI Manager")], profile_text="profile")[0]
    assert job["ad_language"] == "German"
    assert job["contact_person"] == "Anna Beispiel, Leiterin Personal"
    assert job["seniority"] == "Mid" and job["required_years"] == 3
    assert job["hard_requirements"] == ["construction-site experience", "Bauingenieur degree"]
    assert job["unmet_requirements"] == [] and job["relevance_score"] == 60


def test_unmet_hard_requirement_caps_the_score(monkeypatch):
    """70 on a title snippet, but the ad requires years on construction
    sites, which the candidate does not have."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    client = _fake_client(lambda p: _result(
        id=p["id"], score=7, hard_requirements=["3+ years construction-site experience"],
        unmet_requirements=[_unmet("3+ years construction-site experience", "domain")],
    ))
    with patch.object(pl.anthropic, "Anthropic", return_value=client), \
         patch.object(pl, "fetch_full_description", return_value=(CONSTRUCTION_AD, "full_jd")):
        job = _score([_make_job("KI Manager")], profile_text="profile")[0]
    assert job["relevance_score"] == pl.UNMET_REQUIREMENT_CAP * 10
    assert job["unmet_requirements"] == ["3+ years construction-site experience"]


def test_flexible_ad_is_not_capped(monkeypatch):
    """The ad says nobody needs every listed skill."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    client = _fake_client(lambda p: _result(
        id=p["id"], score=8, hard_requirements=["Kubernetes", "team lead experience"],
        requirements_flexible=True, unmet_requirements=[_unmet("Leading an ML platform team", "years")],
    ))
    with patch.object(pl.anthropic, "Anthropic", return_value=client), \
         patch.object(pl, "fetch_full_description", return_value=("Head of ML ad", "full_jd")):
        job = _score([_make_job("Head of Machine Learning")], profile_text="profile")[0]
    assert job["relevance_score"] == 80
    assert job["requirements_flexible"] is True


def test_the_profile_decides_what_a_fit_is_not_the_title(monkeypatch):
    """A profile's own fit questions (profile.example.md, "What counts as a fit")
    reach the scorer, which is told to weigh them above the title."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    profile = (Path(__file__).parent.parent / "profile" / "profile.example.md").read_text(encoding="utf-8")
    assert "## What counts as a fit" in profile
    client = _fake_client(lambda p: _result(id=p["id"], score=5))
    with patch.object(pl.anthropic, "Anthropic", return_value=client), \
         patch.object(pl, "fetch_full_description", return_value=("ad", "full_jd")):
        _score([_make_job("KI Manager")], profile_text=profile)
    system = client.messages.parse.call_args.kwargs["system"]
    assert "What would I learn?" in system
    assert "regardless of the job title" in system
    assert "Neither the industry nor the job title matters on its own" in system


def test_prompt_asks_for_judgement_not_keyword_counting(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    client = _fake_client(lambda p: _result(id=p["id"], score=5))
    with patch.object(pl.anthropic, "Anthropic", return_value=client), \
         patch.object(pl, "fetch_full_description", return_value=("ad", "full_jd")):
        _score([_make_job("KI Manager")], profile_text="profile")
    system = client.messages.parse.call_args.kwargs["system"]
    assert "requirements_flexible" in system
    assert "does not mention is not unmet" in system
    assert "Do not count keywords" in system


def test_without_claude_language_and_years_come_from_the_text(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ad = ("We are looking for a data lead. You will own the platform and you will work with the "
          "CTO. You bring 5+ years of experience in data engineering and you are fluent in English.")
    with patch.object(pl, "fetch_full_description", return_value=(ad, "full_jd")):
        job = _score([_make_job("Data Lead")], profile_text="profile")[0]
    assert job["scoring_method"] == "keyword_fallback"
    assert job["ad_language"] == "English" and job["required_years"] == 5
    assert job["description"] == ad


# ---------------------------------------------------------------- fetch routing

def test_held_over_job_is_not_fetched_again():
    job = {"url": "https://x/1", "source": "good_jobs", "fetch_method": "full_jd", "description": "Full ad."}
    with patch.object(pl, "fetch_full_description") as fetch:
        pl._attach_full_ad(job)
    fetch.assert_not_called()
    assert job["description"] == "Full ad."


def test_fetch_routes_by_source():
    with patch("scrapers.arbeitsagentur.fetch_description", return_value="API ad") as api:
        assert pl.fetch_full_description("https://www.arbeitsagentur.de/jobsuche/jobdetail/1", "arbeitsagentur") == ("API ad", "full_jd")
    api.assert_called_once()
    with patch("utils.job_ad.fetch_ad_page", return_value="Board ad") as page:
        assert pl.fetch_full_description("https://goodjobs.eu/jobs/x", "good_jobs") == ("Board ad", "full_jd")
        assert pl.fetch_full_description("https://new-board.example/1", "a_new_source") == ("Board ad", "full_jd")
    assert page.call_count == 2
    with patch("utils.job_ad.fetch_ad_page") as page:
        assert pl.fetch_full_description("https://google.com/x", "google_jobs") == ("", "snippet")
        assert pl.fetch_full_description("", "good_jobs") == ("", "snippet")
    page.assert_not_called()


def test_a_source_can_bring_its_own_fetcher(monkeypatch):
    monkeypatch.setitem(pl.FETCHERS, "my_board", None)
    assert pl.fetch_full_description("https://my-board.example/1", "my_board") == ("", "snippet")
    pl.register_fetcher("my_board", lambda url: f"Ad from {url}")
    assert pl.fetch_full_description("https://my-board.example/1", "my_board") == ("Ad from https://my-board.example/1", "full_jd")
    pl.register_fetcher("my_board", "utils.job_ad:html_to_text")
    assert pl.fetch_full_description("<p>Hallo</p>", "my_board") == ("Hallo", "full_jd")


# ---------------------------------------------------------------- tools are learnable

AZURE_AD = (Path(__file__).parent / "fixtures" / "azure_must_have_ad.txt").read_text(encoding="utf-8")
PROFILE_WITHOUT_AZURE = (
    "Builds LLM integrations and process automation in Python for Mittelstand companies. "
    "Five years of hands-on work with data pipelines and APIs; deploys on Hetzner and Vercel."
)


def _score_azure_ad(monkeypatch, unmet, score=9):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    client = _fake_client(lambda p: _result(
        id=p["id"], score=score, hard_requirements=["Azure AI und Cloud-Architekturen", "Python"],
        unmet_requirements=unmet,
    ))
    with patch.object(pl.anthropic, "Anthropic", return_value=client), \
         patch.object(pl, "fetch_full_description", return_value=(AZURE_AD, "full_jd")):
        job = _score([_make_job("KI-Engineer")], profile_text=PROFILE_WITHOUT_AZURE)[0]
    return job, client


def test_missing_azure_is_a_tool_gap_not_an_unmet_requirement(monkeypatch):
    """A strong fit must not drop from 90 to 50 because Azure is not in the profile."""
    job, client = _score_azure_ad(monkeypatch, [_unmet("Azure AI und Cloud-Architekturen", "tool")])
    assert "zwingend" in AZURE_AD and "Azure" in client.messages.parse.call_args.kwargs["messages"][0]["content"]
    assert job["unmet_requirements"] == []
    assert job["tool_gaps"] == ["Azure AI und Cloud-Architekturen"]
    assert job["relevance_score"] == 90


def test_a_tool_filed_under_another_kind_is_still_a_tool_gap(monkeypatch):
    job, _ = _score_azure_ad(monkeypatch, [_unmet("Azure AI und Cloud-Architekturen", "domain")])
    assert job["unmet_requirements"] == [] and job["relevance_score"] == 90


def test_made_up_profile_evidence_does_not_count(monkeypatch):
    job, _ = _score_azure_ad(monkeypatch, [
        _unmet("Azure AI und Cloud-Architekturen", "tool", evidence="The candidate has never used Azure."),
    ])
    assert job["unmet_requirements"] == [] and job["relevance_score"] == 90


def test_a_tool_the_profile_rules_out_is_unmet(monkeypatch):
    profile = PROFILE_WITHOUT_AZURE + " Will not work with Microsoft Azure or other Microsoft clouds."
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    client = _fake_client(lambda p: _result(id=p["id"], score=9, unmet_requirements=[
        _unmet("Azure AI und Cloud-Architekturen", "tool", evidence="will not work with  Microsoft Azure"),
    ]))
    with patch.object(pl.anthropic, "Anthropic", return_value=client), \
         patch.object(pl, "fetch_full_description", return_value=(AZURE_AD, "full_jd")):
        job = _score([_make_job("KI-Engineer")], profile_text=profile)[0]
    assert job["unmet_requirements"] == ["Azure AI und Cloud-Architekturen"]
    assert job["relevance_score"] == pl.UNMET_REQUIREMENT_CAP * 10


def test_years_language_and_degree_still_cap_even_when_they_name_a_tool(monkeypatch):
    for requirement, kind in [("5+ Jahre SAP FI/CO", "years"), ("Verhandlungssicheres Deutsch", "language"),
                              ("Studium der Informatik", "degree")]:
        job, _ = _score_azure_ad(monkeypatch, [_unmet(requirement, kind)])
        assert job["unmet_requirements"] == [requirement]
        assert job["relevance_score"] == pl.UNMET_REQUIREMENT_CAP * 10


def test_prompt_says_tools_are_learnable(monkeypatch):
    _, client = _score_azure_ad(monkeypatch, [])
    system = client.messages.parse.call_args.kwargs["system"]
    assert "learnable on the job" in system and "lowers the score by at most 1" in system


# ---------------------------------------------------------------- years are compared in code

def _score_years(monkeypatch, required, has, flexible=False, unmet=()):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    client = _fake_client(lambda p: _result(
        id=p["id"], score=8, required_years=required, candidate_years=has,
        requirements_flexible=flexible, unmet_requirements=list(unmet),
    ))
    with patch.object(pl.anthropic, "Anthropic", return_value=client), \
         patch.object(pl, "fetch_full_description", return_value=("5+ Jahre Disposition", "full_jd")):
        return _score([_make_job("Data Analyst Logistik")], profile_text="profile")[0]


def test_fewer_years_than_asked_is_unmet_even_if_the_list_forgets_it(monkeypatch):
    """The model's own list is not stable from run to run; the years check is."""
    job = _score_years(monkeypatch, required=5, has=2)
    assert job["unmet_requirements"] == ["5+ years asked for, profile shows 2"]
    assert job["relevance_score"] == pl.UNMET_REQUIREMENT_CAP * 10


def test_enough_years_or_no_answer_is_not_unmet(monkeypatch):
    assert _score_years(monkeypatch, required=5, has=6)["relevance_score"] == 80
    assert _score_years(monkeypatch, required=5, has=None)["relevance_score"] == 80
    assert _score_years(monkeypatch, required=None, has=0)["relevance_score"] == 80


def test_years_are_not_listed_twice(monkeypatch):
    job = _score_years(monkeypatch, required=5, has=2, unmet=[_unmet("5+ Jahre Disposition", "years")])
    assert job["unmet_requirements"] == ["5+ Jahre Disposition"]


def test_flexible_ad_lists_missing_years_without_capping(monkeypatch):
    job = _score_years(monkeypatch, required=5, has=2, flexible=True)
    assert job["unmet_requirements"] and job["relevance_score"] == 80
