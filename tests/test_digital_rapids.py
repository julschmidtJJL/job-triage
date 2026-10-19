import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.digital_rapids import _parse_jobs, scrape_digital_rapids

REQUIRED_FIELDS = {"title", "company", "location", "date_posted", "url", "description", "source"}

SAMPLE_HTML = """
<html><body>
<article class="job-listing">
  <h2><a href="/karriere/jobs/11">Data Engineer (m/w/d)</a></h2>
  <span class="standort">Deutschland</span>
  <time datetime="2024-04-01">01.04.2024</time>
  <p>Aufbau von Datenpipelines mit Python und Spark.</p>
</article>
<article class="job-listing">
  <h2><a href="/karriere/jobs/22">Marketing Manager (m/w/d)</a></h2>
  <span class="standort">Deutschland</span>
  <time datetime="2024-04-02">02.04.2024</time>
  <p>Online-Marketing und Kampagnenmanagement.</p>
</article>
</body></html>
"""

EMPTY_HTML = "<html><body><p>Keine Stellen vorhanden.</p></body></html>"


def _mock_response(ok=True, status_code=200, text=SAMPLE_HTML):
    resp = MagicMock()
    resp.ok = ok
    resp.status_code = status_code
    resp.text = text
    return resp


# ---------------------------------------------------------------------------
# Happy Path
# ---------------------------------------------------------------------------

def test_parse_jobs_filters_by_keyword():
    jobs = _parse_jobs(SAMPLE_HTML, ["data engineer"])
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Data Engineer (m/w/d)"
    assert jobs[0]["source"] == "digital_rapids"
    assert jobs[0]["company"] == "Digital Rapids"


def test_parse_jobs_all_required_fields():
    jobs = _parse_jobs(SAMPLE_HTML, ["data engineer"])
    for job in jobs:
        assert REQUIRED_FIELDS.issubset(job.keys())


def test_scrape_digital_rapids_returns_matching_jobs():
    with patch("scrapers.digital_rapids.requests.get", return_value=_mock_response()):
        results = scrape_digital_rapids(["data engineer"])

    assert isinstance(results, list)
    assert len(results) == 1
    assert results[0]["title"] == "Data Engineer (m/w/d)"


def test_scrape_digital_rapids_source_is_digital_rapids():
    with patch("scrapers.digital_rapids.requests.get", return_value=_mock_response()):
        results = scrape_digital_rapids(["data engineer"])

    for job in results:
        assert job["source"] == "digital_rapids"


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------

def test_scrape_digital_rapids_http_error_returns_empty(capsys):
    with patch("scrapers.digital_rapids.requests.get", return_value=_mock_response(ok=False, status_code=500)):
        results = scrape_digital_rapids(["keyword"])

    assert results == []
    assert "[ERROR]" in capsys.readouterr().out


def test_scrape_digital_rapids_empty_page_returns_empty():
    with patch("scrapers.digital_rapids.requests.get", return_value=_mock_response(text=EMPTY_HTML)):
        results = scrape_digital_rapids(["data engineer"])

    assert results == []


def test_scrape_digital_rapids_exception_returns_empty():
    with patch("scrapers.digital_rapids.requests.get", side_effect=Exception("Connection refused")):
        results = scrape_digital_rapids(["keyword"])

    assert results == []


def test_scrape_digital_rapids_no_keyword_match_returns_empty():
    with patch("scrapers.digital_rapids.requests.get", return_value=_mock_response()):
        results = scrape_digital_rapids(["Buchhalter"])

    assert results == []
