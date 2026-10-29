"""The Notion board: a database whose rows are the jobs (README → Notion setup).

Writing jobs and reading decisions live in notion_sync/; this module adds the
letter step: read the Apply rows and the ad stored in each row's page body, and
put a letter on a row as a child page "Anschreiben – <Company>".
"""
import os
import time

from notion_client import APIResponseError, Client

from job_board import APPLY, LETTER_TITLE, MAX_AD_CHARS, LetterPage
from notion_sync import feedback, sync
from notion_sync.sync import _ad_blocks, _divider_block, _plain, _text_block

# Notion: at most 100 blocks in one create request.
_MAX_PAGE_BLOCKS = 100


class NotionBoard:
    label = "Notion"

    def __init__(self):
        self._notion = None

    def _client(self) -> Client:
        if self._notion is None:
            self._notion = Client(auth=os.environ["NOTION_API_KEY"])
        return self._notion

    def sync_jobs(self, jobs: list) -> dict:
        return sync.sync_jobs(jobs)

    def sync_auto_dropped(self, jobs: list) -> dict:
        return sync.sync_auto_dropped(jobs)

    def fetch_decisions(self, lookback_days: int = feedback.LOOKBACK_DAYS_DEFAULT) -> feedback.DecisionHistory:
        return feedback.fetch_decisions_from_env(lookback_days=lookback_days)

    def letter_rows(self, row_ids: list[str] = ()) -> tuple[int, list[dict], list[tuple[dict, str]]]:
        notion = self._client()
        if row_ids:
            pages = [notion.pages.retrieve(pid) for pid in row_ids]
        else:
            pages = apply_rows(notion, os.environ["NOTION_DATA_SOURCE_ID"].replace("-", "").strip())
        todo, skipped = select_rows(notion, pages)
        return len(pages), todo, skipped

    def add_letter(self, row: dict, page: LetterPage) -> str:
        return write_letter(self._client(), row, page_blocks(page))


# ---------------------------------------------------------------- reading the rows


