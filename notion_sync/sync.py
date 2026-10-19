import json
import re
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from notion_client import APIResponseError, Client

from notion_sync.feedback import DECISION_AUTO_DROPPED, DECISION_NEW

DATA_DIR = Path(__file__).parent.parent / "data"
SEEN_URLS_FILE = DATA_DIR / "jobs_seen_urls.json"

SOURCE_MAP = {
    "linkedin": "LinkedIn",
    "google_jobs": "Google Jobs",
    "hamburg_karriere": "Hamburg Karriere",
    "good_jobs": "Good Jobs",
    "green_jobs": "Green Jobs",
    "digital_rapids": "Digital Rapids",
    "arbeitsagentur": "Arbeitsagentur",
}


_GENDER_MARKER = re.compile(r"\(\s*(?:[mwfdxgn]\s*[/|,]?\s*){1,4}\)|\(\s*all genders\s*\)|\(\s*alle geschlechter\s*\)", re.IGNORECASE)


def _job_key(title: str, company: str) -> tuple[str, str]:
    """Same job, new link: employers repost on LinkedIn under a new job ID,
    so the board is also checked by normalized title + company. Gender
    markers are dropped, because reposts switch between (m/w/d) and (w/m/d)."""
    from utils.text_normalize import normalize_company, normalize_title
    title = _GENDER_MARKER.sub(" ", title or "")
    return normalize_title(title), normalize_company(company or "")


def _plain(prop: dict, kind: str) -> str:
    return "".join(item.get("plain_text", "") for item in (prop or {}).get(kind, []) or [])


def _get_existing(notion: Client, ds_id: str) -> tuple[set, set]:
    """(URLs, (title, company) keys) of every row already on the board."""
    existing_urls: set = set()
    existing_keys: set = set()
    try:
        cursor = None
        while True:
            kwargs: dict = {"page_size": 100}
            if cursor:
                kwargs["start_cursor"] = cursor
            data = notion.data_sources.query(ds_id, **kwargs)
            for page in data.get("results", []):
                props = page.get("properties", {})
                url = (props.get("URL", {}) or {}).get("url")
                if url:
                    existing_urls.add(url)
                title = _plain(props.get("Job Title"), "title")
                if title:
                    existing_keys.add(_job_key(title, _plain(props.get("Company"), "rich_text")))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
    except Exception as e:
        print(f"[WARN] Could not load existing URLs: {e}")
    return existing_urls, existing_keys


def _get_existing_urls(notion: Client, ds_id: str) -> set:
    return _get_existing(notion, ds_id)[0]


def _option(name: str) -> str:
    """Notion select and multi-select options may not contain commas and are
    capped at 100 characters. Claude's skill names sometimes have commas
    ("LLM APIs (OpenAI, Claude)"), and one bad option rejects the whole page."""
    return name.replace(",", " /").strip()[:100]


def _build_properties(job: dict) -> dict:
    portal = SOURCE_MAP.get(job.get("source", ""), job.get("source", ""))
    keywords = [{"name": _option(kw)} for kw in job.get("matched_keywords", [])[:5] if kw]
    ai_skills = [{"name": _option(sk)} for sk in job.get("ai_matched_skills", [])[:3] if sk]
    sources_text = ", ".join(job.get("sources", [])) if job.get("sources") else ""
    duplicate_status = job.get("duplicate_status", "")
    fetch_method = job.get("fetch_method", "snippet") or "snippet"
    scoring_method = job.get("scoring_method", "keyword_fallback") or "keyword_fallback"

    props = {
        "Job Title": {"title": [{"text": {"content": job.get("title", "")}}]},
        "Company": {"rich_text": [{"text": {"content": job.get("company", "")}}]},
        "Location": {"rich_text": [{"text": {"content": job.get("location", "")}}]},
        "Portal": {"select": {"name": _option(portal)}},
        "URL": {"url": job.get("url") or None},
        "Relevance Score": {"number": job.get("relevance_score", 0)},
        "Matched Keywords": {"multi_select": keywords},
        "Status": {"select": {"name": DECISION_NEW}},
        "Decision": {"select": {"name": DECISION_NEW}},
        "Scoring Method": {"select": {"name": scoring_method}},
        "Fetch Method": {"select": {"name": fetch_method}},
    }

    if job.get("query"):
        props["Query"] = {"select": {"name": _option(job["query"])}}
    if ai_skills:
        props["AI Matched Skills"] = {"multi_select": ai_skills}
    if sources_text:
        props["Sources"] = {"rich_text": [{"text": {"content": sources_text[:2000]}}]}
    if duplicate_status:
        props["Duplicate Status"] = {"select": {"name": duplicate_status}}

    return props


