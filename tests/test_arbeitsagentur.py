"""Tests for scrapers/arbeitsagentur.py (HTTP mocked)."""
import base64
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from scrapers import arbeitsagentur as ba

ITEM = {
    "stellenangebotsTitel": "Digitalisierungsmanager (m/w/d)",
    "firma": "Musterfirma Korrosionsschutz GmbH",
    "referenznummer": "12345-ABC-S",
    "datumErsteVeroeffentlichung": "2026-10-14",
    "stellenlokationen": [{"adresse": {"ort": "Quickborn", "plz": "25451"}}],
    "verguetungsangabe": "JAHRESGEHALT",
    "gehaltsspanneVon": 55000.0,
    "gehaltsspanneBis": 65000.0,
    "arbeitszeitVollzeit": True,
    "hauptberuf": "IT-Manager/in",
}


def _response(payload, ok=True, status=200):
    r = MagicMock()
    r.ok, r.status_code = ok, status
    r.json.return_value = payload
    return r


def test_maps_a_search_result():
    with patch("scrapers.arbeitsagentur.requests.get", return_value=_response({"ergebnisliste": [ITEM]})) as get, \
         patch("scrapers.arbeitsagentur.time.sleep"):
        jobs = ba.scrape_arbeitsagentur(["Digitalisierung"])
    assert len(jobs) == 1
    job = jobs[0]
    assert job["title"] == "Digitalisierungsmanager (m/w/d)"
    assert job["company"] == "Musterfirma Korrosionsschutz GmbH"
    assert job["location"] == "Quickborn"
    assert job["url"] == "https://www.arbeitsagentur.de/jobsuche/jobdetail/12345-ABC-S"
    assert job["source"] == "arbeitsagentur" and job["query"] == "Digitalisierung"
    assert job["within_radius"] is True
    assert "Salary: 55,000–65,000 EUR per year" in job["description"]
    assert "full-time" in job["description"]
    params = get.call_args.kwargs["params"]
    assert params["was"] == "Digitalisierung" and params["wo"] == "Hamburg"
    assert params["zeitarbeit"] == "false" and params["pav"] == "false"
    assert get.call_args.kwargs["headers"]["X-API-Key"] == "jobboerse-jobsuche"


def test_agencies_can_be_included():
    with patch("scrapers.arbeitsagentur.requests.get", return_value=_response({"ergebnisliste": []})) as get, \
         patch("scrapers.arbeitsagentur.time.sleep"):
        ba.scrape_arbeitsagentur(["x"], exclude_agencies=False)
    assert "zeitarbeit" not in get.call_args.kwargs["params"]


def test_same_job_from_two_queries_counts_once():
    with patch("scrapers.arbeitsagentur.requests.get", return_value=_response({"ergebnisliste": [ITEM]})), \
         patch("scrapers.arbeitsagentur.time.sleep"):
        jobs = ba.scrape_arbeitsagentur(["Digitalisierung", "KI Manager"])
    assert len(jobs) == 1


def test_http_error_and_bad_items_are_skipped(capsys):
    bad = {"stellenangebotsTitel": "", "referenznummer": "1"}
    responses = [_response({}, ok=False, status=503), _response({"ergebnisliste": [bad, ITEM]})]
    with patch("scrapers.arbeitsagentur.requests.get", side_effect=responses), \
         patch("scrapers.arbeitsagentur.time.sleep"):
        jobs = ba.scrape_arbeitsagentur(["a", "b"])
    assert len(jobs) == 1
    assert "[ERROR] Arbeitsagentur HTTP 503 for: a" in capsys.readouterr().out


def test_fetch_description_uses_base64_ref():
    with patch("scrapers.arbeitsagentur.requests.get",
               return_value=_response({"stellenangebotsBeschreibung": "Wir digitalisieren."})) as get:
        text = ba.fetch_description("https://www.arbeitsagentur.de/jobsuche/jobdetail/12345-ABC-S")
    assert text == "Wir digitalisieren."
    expected = base64.b64encode(b"12345-ABC-S").decode()
    assert get.call_args.args[0].endswith(f"/pc/v4/jobdetails/{expected}")


def test_pipeline_fetches_ba_descriptions_through_the_api():
    import utils.pipeline as pl
    with patch("scrapers.arbeitsagentur.fetch_description", return_value="Volltext") as fetch:
        assert pl.fetch_full_description("https://www.arbeitsagentur.de/jobsuche/jobdetail/x", "arbeitsagentur") == ("Volltext", "full_jd")
    fetch.assert_called_once()


def test_radius_jobs_skip_the_hamburg_place_list():
    import main as m
    assert m.keep_location({"location": "Quickborn", "within_radius": True}) is True
    assert m.keep_location({"location": "Quickborn"}) is False


PARTNER_PAGE = (Path(__file__).parent / "fixtures" / "partner_jobposting.html").read_text(encoding="utf-8")
PARTNER_URL = "https://www.jobexport.de/job/1234567.html"
JOB_URL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-1234567890-S"


def _page(html):
    r = MagicMock()
    r.ok, r.status_code, r.text = True, 200, html
    return r


def _route(details, partner):
    """One requests.get for both hosts (it is the same function): the API
    answers with `details`, the partner site with `partner` (a response or an exception)."""
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        if url.startswith(ba.API_BASE):
            return _response(details)
        if isinstance(partner, Exception):
            raise partner
        return partner
    return get, calls


def test_short_api_text_follows_the_partner_link():
    details = {"stellenangebotsBeschreibung": "Vollständige Stellenbeschreibung bei unserem Kooperationspartner einsehen",
               "externeURL": PARTNER_URL}
    get, calls = _route(details, _page(PARTNER_PAGE))
    with patch("scrapers.arbeitsagentur.requests.get", side_effect=get):
        text = ba.fetch_description(JOB_URL)
    assert "Mindestens 5 Jahre Berufserfahrung in der Disposition oder Logistik" in text
    assert "Jana Muster, Recruiting Specialist" in text
    assert calls[-1] == PARTNER_URL


def test_complete_api_text_skips_the_partner():
    long_text = "Wir suchen einen Disponenten. " * 40
    details = {"stellenangebotsBeschreibung": long_text, "externeURL": PARTNER_URL}
    get, calls = _route(details, _page(PARTNER_PAGE))
    with patch("scrapers.arbeitsagentur.requests.get", side_effect=get):
        assert ba.fetch_description(JOB_URL) == long_text.strip()
    assert PARTNER_URL not in calls


def test_partner_failure_keeps_the_api_text():
    details = {"stellenangebotsBeschreibung": "Kurztext.", "externeURL": PARTNER_URL}
    get, calls = _route(details, TimeoutError("slow"))
    with patch("scrapers.arbeitsagentur.requests.get", side_effect=get):
        assert ba.fetch_description(JOB_URL) == "Kurztext."
    assert calls[-1] == PARTNER_URL


def test_api_error_gives_nothing():
    with patch("scrapers.arbeitsagentur.requests.get", side_effect=ConnectionError("down")):
        assert ba.fetch_description(JOB_URL) == ""
