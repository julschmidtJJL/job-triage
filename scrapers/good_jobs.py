import time
from urllib.parse import quote_plus

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://goodjobs.eu/jobs"
BASE_DOMAIN = "https://goodjobs.eu"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9",
}


def _parse_jobs(html: str) -> list:
    soup = BeautifulSoup(html, "lxml")
    jobs = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if BASE_DOMAIN + "/jobs/" not in href or "mailto" in href:
            continue

        h2 = a.find("h2")
        if not h2:
            continue
        title = h2.get_text(strip=True)
        if not title:
            continue

        # Company: first span with font-overpass class (company name)
        company = ""
        for span in a.find_all("span"):
            cls = " ".join(span.get("class", []))
            txt = span.get_text(strip=True)
            if "font-overpass" in cls and txt and txt != title:
                company = txt
                break

        # Location: find the compact "City | WorkMode" div (gap-2 class, short text)
        location = "Deutschland"
        for div in a.find_all("div"):
            txt = div.get_text(separator=" ", strip=True)
            cls = " ".join(div.get("class", []))
            if "|" in txt and "gap-2" in cls and len(txt) < 50:
                city_part = txt.split("|")[0].strip()
                if city_part and not city_part.startswith("vor "):
                    location = city_part
                    break

        jobs.append({
            "title": title,
            "company": company,
            "location": location,
            "date_posted": "",
            "url": href,
            "description": "",
            "source": "good_jobs",
        })

    # Deduplicate by URL
    seen = set()
    unique = []
    for j in jobs:
        if j["url"] not in seen:
            seen.add(j["url"])
            unique.append(j)
    return unique


def scrape_good_jobs(keywords: list, location: str = "Hamburg", radius: int = 100) -> list:
    all_jobs = []
    seen_urls: set = set()

    for keyword in keywords:
        url = (
            f"{BASE_URL}?q={quote_plus(keyword)}"
            f"&location={quote_plus(location)}"
        )
        print(f"[INFO] Scraping Good Jobs for: {keyword} ({location})")

        try:
            response = requests.get(url, headers=HEADERS, timeout=10)
            if not response.ok:
                print(f"[ERROR] Good Jobs HTTP {response.status_code} for: {keyword}")
                time.sleep(2)
                continue

            jobs = _parse_jobs(response.text)
            new_jobs = [j for j in jobs if j["url"] not in seen_urls]
            for j in new_jobs:
                seen_urls.add(j["url"])
                j["query"] = keyword
            all_jobs.extend(new_jobs)
            print(f"[OK] {len(new_jobs)} jobs found for: {keyword}")

        except Exception as e:
            print(f"[ERROR] Good Jobs scraping failed for '{keyword}': {e}")

        time.sleep(2)

    return all_jobs


if __name__ == "__main__":
    results = scrape_good_jobs(["CFO", "Finance Manager"])
    print(f"\n{len(results)} jobs found.")
    for r in results[:5]:
        print(f"  [{r['location']}] {r['title']} @ {r['company']}")
