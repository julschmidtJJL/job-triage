import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from notion_sync.sync import _build_properties, _get_existing_urls, sync_jobs

SAMPLE_JOB = {
    "title": "Senior Data Analyst",
    "company": "Acme GmbH",
    "location": "Berlin",
    "date_posted": "2024-01-15",
    "url": "https://linkedin.com/jobs/12345",
    "description": "Python SQL Kenntnisse gesucht.",
    "source": "linkedin",
    "relevance_score": 80,
    "matched_keywords": ["Data Analyst", "Python", "SQL"],
}

ENV = {
    "NOTION_API_KEY": "test-api-key",
    "NOTION_JOB_BOARD_DB_ID": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "NOTION_DATA_SOURCE_ID": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
}


def _make_mock_notion(existing_urls: list = None):
    """Build a fully mocked Notion client."""
    mock_notion = MagicMock()
    results = []
    for url in (existing_urls or []):
        results.append({"properties": {"URL": {"url": url}}})
    mock_notion.data_sources.query.return_value = {"results": results, "has_more": False}
    mock_notion.pages.create.return_value = {"id": "new-page-id"}
    return mock_notion


# ---------------------------------------------------------------------------
# _build_properties
# ---------------------------------------------------------------------------


def test_build_properties_source_mapping_linkedin():
    props = _build_properties(SAMPLE_JOB)
    assert props["Portal"]["select"]["name"] == "LinkedIn"


def test_build_properties_source_mapping_google_jobs():
    job = {**SAMPLE_JOB, "source": "google_jobs"}
    props = _build_properties(job)
    assert props["Portal"]["select"]["name"] == "Google Jobs"


def test_build_properties_keywords_capped_at_5():
    job = {**SAMPLE_JOB, "matched_keywords": [f"kw{i}" for i in range(10)]}
    props = _build_properties(job)
    assert len(props["Matched Keywords"]["multi_select"]) == 5


def test_build_properties_status_is_new():
    props = _build_properties(SAMPLE_JOB)
    assert props["Status"]["select"]["name"] == "New"


# ---------------------------------------------------------------------------
# _get_existing_urls
# ---------------------------------------------------------------------------


def test_get_existing_urls_returns_url_set():
    mock_notion = _make_mock_notion(["https://existing.com/1", "https://existing.com/2"])
    result = _get_existing_urls(mock_notion, "db-id")
    assert result == {"https://existing.com/1", "https://existing.com/2"}


def test_get_existing_urls_returns_empty_on_exception():
    mock_notion = MagicMock()
    mock_notion.data_sources.query.side_effect = Exception("API-Fehler")
    result = _get_existing_urls(mock_notion, "ds-id")
    assert result == set()


# ---------------------------------------------------------------------------
# Happy Path: sync_jobs
# ---------------------------------------------------------------------------


def test_sync_jobs_creates_new_page():
    mock_notion = _make_mock_notion()
    with patch("notion_sync.sync.Client", return_value=mock_notion):
        with patch.dict("os.environ", ENV):
            with patch("notion_sync.sync.time.sleep"):
                result = sync_jobs([SAMPLE_JOB])

    mock_notion.pages.create.assert_called_once()
    assert result["added"] == 1
    assert result["skipped"] == 0


def test_sync_jobs_returns_added_and_skipped():
    mock_notion = _make_mock_notion()
    jobs = [
        {**SAMPLE_JOB, "url": "https://linkedin.com/1"},
        {**SAMPLE_JOB, "url": "https://linkedin.com/2", "title": "Data Engineer"},
    ]
    with patch("notion_sync.sync.Client", return_value=mock_notion):
        with patch.dict("os.environ", ENV):
            with patch("notion_sync.sync.time.sleep"):
                result = sync_jobs(jobs)

    assert result["added"] == 2
    assert result["skipped"] == 0


# ---------------------------------------------------------------------------
# Deduplizierung
# ---------------------------------------------------------------------------


def test_sync_jobs_skips_existing_url():
    existing = [SAMPLE_JOB["url"]]
    mock_notion = _make_mock_notion(existing)
    with patch("notion_sync.sync.Client", return_value=mock_notion):
        with patch.dict("os.environ", ENV):
            with patch("notion_sync.sync.time.sleep"):
                result = sync_jobs([SAMPLE_JOB])

    mock_notion.pages.create.assert_not_called()
    assert result["added"] == 0
    assert result["skipped"] == 1


def test_sync_jobs_mixed_new_and_duplicate():
    existing_url = "https://linkedin.com/existing"
    mock_notion = _make_mock_notion([existing_url])
    jobs = [
        {**SAMPLE_JOB, "url": existing_url},
        {**SAMPLE_JOB, "url": "https://linkedin.com/new"},
    ]
    with patch("notion_sync.sync.Client", return_value=mock_notion):
        with patch.dict("os.environ", ENV):
            with patch("notion_sync.sync.time.sleep"):
                result = sync_jobs(jobs)

    assert result["added"] == 1
    assert result["skipped"] == 1


