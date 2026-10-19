"""Bundesagentur für Arbeit job search (Jobbörse).

The official REST backend of arbeitsagentur.de/jobsuche, with the public client
key the site itself uses. Strong coverage of Mittelstand employers who never
post on LinkedIn. Endpoints (probed 15 Oct 2026):

  search:  GET .../pc/v6/jobs?was=&wo=&umkreis=&angebotsart=1&veroeffentlichtseit=&size=&page=
  details: GET .../pc/v4/jobdetails/<base64(referenznummer)>   (v6 details is 403)

Temp-work (Zeitarbeit) and private placement agencies (PAV) are excluded by
default: they hide the actual employer, and the profile is about the company's
situation.
"""
import base64
import time

import requests

API_BASE = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service"
SEARCH_URL = f"{API_BASE}/pc/v6/jobs"
DETAILS_URL = f"{API_BASE}/pc/v4/jobdetails/{{ref_b64}}"
JOB_PAGE_URL = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{ref}"
HEADERS = {"X-API-Key": "jobboerse-jobsuche", "User-Agent": "jobhunt/1.0"}
TIMEOUT = 20


def _salary_line(item: dict) -> str:
    low, high = item.get("gehaltsspanneVon"), item.get("gehaltsspanneBis")
    if not low and not high:
        return ""
    unit = "per year" if item.get("verguetungsangabe") == "JAHRESGEHALT" else ""
    span = f"{int(low):,}–{int(high):,} EUR" if low and high else f"{int(low or high):,} EUR"
    return f"Salary: {span} {unit}".strip()


def _hours_line(item: dict) -> str:
    parts = []
    if item.get("arbeitszeitVollzeit"):
        parts.append("full-time")
    if any(item.get(k) for k in ("arbeitszeitTeilzeitVormittag", "arbeitszeitTeilzeitNachmittag",
                                  "arbeitszeitTeilzeitAbend", "arbeitszeitTeilzeitFlexibel")):
        parts.append("part-time")
    return f"Hours: {' or '.join(parts)}" if parts else ""


def _to_job(item: dict, query: str) -> dict | None:
    ref = item.get("referenznummer")
    title = item.get("stellenangebotsTitel", "")
    if not ref or not title:
        return None
    locations = item.get("stellenlokationen") or [{}]
    address = (locations[0] or {}).get("adresse", {}) or {}
    snippet = ". ".join(x for x in (_salary_line(item), _hours_line(item), item.get("hauptberuf", "")) if x)
    return {
        "title": title,
        "company": item.get("firma", ""),
        "location": address.get("ort", ""),
        "date_posted": item.get("datumErsteVeroeffentlichung", ""),
        "url": JOB_PAGE_URL.format(ref=ref),
        "description": snippet,
        "source": "arbeitsagentur",
        "query": query,
        # The search already limited results to the radius around Hamburg,
        # and small towns in that radius are not on main.py's place list.
        "within_radius": True,
    }


def scrape_arbeitsagentur(
    keywords: list,
    location: str = "Hamburg",
    radius: int = 30,
    days: int = 7,
    max_per_keyword: int = 50,
    exclude_agencies: bool = True,
) -> list:
    jobs: list = []
    seen: set = set()
    for keyword in keywords:
        params = {
            "was": keyword, "wo": location, "umkreis": radius, "angebotsart": 1,
            "veroeffentlichtseit": days, "size": max_per_keyword, "page": 1,
        }
        if exclude_agencies:
            params.update({"zeitarbeit": "false", "pav": "false"})
        print(f"[INFO] Scraping Arbeitsagentur for: {keyword} ({location}, {radius} km)")
        try:
            response = requests.get(SEARCH_URL, params=params, headers=HEADERS, timeout=TIMEOUT)
            if not response.ok:
                print(f"[ERROR] Arbeitsagentur HTTP {response.status_code} for: {keyword}")
                time.sleep(1)
                continue
            found = 0
            for item in response.json().get("ergebnisliste", []) or []:
                job = _to_job(item, keyword)
                if job and job["url"] not in seen:
                    seen.add(job["url"])
                    jobs.append(job)
                    found += 1
            print(f"[OK] {found} jobs found for: {keyword}")
        except Exception as e:
            print(f"[ERROR] Arbeitsagentur scraping failed for '{keyword}': {e}")
        time.sleep(1)
    return jobs


def fetch_description(url: str) -> str:
    """Full job description for a job page URL from this source, or ""."""
    ref = url.rstrip("/").rsplit("/", 1)[-1]
    ref_b64 = base64.b64encode(ref.encode()).decode()
    try:
        response = requests.get(DETAILS_URL.format(ref_b64=ref_b64), headers=HEADERS, timeout=TIMEOUT)
        if not response.ok:
            return ""
        return (response.json().get("stellenangebotsBeschreibung") or "")[:8000]
    except Exception:
        return ""


if __name__ == "__main__":
    results = scrape_arbeitsagentur(["Digitalisierung"])
    print(f"\n{len(results)} jobs found.")
