import time

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.greenjobs.de/stellenanzeige/hamburg/"
BASE_DOMAIN = "https://www.greenjobs.de"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9",
}


def _parse_jobs(html: str, keywords: list) -> list:
    soup = BeautifulSoup(html, "lxml")
    jobs = []
    kw_lower = [kw.lower() for kw in keywords]

    # Structure: <div class="jobitem">
    #   <div><img ...></div>
    #   <div><strong><a href="/stellenanzeige/...">TITLE</a></strong><br/>COMPANY | LOCATION</div>
    # </div>
    for card in soup.find_all("div", class_="jobitem"):
        try:
            link_tag = card.find("a", href=True)
            if not link_tag:
                continue
            title = link_tag.get_text(strip=True)
            if not title or len(title) < 4:
                continue

            # Query filter
            if not any(kw in title.lower() for kw in kw_lower):
                continue

            href = link_tag["href"]
            if not href.startswith("http"):
                href = BASE_DOMAIN + "/" + href.lstrip("/")

            # Company + Location: text after <br> in the second div
            info_div = card.find_all("div")
            company = ""
            location = "Hamburg, Deutschland"
            for div in info_div:
                br = div.find("br")
                if br and br.next_sibling:
                    info_text = br.next_sibling.strip() if isinstance(br.next_sibling, str) else ""
                    if "|" in info_text:
                        parts = info_text.split("|", 1)
                        company = parts[0].strip()
                        location = parts[1].strip() if len(parts) > 1 else "Hamburg, Deutschland"
                    elif info_text:
                        company = info_text
                    break

            jobs.append({
                "title": title,
                "company": company,
                "location": location,
                "date_posted": "",
                "url": href,
                "description": "",
                "source": "green_jobs",
            })
        except Exception as e:
            print(f"[WARN] Could not parse Green Jobs card: {e}")
            continue

    return jobs


def scrape_green_jobs(keywords: list, location: str = "Hamburg", radius: int = 100) -> list:
    if not keywords:
        return []

    print(f"[INFO] Scraping Green Jobs Hamburg (greenjobs.de/stellenanzeige/hamburg/)...")

    try:
        response = requests.get(BASE_URL, headers=HEADERS, timeout=10)
        if not response.ok:
            print(f"[ERROR] Green Jobs HTTP {response.status_code}")
            return []

        if len(response.text) < 200:
            print(f"[WARN] Green Jobs: response too short, probably a JS redirect")
            return []

        jobs = _parse_jobs(response.text, keywords)
        print(f"[OK] {len(jobs)} matching jobs found on Green Jobs")
        return jobs

    except Exception as e:
        print(f"[ERROR] Green Jobs scraping failed: {e}")
        return []


if __name__ == "__main__":
    results = scrape_green_jobs(["Ingenieur", "Manager", "Analyst", "Leiter"])
    print(f"\n{len(results)} jobs found.")
    for r in results[:5]:
        print(f"  [{r['location']}] {r['title']} @ {r['company']}")