# ---------------------------------------------------------------------------
# Rate Limiting
# ---------------------------------------------------------------------------


def test_sync_jobs_sleep_called_per_new_page():
    mock_notion = _make_mock_notion()
    jobs = [
        {**SAMPLE_JOB, "url": "https://linkedin.com/1"},
        {**SAMPLE_JOB, "url": "https://linkedin.com/2", "title": "Data Engineer"},
    ]
    with patch("notion_sync.sync.Client", return_value=mock_notion):
        with patch.dict("os.environ", ENV):
            with patch("notion_sync.sync.time.sleep") as mock_sleep:
                sync_jobs(jobs)

    assert mock_sleep.call_count == 2
    mock_sleep.assert_called_with(0.4)


# ---------------------------------------------------------------------------
# Fehlerbehandlung
# ---------------------------------------------------------------------------


def test_sync_jobs_no_crash_if_page_create_fails(capsys):
    mock_notion = _make_mock_notion()
    mock_notion.pages.create.side_effect = Exception("Rate limit exceeded")
    with patch("notion_sync.sync.Client", return_value=mock_notion):
        with patch.dict("os.environ", ENV):
            with patch("notion_sync.sync.time.sleep"):
                result = sync_jobs([SAMPLE_JOB])

    assert result["added"] == 0
    assert result["failed"] == 1
    assert "[ERROR]" in capsys.readouterr().out


def test_sync_jobs_returns_zero_if_credentials_missing(monkeypatch, capsys):
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    monkeypatch.delenv("NOTION_JOB_BOARD_DB_ID", raising=False)
    monkeypatch.delenv("NOTION_DATA_SOURCE_ID", raising=False)
    result = sync_jobs([SAMPLE_JOB])
    assert result["added"] == 0 and result["failed"] == 1  # counts as failed, so the run goes red
    assert result["failed_jobs"] == [SAMPLE_JOB]
    assert "[ERROR]" in capsys.readouterr().out


def test_sync_jobs_empty_input():
    mock_notion = _make_mock_notion()
    with patch("notion_sync.sync.Client", return_value=mock_notion):
        with patch.dict("os.environ", ENV):
            with patch("notion_sync.sync.time.sleep"):
                result = sync_jobs([])

    assert result == {"added": 0, "skipped": 0, "failed": 0, "failed_jobs": []}


def test_options_with_commas_are_cleaned():
    """Notion rejects a whole page if one select option contains a comma."""
    job = dict(SAMPLE_JOB, ai_matched_skills=["LLM APIs (OpenAI, Claude)"],
               matched_keywords=["ai, data"], query="KI, Manager")
    props = _build_properties(job)
    names = [o["name"] for o in props["AI Matched Skills"]["multi_select"]]
    names += [o["name"] for o in props["Matched Keywords"]["multi_select"]]
    names.append(props["Query"]["select"]["name"])
    assert names == ["LLM APIs (OpenAI / Claude)", "ai / data", "KI / Manager"]
    assert all("," not in n for n in names)


def test_reposted_job_with_new_link_is_a_duplicate():
    """Employers repost on LinkedIn under a new job ID; same title + company
    already on the board must not create a second row."""
    page = {"properties": {
        "URL": {"url": "https://linkedin.com/jobs/old-id"},
        "Job Title": {"title": [{"plain_text": "Senior Data Analyst (m/w/d)"}]},
        "Company": {"rich_text": [{"plain_text": "Acme GmbH"}]},
    }}
    mock_notion = MagicMock()
    mock_notion.data_sources.query.return_value = {"results": [page], "has_more": False}
    repost = {**SAMPLE_JOB, "url": "https://linkedin.com/jobs/new-id", "title": "Senior Data Analyst"}
    with patch("notion_sync.sync.Client", return_value=mock_notion):
        with patch.dict("os.environ", ENV):
            with patch("notion_sync.sync.time.sleep"):
                result = sync_jobs([repost])
    assert result["added"] == 0 and result["skipped"] == 1
    mock_notion.pages.create.assert_not_called()


def test_same_title_at_another_company_is_not_a_duplicate():
    page = {"properties": {
        "URL": {"url": "https://linkedin.com/jobs/1"},
        "Job Title": {"title": [{"plain_text": "Data Engineer"}]},
        "Company": {"rich_text": [{"plain_text": "Other AG"}]},
    }}
    mock_notion = MagicMock()
    mock_notion.data_sources.query.return_value = {"results": [page], "has_more": False}
    with patch("notion_sync.sync.Client", return_value=mock_notion):
        with patch.dict("os.environ", ENV):
            with patch("notion_sync.sync.time.sleep"):
                result = sync_jobs([{**SAMPLE_JOB, "title": "Data Engineer", "url": "https://linkedin.com/jobs/2"}])
    assert result["added"] == 1


# ---------------------------------------------------------------------------
# Full ad and what it says
# ---------------------------------------------------------------------------

from notion_sync.sync import AD_PROPERTIES, _build_children, ensure_ad_properties

