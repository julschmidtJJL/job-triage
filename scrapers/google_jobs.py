"""
Google Jobs scraper — uses ScaleSerp (api.scaleserp.com) for web search.
ScaleSerp has no native google_jobs API, so it runs site: queries against
German job boards and extracts individual postings.
"""
import os
import re

import requests

SCALESERP_URL = "https://api.scaleserp.com/search"

# Job boards that return individual postings
JOB_SITES = (
    "site:xing.com/jobs "
    "OR site:monster.de/jobs "
    "OR site:karriere.de/stellenanzeige"
)

# Regex patterns for aggregation/listing pages (not individual postings)
_LISTING_PATTERNS = [
    re.compile(r"\b\d{1,4}\b.{0,30}\bjobs?\b", re.I),           # "25 CFO Jobs" / "94 Jobs"
    re.compile(r"\b\d{1,4}\b.{0,30}\bstellenangebote\b", re.I),  # "19 Stellenangebote"
    re.compile(r"\bjobs?\s*&\s*stellenangebote\b", re.I),         # "Jobs & Stellenangebote"
    re.compile(r"\b\d{1,4}\b.{0,30}\boffene\s+stellen\b", re.I), # "25 offene Stellen"
    re.compile(r"\bstellenangebote\s+in\b", re.I),                # "Stellenangebote in Hamburg"
    re.compile(r"\btop\s*-?\s*jobs?\s+des\s+tages\b", re.I),     # "Top-Jobs des Tages"
    re.compile(r"\bjetzt\s+\d+\s+offene\b", re.I),               # "Jetzt 25 offene"
    re.compile(r"\bjobs?\s+in\s+\w", re.I),                      # "Jobs in Hamburg"
    re.compile(r"\bstellen\s+in\s+\w", re.I),                    # "Stellen in Hamburg"
    re.compile(r"\bjobs?\s+fuer\s+\w", re.I),                    # "Jobs fuer CFO"
    re.compile(r"\b\d{1,3}\s+top-?jobs?\b", re.I),               # "94 Top-Jobs"
]

# Months and year suffixes that are not company names
_NOT_COMPANY_PATTERNS = [
    re.compile(r"^\w{3,9}\s+202\d$"),   # "März 2026", "Apr 2026"
    re.compile(r"^20[12]\d$"),           # "2024", "2025"
    re.compile(r"^\d+$"),               # bare number
]


def _looks_like_company(text: str) -> bool:
    text = text.strip()
    if len(text) < 3 or len(text) > 80:
        return False
    for pat in _NOT_COMPANY_PATTERNS:
        if pat.match(text):
            return False
    return True


def _is_listing_page(title: str) -> bool:
    for pat in _LISTING_PATTERNS:
        if pat.search(title):
            return True
    return False


def _extract_company(title: str, snippet: str, domain: str) -> str:
    """Try to extract the company name from the title or snippet."""
    # Xing/Monster: title often ends with " - Company Name" or " bei Company Name"
    for sep in [" - ", " bei ", " | ", " @ "]:
        if sep in title:
            parts = title.split(sep)
            if len(parts) >= 2:
                candidate = parts[-1].strip()
                if _looks_like_company(candidate) and not _is_listing_page(candidate):
                    return candidate
    # Fallback: use the domain as the company name
    domain_clean = domain.replace("www.", "").split(".")[0].capitalize()
    return domain_clean


def scrape_google_jobs(
    keywords: list, location: str = "Hamburg, Deutschland", radius: int = 100
) -> list:
    api_key = os.environ.get("SERPAPI_KEY")
    if not api_key:
        print("[INFO] SERPAPI_KEY not set — Google Jobs skipped.")
        return []

    city = location.split(",")[0].strip()
    all_jobs = []
    seen_urls: set = set()

    # Bundle queries in batches of 3 to save credits
    batch_size = 3
    batches = [keywords[i:i + batch_size] for i in range(0, len(keywords), batch_size)]

    for batch in batches:
        kw_query = " OR ".join(f'"{kw}"' for kw in batch)
        query = f"({kw_query}) {city} ({JOB_SITES})"
        print(f"[INFO] Google Jobs via ScaleSerp: {', '.join(batch)} ({city})")

        try:
            params = {
                "api_key": api_key,
                "q": query,
                "gl": "de",
                "hl": "de",
                "num": "10",
            }
            response = requests.get(SCALESERP_URL, params=params, timeout=15)
            if not response.ok:
                print(f"[ERROR] ScaleSerp HTTP {response.status_code}")
                continue

            data = response.json()
            batch_count = 0

            for item in data.get("organic_results", []):
                title = item.get("title", "").strip()
                url = item.get("link", "").strip()
                snippet = item.get("snippet", "").strip()
                domain = item.get("domain", "")

                if not title or not url:
                    continue
                if url in seen_urls:
                    continue
                if _is_listing_page(title):
                    continue

                # Query check: at least one query from the batch must appear in the title
                kw_match = next((kw for kw in batch if kw.lower() in title.lower()), None)
                if not kw_match:
                    continue

                seen_urls.add(url)
                company = _extract_company(title, snippet, domain)
                # Clean the title: drop the company suffix if we extracted one
                clean_title = title
                for sep in [" - ", " | ", " @ "]:
                    if sep in clean_title:
                        parts = clean_title.split(sep)
                        if parts[-1].strip() == company:
                            clean_title = sep.join(parts[:-1]).strip()
                            break

                all_jobs.append({
                    "title": clean_title,
                    "company": company,
                    "location": location,
                    "date_posted": "",
                    "url": url,
                    "description": snippet,
                    "source": "google_jobs",
                    "query": kw_match,
                })
                batch_count += 1

            print(f"[OK] {batch_count} jobs found for: {', '.join(batch)}")

        except Exception as e:
            print(f"[ERROR] Google Jobs failed: {e}")

    return all_jobs


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    results = scrape_google_jobs(["CFO", "Head of Finance"], "Hamburg, Deutschland", 100)
    print(f"\n{len(results)} jobs found.")
    for r in results[:5]:
        print(f"  [{r['location']}] {r['title']} @ {r['company']}")
        print(f"   {r['url']}")
