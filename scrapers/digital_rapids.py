import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.digitalrapids.de/karriere"
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

    selectors = [
        lambda s: s.find_all(["article", "li"], class_=lambda c: c and "job" in c.lower()),
        lambda s: s.find_all("div", class_=lambda c: c and any(
            x in c.lower() for x in ["stelle", "position", "vacancy", "listing", "career", "offer"]
        )),
    ]

    cards = []
    for selector in selectors:
        found = selector(soup)
        if found:
            cards = found
            break

    if not cards:
        for link in soup.find_all("a", href=True):
            text = link.get_text(strip=True)
            if text and any(kw in text.lower() for kw in kw_lower):
                href = link["href"]
                if not href.startswith("http"):
                    href = "https://www.digitalrapids.de" + href
                jobs.append(
                    {
                        "title": text,
                        "company": "Digital Rapids",
                        "location": "Deutschland",
                        "date_posted": "",
                        "url": href,
                        "description": "",
                        "source": "digital_rapids",
                    }
                )
        return jobs

    for card in cards:
        try:
            title_tag = card.find(["h2", "h3", "a"])
            if not title_tag:
                continue
            title = title_tag.get_text(strip=True)
            if not title:
                continue

            # Filter by keywords
            if not any(kw in title.lower() for kw in kw_lower):
                continue

            location_tag = card.find(class_=lambda c: c and any(
                x in c.lower() for x in ["location", "ort", "city", "standort"]
            ))
            location = location_tag.get_text(strip=True) if location_tag else "Deutschland"

            date_tag = card.find("time")
            date_posted = (
                date_tag.get("datetime", date_tag.get_text(strip=True)) if date_tag else ""
            )

            link_tag = card.find("a", href=True)
            href = link_tag["href"] if link_tag else ""
            if href and not href.startswith("http"):
                href = "https://www.digitalrapids.de" + href
            url = href

            desc_tag = card.find("p")
            description = desc_tag.get_text(strip=True) if desc_tag else ""

            jobs.append(
                {
                    "title": title,
                    "company": "Digital Rapids",
                    "location": location,
                    "date_posted": date_posted,
                    "url": url,
                    "description": description,
                    "source": "digital_rapids",
                }
            )
        except Exception as e:
            print(f"[WARN] Could not parse Digital Rapids card: {e}")
            continue

    return jobs


def scrape_digital_rapids(keywords: list) -> list:
    print(f"[INFO] Scraping Digital Rapids Karriere...")

    try:
        response = requests.get(BASE_URL, headers=HEADERS, timeout=10)
        if not response.ok:
            print(f"[ERROR] Digital Rapids HTTP {response.status_code}")
            return []

        jobs = _parse_jobs(response.text, keywords)
        print(f"[OK] {len(jobs)} matching jobs found on Digital Rapids")
        return jobs

    except Exception as e:
        print(f"[ERROR] Digital Rapids scraping failed: {e}")
        return []


if __name__ == "__main__":
    results = scrape_digital_rapids(["Data Analyst", "Python"])
    print(f"\n{len(results)} jobs found.")