def _list_blocks(notion: Client, page_id: str) -> list[dict]:
    blocks: list[dict] = []
    cursor = None
    while True:
        kwargs: dict = {"page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        data = notion.blocks.children.list(page_id, **kwargs)
        blocks.extend(data.get("results", []))
        if not data.get("has_more"):
            return blocks
        cursor = data.get("next_cursor")


def has_letter(blocks: list[dict]) -> bool:
    """A child page titled "Anschreiben…" means the row has a letter (drafted
    here or by hand), so it is never drafted twice."""
    return any(b.get("type") == "child_page"
               and (b.get("child_page") or {}).get("title", "").startswith(LETTER_TITLE)
               for b in blocks)


def _block_text(block: dict) -> str:
    body = block.get(block.get("type", "")) or {}
    return "".join(t.get("plain_text", "") for t in body.get("rich_text", []) or [])


def body_sections(blocks: list[dict]) -> dict[str, str]:
    """The page body as {heading: text}, one entry per heading_3 section."""
    sections: dict[str, list[str]] = {}
    current = None
    for block in blocks:
        kind = block.get("type")
        if kind == "heading_3":
            current = _block_text(block).strip()
            sections[current] = []
        elif current and kind == "paragraph":
            sections[current].append(_block_text(block))
    return {name: "\n\n".join(p for p in parts if p.strip()) for name, parts in sections.items()}


def row_facts(page: dict) -> dict:
    props = page.get("properties", {})

    def select(name: str) -> str:
        return ((props.get(name) or {}).get("select") or {}).get("name", "")

    return {
        "row_id": page.get("id", ""),
        "page_id": page.get("id", ""),
        "title": _plain(props.get("Job Title"), "title"),
        "company": _plain(props.get("Company"), "rich_text").strip(),
        "location": _plain(props.get("Location"), "rich_text"),
        "url": (props.get("URL") or {}).get("url") or "",
        "decision": select("Decision"),
        "score": (props.get("Relevance Score") or {}).get("number"),
        "ad_language": select("Ad Language") or "German",
        "seniority": select("Seniority"),
        "required_years": (props.get("Required Years") or {}).get("number"),
        "contact_person": _plain(props.get("Contact Person"), "rich_text"),
        "hard_requirements": _plain(props.get("Hard Requirements"), "rich_text"),
        "requirements_flexible": bool((props.get("Requirements Flexible") or {}).get("checkbox")),
        "unmet_requirements": _plain(props.get("Unmet Requirements"), "rich_text"),
    }


def apply_rows(notion: Client, ds_id: str) -> list[dict]:
    flt = {"property": "Decision", "select": {"equals": APPLY}}
    pages: list[dict] = []
    cursor = None
    while True:
        kwargs: dict = {"filter": flt, "page_size": 100}
        if cursor:
            kwargs["start_cursor"] = cursor
        data = notion.data_sources.query(ds_id, **kwargs)
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            return pages
        cursor = data.get("next_cursor")


def select_rows(notion: Client, pages: list[dict]) -> tuple[list[dict], list[tuple[dict, str]]]:
    """(rows to draft, each with its ad and requirements; (row, reason) skipped)."""
    todo, skipped = [], []
    for page in pages:
        row = row_facts(page)
        if not row["company"]:
            skipped.append((row, "no company on the row"))
            continue
        blocks = _list_blocks(notion, row["page_id"])
        if has_letter(blocks):
            skipped.append((row, "already has an Anschreiben page"))
            continue
        sections = body_sections(blocks)
        if not sections.get("Job Ad"):
            reason = ("only a search snippet on the row" if "Job Ad (search snippet only)" in sections
                      else "no Job Ad block on the row")
            skipped.append((row, reason + "; paste the full ad under a 'Job Ad' heading to get a letter"))
            continue
        row["job_ad"] = sections["Job Ad"][:MAX_AD_CHARS]
        row["requirements_block"] = sections.get("Requirements", "")
        todo.append(row)
    return todo, skipped


# ---------------------------------------------------------------- writing the letter


def _bold_block(text: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
        {"type": "text", "text": {"content": text}, "annotations": {"bold": True}}]}}


def _callout_block(lines: list[str]) -> dict:
    return {"object": "block", "type": "callout", "callout": {
        "icon": {"type": "emoji", "emoji": "💡"}, "color": "gray_background",
        # One text object per line: each may hold 2000 characters.
        "rich_text": [{"type": "text", "text": {"content": (line + "\n" if i < len(lines) - 1 else line)[:2000]}}
                      for i, line in enumerate(lines)]}}


def page_blocks(page: LetterPage) -> list[dict]:
    blocks = [_callout_block(page.callout), _divider_block(), _text_block(page.date_line),
              _bold_block(page.subject), _text_block(page.salutation)]
    for paragraph in page.paragraphs:
        blocks.extend(_ad_blocks(paragraph))
    blocks.append(_text_block(page.closing))
    return blocks


def write_letter(notion: Client, row: dict, blocks: list[dict]) -> str:
    """Create the letter page under the row. Checks the row again first, so a
    run that overlaps another (schedule plus a manual run) does not add a second."""
    if len(blocks) > _MAX_PAGE_BLOCKS:
        raise ValueError(f"{len(blocks)} blocks, Notion takes at most {_MAX_PAGE_BLOCKS} per page create")
    if has_letter(_list_blocks(notion, row["page_id"])):
        raise ValueError("the row got an Anschreiben page during this run; not writing a second")
    for attempt in range(2):
        try:
            page = notion.pages.create(
                parent={"page_id": row["page_id"]},
                icon={"type": "emoji", "emoji": "✉️"},
                properties={"title": {"title": [{"text": {"content": f"{LETTER_TITLE} – {row['company']}"}}]}},
                children=blocks,
            )
            time.sleep(0.4)
            return page.get("url", page.get("id", ""))
        except APIResponseError as e:
            if e.status == 429 and attempt == 0:
                time.sleep(1.0)
                continue
            raise