def _text_block(text: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]}}


def _heading_block(text: str) -> dict:
    return {"object": "block", "type": "heading_3", "heading_3": {"rich_text": [{"type": "text", "text": {"content": text}}]}}


def _divider_block() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def _build_children(job: dict) -> list:
    blocks = []
    ai_assessment = (job.get("ai_assessment") or "").strip()
    situation_fit = (job.get("situation_fit") or "").strip()
    ai_skills = [sk for sk in job.get("ai_matched_skills", [])[:10] if sk]
    keywords = [kw for kw in job.get("matched_keywords", []) if kw]

    if ai_assessment:
        blocks.append(_heading_block("AI Assessment"))
        for chunk in [ai_assessment[i:i+2000] for i in range(0, min(len(ai_assessment), 6000), 2000)]:
            blocks.append(_text_block(chunk))

    if situation_fit:
        if blocks:
            blocks.append(_divider_block())
        blocks.append(_heading_block("Situation Fit"))
        blocks.append(_text_block(situation_fit[:2000]))

    if ai_skills:
        if blocks:
            blocks.append(_divider_block())
        blocks.append(_heading_block("Matched Skills"))
        blocks.append(_text_block("  •  ".join(ai_skills)))

    if keywords:
        if blocks:
            blocks.append(_divider_block())
        blocks.append(_heading_block("Matched Keywords"))
        blocks.append(_text_block("  •  ".join(keywords)))

    return blocks


def sync_jobs(jobs: list) -> dict:
    api_key = os.environ.get("NOTION_API_KEY", "").strip()
    db_id = os.environ.get("NOTION_JOB_BOARD_DB_ID", "").replace("-", "").strip()
    ds_id = os.environ.get("NOTION_DATA_SOURCE_ID", "").replace("-", "").strip()

    print(f"[INFO] Notion DB ID: {db_id} ({len(db_id)} chars)")

    if not api_key or not db_id or len(db_id) != 32:
        print("[ERROR] NOTION_JOB_BOARD_DB_ID missing or invalid — sync skipped")
        return {"added": 0, "skipped": 0, "failed": len(jobs), "failed_jobs": list(jobs)}
    if not ds_id:
        print("[ERROR] NOTION_DATA_SOURCE_ID missing — sync skipped")
        return {"added": 0, "skipped": 0, "failed": len(jobs), "failed_jobs": list(jobs)}

    notion = Client(auth=api_key)
    existing_urls, existing_keys = _get_existing(notion, ds_id)

    added = 0
    skipped = 0
    failed_jobs: list = []

    for job in jobs:
        job_url = job.get("url", "")
        key = _job_key(job.get("title", ""), job.get("company", ""))
        if (job_url and job_url in existing_urls) or (key[0] and key in existing_keys):
            print(f"[INFO] Duplicate skipped: {job.get('title', '')} @ {job.get('company', '')}")
            skipped += 1
            continue

        for attempt in range(2):
            try:
                children = _build_children(job)
                create_kwargs: dict = {
                    "parent": {"database_id": db_id},
                    "properties": _build_properties(job),
                }
                if children:
                    create_kwargs["children"] = children
                notion.pages.create(**create_kwargs)
                existing_urls.add(job_url)
                existing_keys.add(key)
                added += 1
                print(f"[OK] Notion page created: {job.get('title', '')} @ {job.get('company', '')}")
                break
            except APIResponseError as e:
                if e.status == 429 and attempt == 0:
                    time.sleep(1.0)
                    continue
                print(f"[ERROR] Notion upload failed for '{job.get('title', '')}': {e}")
                failed_jobs.append(job)
                break
            except Exception as e:
                print(f"[ERROR] Notion upload failed for '{job.get('title', '')}': {e}")
                failed_jobs.append(job)
                break

        time.sleep(0.4)

    print(f"[INFO] Sync done: {added} new, {skipped} skipped, {len(failed_jobs)} failed")
    return {"added": added, "skipped": skipped, "failed": len(failed_jobs), "failed_jobs": failed_jobs}


