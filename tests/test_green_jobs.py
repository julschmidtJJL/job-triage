import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.green_jobs import _parse_jobs, scrape_green_jobs

REQUIRED_FIELDS = {"title", "company", "location", "date_posted", "url", "description", "source"}

SAMPLE_HTML = """
<html><body>
<div class="jobitem">
  <div>
    <a href="/stellenanzeige/123">Umweltberater (m/w/d)</a>
    <br/>EcoFirm GmbH | Hamburg, Deutschland
  </div>
</div>
<div class="jobitem">
  <div>
    <a href="/stellenanzeige/456">Marketing Manager (m/w/d)</a>
    <br/>OtherCorp | Hamburg, Deutschland
  </div>
</div>
</body></html>
"""

EMPTY_HTML = "<html><body><p>Keine Treffer.</p></body></html>"
SHORT_HTML = "<html><body></body></html>"


def _mock_response(ok=True, status_code=200, text=SAMPLE_HTML):
    resp = MagicMock()
    resp.ok = ok
    resp.status_code = status_code
    resp.text = text
    return resp


# ---------------------------------------------------------------------------
# The new URL is used
# ---------------------------------------------------------------------------

def test_scrape_green_jobs_uses_new_url():
    """The new URL greenjobs.de/stellenanzeige/hamburg/ must be used."""
    with patch("scrapers.green_jobs.requests.get", return_value=_mock_response()) as mock_get:
        scrape_green_jobs(["Umweltberater"])
    called_url = mock_get.call_args[0][0]
    assert "stellenanzeige/hamburg" in called_url


def test_scrape_green_jobs_calls_get_once():
    """Only one GET request (static listing page)."""
    with patch("scrapers.green_jobs.requests.get", return_value=_mock_response()) as mock_get:
        scrape_green_jobs(["Umwelt", "Nachhaltigkeit"])
    assert mock_get.call_count == 1


# ---------------------------------------------------------------------------
# Happy Path
# ---------------------------------------------------------------------------

def test_parse_jobs_filters_by_keyword():
    jobs = _parse_jobs(SAMPLE_HTML, ["umweltberater"])
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Umweltberater (m/w/d)"
    assert jobs[0]["source"] == "green_jobs"


def test_parse_jobs_all_required_fields():
    jobs = _parse_jobs(SAMPLE_HTML, ["umweltberater"])
    for job in jobs:
        assert REQUIRED_FIELDS.issubset(job.keys())


def test_scrape_green_jobs_returns_jobs():
    with patch("scrapers.green_jobs.requests.get", return_value=_mock_response()):
        results = scrape_green_jobs(["Umweltberater"])
    assert isinstance(results, list)
    assert len(results) >= 1
    assert results[0]["source"] == "green_jobs"


def test_scrape_green_jobs_excludes_non_matching():
    with patch("scrapers.green_jobs.requests.get", return_value=_mock_response()):
        results = scrape_green_jobs(["Umweltberater"])
    assert not any("Marketing" in j["title"] for j in results)


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------

def test_scrape_green_jobs_http_error_returns_empty(capsys):
    with patch("scrapers.green_jobs.requests.get", return_value=_mock_response(ok=False, status_code=503)):
        results = scrape_green_jobs(["keyword"])
    assert results == []
    assert "[ERROR]" in capsys.readouterr().out


def test_scrape_green_jobs_short_response_returns_empty(capsys):
    with patch("scrapers.green_jobs.requests.get", return_value=_mock_response(text=SHORT_HTML)):
        results = scrape_green_jobs(["keyword"])
    assert results == []
    assert "[WARN]" in capsys.readouterr().out


def test_scrape_green_jobs_empty_page_returns_empty():
    with patch("scrapers.green_jobs.requests.get", return_value=_mock_response(text=EMPTY_HTML)):
        results = scrape_green_jobs(["Umweltberater"])
    assert results == []


def test_scrape_green_jobs_exception_returns_empty():
    with patch("scrapers.green_jobs.requests.get", side_effect=Exception("Timeout")):
        results = scrape_green_jobs(["keyword"])
    assert results == []


def test_scrape_green_jobs_empty_keywords():
    results = scrape_green_jobs([])
    assert results == []
