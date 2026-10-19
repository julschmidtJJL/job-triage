import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers.hamburg_karriere import scrape_hamburg_karriere

REQUIRED_FIELDS = {"title", "company", "location", "date_posted", "url", "description", "source"}

SAMPLE_API_RESPONSE = {
    "SearchResult": {
        "SearchResultItems": [
            {
                "MatchedObjectDescriptor": {
                    "ID": "12345",
                    "PositionTitle": "Data Analyst (m/w/d)",
                    "PositionURI": "https://stellen.hamburg.de/index.php?ac=jobad&id=12345",
                    "PositionLocation": [{"CityName": "Hamburg"}],
                    "PublicationStartDate": "2024-01-20",
                    "OrganizationShortName": "Behörde für Stadtentwicklung",
                    "ParentOrganizationName": "Hamburg",
                    "CareerLevel": [{"Name": "Fachkraft"}],
                }
            },
            {
                "MatchedObjectDescriptor": {
                    "ID": "99999",
                    "PositionTitle": "Buchhalter (m/w/d)",
                    "PositionURI": "https://stellen.hamburg.de/index.php?ac=jobad&id=99999",
                    "PositionLocation": [{"CityName": "Hamburg"}],
                    "PublicationStartDate": "2024-01-21",
                    "OrganizationShortName": "Finanzbehörde",
                    "ParentOrganizationName": "Hamburg",
                    "CareerLevel": [],
                }
            },
        ]
    }
}

EMPTY_RESPONSE = {"SearchResult": {"SearchResultItems": []}}


def _mock_response(ok=True, status_code=200, json_data=None):
    resp = MagicMock()
    resp.ok = ok
    resp.status_code = status_code
    resp.json.return_value = json_data if json_data is not None else SAMPLE_API_RESPONSE
    return resp


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_scrape_hamburg_karriere_filters_by_keyword():
    with patch("scrapers.hamburg_karriere.requests.get", return_value=_mock_response()):
        results = scrape_hamburg_karriere(["data analyst"])
    assert len(results) == 1
    assert results[0]["title"] == "Data Analyst (m/w/d)"


def test_scrape_hamburg_karriere_all_required_fields():
    with patch("scrapers.hamburg_karriere.requests.get", return_value=_mock_response()):
        results = scrape_hamburg_karriere(["data analyst"])
    for job in results:
        assert REQUIRED_FIELDS.issubset(job.keys())


def test_scrape_hamburg_karriere_source():
    with patch("scrapers.hamburg_karriere.requests.get", return_value=_mock_response()):
        results = scrape_hamburg_karriere(["data analyst"])
    assert all(j["source"] == "hamburg_karriere" for j in results)


def test_scrape_hamburg_karriere_location_extracted():
    with patch("scrapers.hamburg_karriere.requests.get", return_value=_mock_response()):
        results = scrape_hamburg_karriere(["data analyst"])
    assert results[0]["location"] == "Hamburg"


def test_scrape_hamburg_karriere_company_extracted():
    with patch("scrapers.hamburg_karriere.requests.get", return_value=_mock_response()):
        results = scrape_hamburg_karriere(["data analyst"])
    assert results[0]["company"] == "Behörde für Stadtentwicklung"


def test_scrape_hamburg_karriere_url_from_position_uri():
    with patch("scrapers.hamburg_karriere.requests.get", return_value=_mock_response()):
        results = scrape_hamburg_karriere(["data analyst"])
    assert "stellen.hamburg.de" in results[0]["url"]


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_scrape_hamburg_karriere_empty_keywords_returns_empty():
    results = scrape_hamburg_karriere([])
    assert results == []


def test_scrape_hamburg_karriere_http_error_returns_empty(capsys):
    with patch("scrapers.hamburg_karriere.requests.get",
               return_value=_mock_response(ok=False, status_code=503)):
        results = scrape_hamburg_karriere(["keyword"])
    assert results == []
    assert "[ERROR]" in capsys.readouterr().out


def test_scrape_hamburg_karriere_empty_response():
    with patch("scrapers.hamburg_karriere.requests.get",
               return_value=_mock_response(json_data=EMPTY_RESPONSE)):
        results = scrape_hamburg_karriere(["data analyst"])
    assert results == []


def test_scrape_hamburg_karriere_exception_returns_empty():
    with patch("scrapers.hamburg_karriere.requests.get", side_effect=Exception("Timeout")):
        results = scrape_hamburg_karriere(["keyword"])
    assert results == []


def test_scrape_hamburg_karriere_no_keyword_match_returns_empty():
    with patch("scrapers.hamburg_karriere.requests.get", return_value=_mock_response()):
        results = scrape_hamburg_karriere(["Kfz-Mechatroniker"])
    assert results == []
