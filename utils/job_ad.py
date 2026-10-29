"""Turn a job ad page into plain text, and the few facts read from it without an LLM.

Most job boards and applicant-tracking systems embed the ad as schema.org
JobPosting JSON-LD (jobexport.de, jobware.de, softgarden, personio...), so
that is tried first; otherwise the page's main content is used, minus scripts,
navigation and footer. Paragraph breaks and list bullets are kept, because the
text ends up on the Notion board as well as in the scoring prompt.
"""
import json
import re

import requests
from bs4 import BeautifulSoup

MAX_AD_CHARS = 12000
# Shorter than this, a "full" ad is a teaser or an error page.
MIN_AD_CHARS = 400
TIMEOUT = 10

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

_BLOCK_TAGS = ("p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article")
_NOISE_TAGS = ("script", "style", "noscript", "nav", "header", "footer", "form", "svg", "iframe", "button")


def html_to_text(html: str) -> str:
    """Readable text from an HTML fragment: one line per block, "• " before list items."""
    if not html:
        return ""
    if "<" not in html:
        return _tidy(html)
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(_NOISE_TAGS):
        tag.decompose()
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        if tag.name == "li":
            # One line per bullet, not a blank line between them.
            tag.insert(0, "• ")
        else:
            tag.insert_after("\n")
    return _tidy(soup.get_text())


def _tidy(text: str) -> str:
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.splitlines()]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _jobpostings(data) -> list[dict]:
    if isinstance(data, list):
        return [p for item in data for p in _jobpostings(item)]
    if not isinstance(data, dict):
        return []
    if "@graph" in data:
        return _jobpostings(data["@graph"])
    kind = data.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return [data] if "JobPosting" in kinds else []


def jobposting_ld(html: str) -> dict | None:
    """The first schema.org JobPosting embedded in the page, or None."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "", strict=False)
        except ValueError:
            continue
        postings = _jobpostings(data)
        if postings:
            return postings[0]
    return None


def ad_text_from_page(html: str) -> str:
    """The job ad on a page: JSON-LD JobPosting description, else the main content."""
    if not html:
        return ""
    posting = jobposting_ld(html)
    if posting and posting.get("description"):
        text = html_to_text(str(posting["description"]))
        if len(text) >= MIN_AD_CHARS:
            return text[:MAX_AD_CHARS]
    soup = BeautifulSoup(html, "lxml")
    main = soup.find("main") or soup.find("article") or soup.find(attrs={"role": "main"}) or soup.body or soup
    return html_to_text(str(main))[:MAX_AD_CHARS]


def fetch_ad_page(url: str) -> str:
    """Fetch a job ad page and return its ad text, or "" on any failure."""
    try:
        response = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
        if not response.ok:
            print(f"[WARN] Ad page HTTP {response.status_code}: {url}")
            return ""
        text = ad_text_from_page(response.text)
        return text if len(text) >= MIN_AD_CHARS else ""
    except Exception as e:
        print(f"[WARN] Ad page fetch failed ({url}): {e}")
        return ""


# ---------------------------------------------------------------- facts without an LLM

_DE_WORDS = {"und", "der", "die", "das", "wir", "sie", "mit", "für", "ihre", "deine", "bei", "eine", "oder", "ist", "zu", "auf", "von"}
_EN_WORDS = {"and", "the", "we", "you", "with", "for", "your", "our", "are", "of", "to", "in", "a", "is", "will", "an", "or"}


def detect_language(text: str) -> str:
    """'German', 'English' or '' — by counting common function words."""
    words = re.findall(r"[a-zäöüß]+", (text or "").lower())
    de = sum(w in _DE_WORDS for w in words)
    en = sum(w in _EN_WORDS for w in words)
    if de + en < 10:
        return ""
    return "German" if de > en else "English"


_YEARS = re.compile(
    r"(?:mindestens|min\.|at least|minimum(?: of)?)?\s*(\d{1,2})\s*\+?\s*(?:-\s*\d{1,2}\s*)?"
    r"(?:years?|yrs?|jahre?n?)\b(?:[^.\n]{0,40}?)(?:experience|erfahrung|berufserfahrung)",
    re.IGNORECASE,
)


def required_years(text: str) -> int | None:
    """Largest 'N years (of) experience' / 'N Jahre Berufserfahrung' in the ad, or None."""
    years = [int(m.group(1)) for m in _YEARS.finditer(text or "")]
    years = [y for y in years if 0 < y <= 25]
    return max(years) if years else None
