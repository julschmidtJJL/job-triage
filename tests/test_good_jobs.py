import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.good_jobs import _parse_jobs, scrape_good_jobs

REQUIRED_FIELDS = {"title", "company", "location", "date_posted", "url", "description", "source"}

SAMPLE_HTML = """
<html><body>
<a href="https://goodjobs.eu/jobs/42">
  <h2>Nachhaltigkeitsmanager (m/w/d)</h2>
  <span class="font-overpass">GreenCorp GmbH</span>
  <div class="gap-2">Hamburg | Vollzeit</div>
</a>
</body></html>
"""

EMPTY_HTML = "<html><body><p>Keine Jobs gefunden.</p></body></html>"


def _mock_response(ok=True, status_code=200, text=SAMPLE_HTML):
    resp = MagicMock()
    resp.ok = ok
    resp.status_code = status_code
    resp.text = text
    return resp


# ---------------------------------------------------------------------------
# Happy Path
# ---------------------------------------------------------------------------

def test_parse_jobs_extracts_all_fields():
    jobs = _parse_jobs(SAMPLE_HTML)
    assert len(jobs) >= 1
    job = jobs[0]
    assert REQUIRED_FIELDS.issubset(job.keys())
    assert job["title"] == "Nachhaltigkeitsmanager (m/w/d)"
    assert job["source"] == "good_jobs"


def test_scrape_good_jobs_returns_jobs():
    with patch("scrapers.good_jobs.requests.get", return_value=_mock_response()):
        with patch("scrapers.good_jobs.time.sleep"):
            results = scrape_good_jobs(["Nachhaltigkeitsmanager"])

    assert isinstance(results, list)
    assert len(results) >= 1


def test_scrape_good_jobs_all_required_fields():
    with patch("scrapers.good_jobs.requests.get", return_value=_mock_response()):
        with patch("scrapers.good_jobs.time.sleep"):
            results = scrape_good_jobs(["Nachhaltigkeitsmanager"])

    for job in results:
        assert REQUIRED_FIELDS.issubset(job.keys())


def test_scrape_good_jobs_uses_location_radius():
    with patch("scrapers.good_jobs.requests.get", return_value=_mock_response()) as mock_get:
        with patch("scrapers.good_jobs.time.sleep"):
            scrape_good_jobs(["keyword"], location="Berlin", radius=50)

    called_url = mock_get.call_args[0][0]
    assert "Berlin" in called_url


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------

def test_scrape_good_jobs_http_error_returns_empty(capsys):
    with patch("scrapers.good_jobs.requests.get", return_value=_mock_response(ok=False, status_code=403)):
        with patch("scrapers.good_jobs.time.sleep"):
            results = scrape_good_jobs(["keyword"])

    assert results == []
    assert "[ERROR]" in capsys.readouterr().out


def test_scrape_good_jobs_empty_page_returns_empty():
    with patch("scrapers.good_jobs.requests.get", return_value=_mock_response(text=EMPTY_HTML)):
        with patch("scrapers.good_jobs.time.sleep"):
            results = scrape_good_jobs(["keyword"])

    assert results == []


def test_scrape_good_jobs_exception_returns_empty():
    with patch("scrapers.good_jobs.requests.get", side_effect=Exception("Connection error")):
        with patch("scrapers.good_jobs.time.sleep"):
            results = scrape_good_jobs(["keyword"])

    assert results == []


def test_scrape_good_jobs_empty_keywords():
    results = scrape_good_jobs([])
    assert results == []
