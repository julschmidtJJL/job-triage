"""
Tests fuer den Google Jobs ScaleSerp Scraper.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.google_jobs import (
    scrape_google_jobs,
    SCALESERP_URL,
    _is_listing_page,
    _extract_company,
)

REQUIRED_FIELDS = {"title", "company", "location", "date_posted", "url", "description", "source"}

# Single posting (not a listing)
MOCK_JOB_RESULT = {
    "organic_results": [
        {
            "title": "CFO (m/w/d) - Hamburger Logistik AG",
            "link": "https://www.xing.com/jobs/hamburg-cfo-m-w-d-99999",
            "domain": "www.xing.com",
            "snippet": "Wir suchen einen erfahrenen CFO fuer unser Unternehmen in Hamburg.",
        }
    ]
}

# Listing page (must be filtered out)
MOCK_LISTING_RESULT = {
    "organic_results": [
        {
            "title": "25 CFO Jobs in Hamburg | Jetzt bewerben",
            "link": "https://www.xing.com/jobs/cfo/in-hamburg",
            "domain": "www.xing.com",
            "snippet": "Alle CFO Stellen in Hamburg auf einen Blick.",
        }
    ]
}


def _mock_response(data=None, ok=True, status_code=200):
    if data is None:
        data = MOCK_JOB_RESULT
    resp = MagicMock()
    resp.ok = ok
    resp.status_code = status_code
    resp.json.return_value = data
    return resp


# ---------------------------------------------------------------------------
# Test 1: ScaleSerp URL format is built correctly
# ---------------------------------------------------------------------------

def test_scaleserp_url_is_called():
    """Requests go to api.scaleserp.com, not serpapi.com."""
    with patch("scrapers.google_jobs.requests.get", return_value=_mock_response()) as mock_get:
        with patch.dict("os.environ", {"SERPAPI_KEY": "test-key"}):
            scrape_google_jobs(["CFO"], location="Hamburg, Deutschland")

    mock_get.assert_called()
    call_args = mock_get.call_args
    url = call_args[0][0]
    assert url == SCALESERP_URL
    assert "scaleserp.com" in url
    assert "serpapi.com" not in url


def test_scaleserp_params_correct():
    """Params contain api_key, q, gl, hl — no search_type=google_jobs."""
    with patch("scrapers.google_jobs.requests.get", return_value=_mock_response()) as mock_get:
        with patch.dict("os.environ", {"SERPAPI_KEY": "abc123"}):
            scrape_google_jobs(["CFO"], location="Hamburg, Deutschland")

    params = mock_get.call_args[1]["params"]
    assert params["api_key"] == "abc123"
    assert params["gl"] == "de"
    assert params["hl"] == "de"
    assert "search_type" not in params   # no google_jobs search_type
    assert "engine" not in params        # no SerpAPI engine
    assert "CFO" in params["q"]          # Keyword im Query


# ---------------------------------------------------------------------------
# Test 2: without SERPAPI_KEY -> empty list, no error
# ---------------------------------------------------------------------------

def test_no_api_key_returns_empty(monkeypatch):
    monkeypatch.delenv("SERPAPI_KEY", raising=False)
    result = scrape_google_jobs(["CFO"])
    assert result == []


def test_no_api_key_does_not_call_requests(monkeypatch):
    monkeypatch.delenv("SERPAPI_KEY", raising=False)
    with patch("scrapers.google_jobs.requests.get") as mock_get:
        scrape_google_jobs(["CFO"])
    mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# Test 3: mocked ScaleSerp response -> correct job dict
# ---------------------------------------------------------------------------

def test_scaleserp_response_parses_correctly():
    with patch("scrapers.google_jobs.requests.get", return_value=_mock_response()):
        with patch.dict("os.environ", {"SERPAPI_KEY": "test-key"}):
            results = scrape_google_jobs(["CFO"])

    assert len(results) == 1
    job = results[0]
    assert "CFO" in job["title"]
    assert job["company"] != ""
    assert job["location"] == "Hamburg, Deutschland"
    assert "xing.com" in job["url"]
    assert job["source"] == "google_jobs"
    assert job["description"] != ""


def test_scaleserp_response_contains_all_required_fields():
    with patch("scrapers.google_jobs.requests.get", return_value=_mock_response()):
        with patch.dict("os.environ", {"SERPAPI_KEY": "test-key"}):
            results = scrape_google_jobs(["CFO"])

    for job in results:
        missing = REQUIRED_FIELDS - job.keys()
        assert not missing, f"Fehlende Felder: {missing}"


def test_listing_pages_are_filtered_out():
    """Aggregation/listing pages are not returned as jobs."""
    with patch("scrapers.google_jobs.requests.get", return_value=_mock_response(data=MOCK_LISTING_RESULT)):
        with patch.dict("os.environ", {"SERPAPI_KEY": "test-key"}):
            results = scrape_google_jobs(["CFO"])

    assert results == []


# ---------------------------------------------------------------------------
# Test 4: API error -> empty list, no crash
# ---------------------------------------------------------------------------

def test_http_400_returns_empty():
    with patch("scrapers.google_jobs.requests.get", return_value=_mock_response(ok=False, status_code=400)):
        with patch.dict("os.environ", {"SERPAPI_KEY": "test-key"}):
            result = scrape_google_jobs(["CFO"])
    assert result == []


def test_http_401_returns_empty():
    with patch("scrapers.google_jobs.requests.get", return_value=_mock_response(ok=False, status_code=401)):
        with patch.dict("os.environ", {"SERPAPI_KEY": "bad-key"}):
            result = scrape_google_jobs(["CFO"])
    assert result == []


def test_connection_error_returns_empty():
    import requests as req
    with patch("scrapers.google_jobs.requests.get", side_effect=req.exceptions.ConnectionError("down")):
        with patch.dict("os.environ", {"SERPAPI_KEY": "test-key"}):
            result = scrape_google_jobs(["CFO"])
    assert result == []


def test_empty_organic_results_returns_empty():
    with patch("scrapers.google_jobs.requests.get", return_value=_mock_response(data={"organic_results": []})):
        with patch.dict("os.environ", {"SERPAPI_KEY": "test-key"}):
            result = scrape_google_jobs(["CFO"])
    assert result == []


# ---------------------------------------------------------------------------
# Helper-Funktionen
# ---------------------------------------------------------------------------

def test_is_listing_page_detects_aggregation_titles():
    assert _is_listing_page("25 CFO Jobs in Hamburg") is True
    assert _is_listing_page("94 Jobs fuer CFO in Hamburg, Deutschland") is True
    assert _is_listing_page("Jobs & Stellenangebote CFO Hamburg") is True
    assert _is_listing_page("19 Stellenangebote CFO Hamburg") is True
    assert _is_listing_page("Jetzt 30 offene Stellen finden") is True
    assert _is_listing_page("CFO (m/w/d) - Hamburger AG") is False
    assert _is_listing_page("Head of Finance gesucht") is False
    assert _is_listing_page("Finance Director bei Musterfirma GmbH") is False


def test_extract_company_from_hyphen_title():
    company = _extract_company("CFO (m/w/d) - Muster GmbH", "", "www.xing.com")
    assert company == "Muster GmbH"


def test_extract_company_fallback_to_domain():
    company = _extract_company("CFO gesucht", "", "www.xing.com")
    # No separator found, falls back to domain
    assert company != ""