AD_JOB = {
    **SAMPLE_JOB,
    "scoring_method": "claude",
    "fetch_method": "full_jd",
    "description": "Über uns\n\nWir bauen.\n\n" + ("Aufgaben. " * 400),
    "ad_language": "German",
    "contact_person": "Anna Beispiel, Leiterin Personal",
    "seniority": "Mid",
    "required_years": 3,
    "hard_requirements": ["3+ years construction-site experience", "Bauingenieur degree"],
    "requirements_flexible": False,
    "unmet_requirements": ["3+ years construction-site experience"],
}


def _text(prop):
    return prop["rich_text"][0]["text"]["content"]


def test_ad_facts_become_properties():
    props = _build_properties(AD_JOB)
    assert _text(props["Contact Person"]) == "Anna Beispiel, Leiterin Personal"
    assert props["Ad Language"] == {"select": {"name": "German"}}
    assert props["Seniority"] == {"select": {"name": "Mid"}}
    assert props["Required Years"] == {"number": 3}
    assert _text(props["Hard Requirements"]) == "3+ years construction-site experience; Bauingenieur degree"
    assert _text(props["Unmet Requirements"]) == "3+ years construction-site experience"
    assert props["Requirements Flexible"] == {"checkbox": False}


def test_empty_ad_facts_are_left_out():
    props = _build_properties({**SAMPLE_JOB, "scoring_method": "keyword_fallback"})
    assert not set(AD_PROPERTIES) & set(props)


def test_properties_the_board_lacks_are_left_out():
    props = _build_properties(AD_JOB, ad_properties={"Ad Language"})
    assert "Ad Language" in props and "Contact Person" not in props
    assert "Job Title" in props  # the existing properties are untouched


def test_page_body_carries_requirements_and_the_full_ad():
    blocks = _build_children(AD_JOB)
    texts = [b[b["type"]]["rich_text"][0]["text"]["content"] for b in blocks if b["type"] != "divider"]
    assert "Requirements" in texts and "Job Ad" in texts
    requirements = texts[texts.index("Requirements") + 1]
    assert "Not met: 3+ years construction-site experience" in requirements
    assert "Contact: Anna Beispiel" in requirements
    ad = texts[texts.index("Job Ad") + 1:]
    assert ad[0].startswith("Über uns\n\nWir bauen.")
    assert all(len(t) <= 2000 for t in ad)
    assert "".join(ad).replace("\n", "").replace(" ", "") == AD_JOB["description"].replace("\n", "").replace(" ", "")
    assert len(blocks) <= 100


def test_snippet_rows_say_so():
    blocks = _build_children({**SAMPLE_JOB, "fetch_method": "snippet"})
    headings = [b["heading_3"]["rich_text"][0]["text"]["content"] for b in blocks if b["type"] == "heading_3"]
    assert "Job Ad (search snippet only)" in headings


def test_ensure_ad_properties_adds_only_the_missing_ones():
    notion = MagicMock()
    notion.data_sources.retrieve.return_value = {"properties": {"Job Title": {}, "Ad Language": {}}}
    assert ensure_ad_properties(notion, "ds") == set(AD_PROPERTIES)
    added = notion.data_sources.update.call_args.kwargs["properties"]
    assert "Ad Language" not in added
    assert added["Required Years"] == {"number": {}} and added["Requirements Flexible"] == {"checkbox": {}}


def test_ensure_ad_properties_is_a_no_op_when_complete():
    notion = MagicMock()
    notion.data_sources.retrieve.return_value = {"properties": dict.fromkeys(AD_PROPERTIES, {})}
    assert ensure_ad_properties(notion, "ds") == set(AD_PROPERTIES)
    notion.data_sources.update.assert_not_called()


def test_rows_still_sync_when_the_schema_cannot_be_changed(capsys):
    notion = _make_mock_notion()
    notion.data_sources.retrieve.return_value = {"properties": {"Ad Language": {}}}
    notion.data_sources.update.side_effect = Exception("insufficient permissions")
    with patch.dict("os.environ", ENV), \
         patch("notion_sync.sync.Client", return_value=notion), \
         patch("notion_sync.sync.time.sleep"):
        result = sync_jobs([AD_JOB])
    assert result["added"] == 1
    props = notion.pages.create.call_args.kwargs["properties"]
    assert "Ad Language" in props and "Contact Person" not in props
    assert "Could not add Notion properties" in capsys.readouterr().out


def test_tool_gaps_are_shown_but_not_as_unmet():
    blocks = _build_children({**AD_JOB, "unmet_requirements": [], "tool_gaps": ["Azure AI"]})
    texts = [b[b["type"]]["rich_text"][0]["text"]["content"] for b in blocks if b["type"] != "divider"]
    requirements = texts[texts.index("Requirements") + 1]
    assert "Tools the profile does not list (not counted against the fit): Azure AI" in requirements
    assert "Not met" not in requirements
    assert "Unmet Requirements" not in _build_properties({**AD_JOB, "unmet_requirements": []})
