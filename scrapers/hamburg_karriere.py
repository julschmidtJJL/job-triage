"""
Hamburg Karriere scraper — uses the undocumented REST API at api-stellen.hamburg.de.
No browser needed; the API answers plain requests.
"""
import json
from urllib.parse import quote

import requests

API_URL = "https://api-stellen.hamburg.de/search/"
JOB_BASE_URL = "https://stellen.hamburg.de"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://stellen.hamburg.de/",
}

_SEARCH_QUERY = {
    "LanguageCode": "DE",
    "SearchParameters": {
        "CountItem": 500,
        "Sort": [{"Criterion": "PublicationStartDate", "Direction": "DESC"}],
        "MatchedObjectDescriptor": [
            "ID",
            "PositionTitle",
            "PositionURI",
            "PositionLocation.CityName",
            "PublicationStartDate",
            "ParentOrganizationName",
            "OrganizationShortName",
            "CareerLevel.Name",
        ],
    },
    "SearchCriteria": [
        {"CriterionName": "PublicationChannel.Code", "CriterionValue": ["12"]}
    ],
}


def scrape_hamburg_karriere(keywords: list) -> list:
    if not keywords:
        return []

    print("[INFO] Scraping Hamburg Karriere (api-stellen.hamburg.de)...")
    kw_lower = [kw.lower() for kw in keywords]

    try:
        url = f"{API_URL}?data={quote(json.dumps(_SEARCH_QUERY))}"
        response = requests.get(url, headers=HEADERS, timeout=15)
        if not response.ok:
            print(f"[ERROR] Hamburg Karriere API HTTP {response.status_code}")
            return []

        data = response.json()
        items = data.get("SearchResult", {}).get("SearchResultItems", [])

        jobs = []
        for item in items:
            desc = item.get("MatchedObjectDescriptor", {})
            title = desc.get("PositionTitle", "").strip()
            if not title:
                continue

            if not any(kw in title.lower() for kw in kw_lower):
                continue

            job_url = desc.get("PositionURI", "")
            if not job_url:
                job_id = desc.get("ID", "")
                job_url = f"{JOB_BASE_URL}/index.php?ac=jobad&id={job_id}" if job_id else ""

            location_list = desc.get("PositionLocation", [])
            if isinstance(location_list, list) and location_list:
                city = location_list[0].get("CityName", "Hamburg")
            elif isinstance(location_list, dict):
                city = location_list.get("CityName", "Hamburg")
            else:
                city = "Hamburg"

            company = (
                desc.get("OrganizationShortName")
                or desc.get("ParentOrganizationName")
                or "Stadt Hamburg"
            )

            jobs.append({
                "title": title,
                "company": company,
                "location": city if city else "Hamburg",
                "date_posted": desc.get("PublicationStartDate", ""),
                "url": job_url,
                "description": "",
                "source": "hamburg_karriere",
            })

        print(f"[OK] {len(jobs)} matching jobs found on Hamburg Karriere")
        return jobs

    except Exception as e:
        print(f"[ERROR] Hamburg Karriere scraping failed: {e}")
        return []


if __name__ == "__main__":
    results = scrape_hamburg_karriere(["Finanzen", "Controlling", "Analyst", "Leiter", "Manager"])
    print(f"\n{len(results)} jobs found.")
    for r in results[:5]:
        print(f"  [{r['location']}] {r['title']} @ {r['company']}")