# ---------------------------------------------------------------- auto-dropped


def _write_sidecar(dropped_jobs: list) -> None:
    """Extend jobs_seen_urls.json: for each dropped job, store the dropped_token
    + pipeline so the rescue path (utils/stoplist.apply_rescue) can find it
    by URL later. Preserves the existing dict (URL → timestamp) format and
    upgrades selected entries to dicts.
    """
    if not dropped_jobs:
        return
    cache: dict = {}
    if SEEN_URLS_FILE.exists():
        try:
            raw = json.loads(SEEN_URLS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cache = raw
        except json.JSONDecodeError:
            cache = {}

    now = datetime.now(timezone.utc).isoformat()
    for job in dropped_jobs:
        url = job.get("url", "")
        if not url:
            continue
        cache[url] = {
            "first_seen": cache.get(url) if isinstance(cache.get(url), str) else now,
            "dropped_token": job.get("stop_token", "") or "",
            "dropped_pipeline": job.get("stop_pipeline", "") or "",
            "dropped_at": now,
            "stop_reason": job.get("stop_reason", "") or "",
        }

    DATA_DIR.mkdir(exist_ok=True)
    SEEN_URLS_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_auto_dropped_properties(job: dict) -> dict:
    """Properties for an Auto-Dropped Notion row. Minimal — Stop Reason carries
    the human-readable why; the structured rescue token lives in the sidecar."""
    portal = SOURCE_MAP.get(job.get("source", ""), job.get("source", ""))
    stop_reason = job.get("stop_reason", "") or ""
    props = {
        "Job Title": {"title": [{"text": {"content": job.get("title", "")}}]},
        "Company": {"rich_text": [{"text": {"content": job.get("company", "")}}]},
        "Location": {"rich_text": [{"text": {"content": job.get("location", "")}}]},
        "URL": {"url": job.get("url") or None},
        "Relevance Score": {"number": job.get("relevance_score", 0)},
        "Status": {"select": {"name": DECISION_AUTO_DROPPED}},
        "Decision": {"select": {"name": DECISION_AUTO_DROPPED}},
        "Stop Reason": {"rich_text": [{"text": {"content": stop_reason[:1900]}}]},
    }
    if portal:
        props["Portal"] = {"select": {"name": portal}}
    return props


def sync_auto_dropped(jobs: list) -> dict:
    """Sync auto-dropped jobs to Notion ONCE per URL. Writes Stop Reason for
    you to see, and the sidecar so rescue (Decision → Interesting) works.

    Per the design, this is a no-op when shadow mode is on (caller controls
    by not passing dropped jobs through to this function in shadow mode)."""
    if not jobs:
        return {"added": 0, "skipped": 0}

    api_key = os.environ.get("NOTION_API_KEY", "").strip()
    db_id = os.environ.get("NOTION_JOB_BOARD_DB_ID", "").replace("-", "").strip()
    ds_id = os.environ.get("NOTION_DATA_SOURCE_ID", "").replace("-", "").strip()

    # Always write the sidecar — even without Notion creds, the sidecar enables
    # local-only rescue testing.
    _write_sidecar(jobs)

    if not api_key or not db_id or len(db_id) != 32 or not ds_id:
        print("[INFO] Auto-Dropped Notion sync skipped (Notion env incomplete)")
        return {"added": 0, "skipped": 0}

    notion = Client(auth=api_key)
    existing_urls = _get_existing_urls(notion, ds_id)

    added = 0
    skipped = 0
    for job in jobs:
        url = job.get("url", "")
        if url and url in existing_urls:
            skipped += 1
            continue
        try:
            notion.pages.create(
                parent={"database_id": db_id},
                properties=_build_auto_dropped_properties(job),
            )
            existing_urls.add(url)
            added += 1
            print(f"[AUTO-DROP] Synced: {job.get('title', '')} @ {job.get('company', '')} ({job.get('stop_reason', '')})")
        except APIResponseError as e:
            print(f"[ERROR] Auto-Dropped sync for '{job.get('title', '')}': {e}")
        except Exception as e:
            print(f"[ERROR] Auto-Dropped sync for '{job.get('title', '')}': {e}")
        time.sleep(0.4)

    return {"added": added, "skipped": skipped}
