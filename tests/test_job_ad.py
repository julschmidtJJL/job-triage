"""Tests for utils/job_ad.py: ad text from partner pages, and facts read without an LLM."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import job_ad

FIXTURES = Path(__file__).parent / "fixtures"


def _page(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _response(text: str, ok: bool = True, status: int = 200):
    r = MagicMock()
    r.ok, r.status_code, r.text = ok, status, text
    return r


# ---------------------------------------------------------------- text

def test_html_to_text_keeps_paragraphs_and_bullets():
    text = job_ad.html_to_text("<p>Über uns</p><p>Wir bauen.</p><ul><li>Python</li><li>SQL</li></ul>")
    assert text.splitlines() == ["Über uns", "", "Wir bauen.", "", "• Python", "• SQL"]


def test_plain_text_passes_through_with_its_line_breaks():
    assert job_ad.html_to_text("Zeile 1\n\n**Zeile 2**  ") == "Zeile 1\n\n**Zeile 2**"


def test_jobposting_json_ld_wins_over_the_page():
    text = job_ad.ad_text_from_page(_page("partner_jobposting.html"))
    assert "Mindestens 5 Jahre Berufserfahrung in der Disposition oder Logistik" in text
    assert "• Tourenplanung und Forecasts" in text
    assert "Startseite" not in text and "Kurzfassung" not in text


def test_page_without_json_ld_uses_main_content_without_scripts_or_chrome():
    text = job_ad.ad_text_from_page(_page("partner_plain.html"))
    assert "build our planning function from scratch" in text
    assert "• 5+ years of experience in operations planning, ideally in logistics" in text
    assert "dataLayer" not in text and "track(" not in text
    assert "Login" not in text and "Imprint" not in text


def test_text_is_capped():
    html = "<main><p>" + "x " * 20000 + "</p></main>"
    assert len(job_ad.ad_text_from_page(html)) == job_ad.MAX_AD_CHARS


# ---------------------------------------------------------------- fetch

def test_fetch_ad_page_returns_the_ad():
    with patch("utils.job_ad.requests.get", return_value=_response(_page("partner_jobposting.html"))) as get:
        text = job_ad.fetch_ad_page("https://www.jobexport.de/job/1.html")
    assert "Power-BI-Reports" in text
    assert get.call_args.kwargs["timeout"] == job_ad.TIMEOUT


def test_fetch_ad_page_fails_quietly():
    with patch("utils.job_ad.requests.get", return_value=_response("", ok=False, status=403)):
        assert job_ad.fetch_ad_page("https://x.example/1") == ""
    with patch("utils.job_ad.requests.get", side_effect=TimeoutError("slow")):
        assert job_ad.fetch_ad_page("https://x.example/1") == ""
    # A page with almost nothing on it is an error page, not an ad.
    with patch("utils.job_ad.requests.get", return_value=_response("<main><p>Not found</p></main>")):
        assert job_ad.fetch_ad_page("https://x.example/1") == ""


# ---------------------------------------------------------------- facts

def test_detect_language():
    assert job_ad.detect_language(job_ad.ad_text_from_page(_page("partner_jobposting.html"))) == "German"
    assert job_ad.detect_language(job_ad.ad_text_from_page(_page("partner_plain.html"))) == "English"
    assert job_ad.detect_language("Data Engineer") == ""


def test_required_years():
    assert job_ad.required_years("5+ years of experience in operations planning") == 5
    assert job_ad.required_years("Mindestens 3 Jahre Berufserfahrung auf der Baustelle") == 3
    assert job_ad.required_years("3-5 Jahre Erfahrung in der Disposition, davon 2 Jahre Berufserfahrung in Führung") == 3
    assert job_ad.required_years("Seit 50 Jahren am Markt. Mehrjährige Erfahrung.") is None
    assert job_ad.required_years("") is None
