"""Draft a base cover letter for every Apply row that has none yet.

For each board row with Decision = Apply and no "Anschreiben" child page: read the
full ad stored on the row (the Requirements and Job Ad blocks, Hard Requirements,
Unmet Requirements, Contact Person, Ad Language), draft a letter in the ad's
language against the fit profile and the letter facts, and put it on the row as
a child page "Anschreiben – <Company>". A short callout above the letter gives the
honest odds and which ad bullets the letter covers and which it doesn't.

The letter is a draft: it opens with a placeholder for your motive
([Warum <Company>, in Ihren eigenen Worten.] / [Why <Company>, in your own words.])
and leaves the start date as [Datum] / [Start date]. Rows whose ad was never
fetched (no "Job Ad" block) are skipped: a letter from a search snippet misses
the language, the contact and the real requirements.

The letter facts (profile/letter_facts.md or the LETTER_FACTS secret; shape in
profile/letter_facts.example.md) are the only facts a letter may state. Their
"Letter frame" section gives the name and city for the signature and date line;
any phrase in backticks under "Never write" is checked in every draft.

    python scripts/draft_letters.py                  # draft and write
    python scripts/draft_letters.py --dry-run        # draft and print, write nothing
    python scripts/draft_letters.py --page-id <row>  # one row, whatever its Decision
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic
from dotenv import load_dotenv
from notion_client import APIResponseError, Client
from pydantic import BaseModel

from notion_sync.sync import _ad_blocks, _divider_block, _plain, _text_block
from utils.profile import load_letter_facts, load_profile_text

DATA_DIR = Path(__file__).parent.parent / "data"
DECISION = "Apply"
LETTER_TITLE = "Anschreiben"
DEFAULT_LETTER_MODEL = "claude-sonnet-5-5"
# The longest ads run to ~20k characters; this only guards against a runaway page.
_MAX_AD_CHARS = 40000
_MONTHS_DE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
              "September", "Oktober", "November", "Dezember"]


def letter_model() -> str:
    return os.getenv("LETTER_MODEL", DEFAULT_LETTER_MODEL)


def _english(row: dict) -> bool:
    return row["ad_language"].lower().startswith("en")


def placeholder(company: str, english: bool = False) -> str:
    if english:
        return f"[Why {company}, in your own words.]"
    return f"[Warum {company}, in Ihren eigenen Worten.]"


def start_date(english: bool) -> str:
    return "[Start date]" if english else "[Datum]"


# ---------------------------------------------------------------- the letter facts


def _section(facts: str, heading: str) -> str:
    """The text under a "## <heading>" line of the letter facts, up to the next heading."""
    match = re.search(rf"^##\s+{re.escape(heading)}\b.*?$(.*?)(?=^##\s|\Z)", facts or "",
                      re.IGNORECASE | re.MULTILINE | re.DOTALL)
    return match.group(1) if match else ""


def letter_frame(facts: str) -> dict[str, str]:
    """Name and city from the "Letter frame" section ("- Name: ...", "- City: ...")."""
    frame = {}
    for key in ("name", "city"):
        match = re.search(rf"^\s*[-*]?\s*{key}\s*:\s*(.+?)\s*$", _section(facts, "Letter frame"),
                          re.IGNORECASE | re.MULTILINE)
        if match:
            frame[key] = match.group(1)
    return frame


def never_write(facts: str) -> list[str]:
    """The phrases in backticks under "Never write": each draft is checked for them."""
    return [p.strip() for p in re.findall(r"`([^`\n]+)`", _section(facts, "Never write")) if p.strip()]


# ---------------------------------------------------------------- the board


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
    flt = {"property": "Decision", "select": {"equals": DECISION}}
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
        row["job_ad"] = sections["Job Ad"][:_MAX_AD_CHARS]
        row["requirements_block"] = sections.get("Requirements", "")
        todo.append(row)
    return todo, skipped


# ---------------------------------------------------------------- the letter


class LetterDraft(BaseModel):
    salutation: str
    paragraphs: list[str]
    odds: str
    covered: list[str]
    not_covered: list[str]
    checks: list[str]


_LETTER_SYSTEM = (
    "You draft a base cover letter for the candidate described below. They will rewrite "
    "and lay it out themselves, so every sentence must be true and checkable.\n\n"
    "<letter_facts>\n{facts}\n</letter_facts>\n\n"
    "<fit_profile>\n{profile}\n</fit_profile>\n\n"
    "The letter facts are the only facts you may state about the candidate. The fit profile "
    "tells you what they look for and how they work; where it disagrees with the letter facts "
    "(dates, grades), the letter facts win. Never invent experience, tools, numbers, results "
    "or motives, and never write anything the letter facts list under \"Never write\".\n\n"
    "Rules:\n"
    "- Write the letter in {language}. If the letter facts include a base letter in that "
    "language, follow its order and tone; otherwise open with what the candidate would "
    "bring, then the work that best matches the ad, then their education, then the start "
    "date and close. Tie each paragraph to what this ad asks for, using the ad's own terms "
    "where they are true of the candidate. About 250 to 350 words. No bullet points, no "
    "headings, no bold.\n"
    "- The first paragraph begins with the literal text {placeholder} and then one or two "
    "sentences on what the candidate would bring to this role, fitted to what the role is "
    "about. Never write why they want the job or the company: that is theirs to write.\n"
    "- The start date is the literal text {start_date}. The date line, subject line, closing "
    "formula and the candidate's name are added for you: return only the salutation and the "
    "body paragraphs.\n"
    "- Salutation: address the named contact if there is one. In German use \"Sehr geehrte "
    "Frau X,\" or \"Sehr geehrter Herr X,\" only when the ad or the contact field itself says "
    "Frau or Herr; otherwise \"Guten Tag <first name> <last name>,\". Never guess a gender "
    "from a first name. In English use \"Dear <first name> <last name>,\". With no named "
    "contact: \"Sehr geehrte Damen und Herren,\" / \"Dear Hiring Team,\".\n"
    "- If the row lists unmet requirements, name the gap honestly, once, in the paragraph "
    "before the closing paragraph, together with how the candidate would close it (based "
    "only on the letter facts, e.g. how they learned comparable things). Never in the "
    "opening, never apologetic, never repeated. With no unmet requirements, don't invent a "
    "gap.\n\n"
    "Also return, in English, for the candidate only (not part of the letter):\n"
    "- odds: one or two honest sentences on their realistic chances for this ad, naming the "
    "biggest gap. Don't soften it.\n"
    "- covered: up to six of the ad's main requirements and tasks the letter addresses, "
    "two to five words each, in the ad's wording.\n"
    "- not_covered: up to six of the ad's main requirements and tasks the letter does not "
    "address, same form.\n"
    "- checks: at most four short notes (one sentence each) on sentences they should verify "
    "before sending: a claim that stretches the facts, how they say they'd close the gap, "
    "the salutation if unsure. Don't list sentences that just repeat the letter facts."
)


def _row_input(row: dict) -> str:
    fields = {
        "job_title": row["title"],
        "company": row["company"],
        "location": row["location"],
        "ad_language": row["ad_language"],
        "seniority": row["seniority"],
        "required_years": row["required_years"],
        "contact_person": row["contact_person"] or "(none named)",
        "hard_requirements": row["hard_requirements"],
        "requirements_flexible": row["requirements_flexible"],
        "unmet_requirements": row["unmet_requirements"] or "(none)",
        "requirements_block": row["requirements_block"],
    }
    return (json.dumps(fields, ensure_ascii=False, indent=2)
            + "\n\n<job_ad>\n" + row["job_ad"] + "\n</job_ad>")


def draft_letter(client, row: dict, profile: str, facts: str) -> LetterDraft:
    english = _english(row)
    system = _LETTER_SYSTEM.format(facts=facts, profile=profile,
                                   language="English" if english else "German",
                                   placeholder=placeholder(row["company"], english),
                                   start_date=start_date(english))
    response = client.messages.parse(
        model=letter_model(),
        max_tokens=8000,
        output_config={"effort": "high"},
        system=system,
        messages=[{"role": "user", "content": _row_input(row)}],
        output_format=LetterDraft,
    )
    draft = response.parsed_output
    if draft is None:
        raise ValueError(f"no parsed output (stop_reason={response.stop_reason})")
    return enforce_rules(draft, row, never_write(facts))


# An amount before or after its currency: "15.000 €", "€15k", "EUR 15000", "$15,000".
_MONEY = re.compile(r"\d[\d.,]*\s?(€|EUR\b|Euro|USD\b|\$|k\b)|(€|\$|\bEUR|\bUSD)\s?\d", re.IGNORECASE)
# Notion: at most 100 blocks in one create request.
_MAX_PAGE_BLOCKS = 100


def enforce_rules(draft: LetterDraft, row: dict, banned: list[str] = ()) -> LetterDraft:
    """The rules the letter must keep whatever the model wrote. `banned`: the
    letter facts' "Never write" phrases, flagged wherever they appear."""
    english = _english(row)
    hold = placeholder(row["company"], english)
    paragraphs = [p.strip() for p in draft.paragraphs if p.strip()]
    if not paragraphs:
        raise ValueError("the model returned no letter text")
    first = paragraphs[0]
    if not first.startswith(hold):
        # Drop a reworded placeholder of the model's own, keep the rest of the paragraph.
        first = re.sub(r"^\[[^\]]*\]\s*", "", first)
        paragraphs = [f"{hold} {first}".strip()] + paragraphs[1:]
    checks = list(draft.checks)
    date_hold = start_date(english)
    if date_hold not in "\n".join(paragraphs):
        start = f"I can start on {date_hold}." if english else f"Ich stehe ab {date_hold} zur Verfügung."
        paragraphs[-1] = f"{start} {paragraphs[-1]}"
        checks.append(f"The model left out the start date; {date_hold} was added to the last paragraph.")
    text = "\n".join([draft.salutation] + paragraphs).lower()
    if _MONEY.search(text):
        checks.append("The letter mentions an amount of money: check it against the letter facts.")
    for phrase in banned:
        if phrase.lower() in text:
            checks.append(f'The letter says "{phrase}", which the letter facts list under Never write.')
    return draft.model_copy(update={"paragraphs": paragraphs, "salutation": draft.salutation.strip(),
                                    "checks": checks})


def _date_line(english: bool, today: date, city: str = "") -> str:
    if english:
        day = f"{today.day} {today.strftime('%B')} {today.year}"
    else:
        day = f"{today.day}. {_MONTHS_DE[today.month - 1]} {today.year}"
    return f"{city}, {day}" if city else day


def _bold_block(text: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
        {"type": "text", "text": {"content": text}, "annotations": {"bold": True}}]}}


def _callout_block(lines: list[str]) -> dict:
    return {"object": "block", "type": "callout", "callout": {
        "icon": {"type": "emoji", "emoji": "💡"}, "color": "gray_background",
        # One text object per line: each may hold 2000 characters.
        "rich_text": [{"type": "text", "text": {"content": (line + "\n" if i < len(lines) - 1 else line)[:2000]}}
                      for i, line in enumerate(lines)]}}


def callout_lines(draft: LetterDraft, row: dict, today: date) -> list[str]:
    lines = [f"Draft by {letter_model()}, {today.isoformat()}, from the ad on this row. "
             f"Still yours: {placeholder(row['company'], _english(row))} and {start_date(_english(row))}.",
             f"Odds: {draft.odds}"]
    if draft.covered:
        lines.append("Covers: " + "; ".join(draft.covered))
    if draft.not_covered:
        lines.append("Doesn't cover: " + "; ".join(draft.not_covered))
    if row["contact_person"]:
        lines.append(f"Contact: {row['contact_person']}")
    if draft.checks:
        lines.append("Check: " + " ".join(draft.checks))
    lines.append("Company website not checked.")
    return lines


def letter_blocks(draft: LetterDraft, row: dict, today: date, frame: dict | None = None) -> list[dict]:
    """frame: name and city from the letter facts (letter_frame())."""
    frame = frame or {}
    english = _english(row)
    subject = (f"Application for {row['title']}" if english else f"Bewerbung als {row['title']}")
    name = frame.get("name") or "[Name]"
    closing = f"Yours sincerely,\n{name}" if english else f"Mit freundlichen Grüßen\n{name}"
    blocks = [_callout_block(callout_lines(draft, row, today)), _divider_block(),
              _text_block(_date_line(english, today, frame.get("city", ""))), _bold_block(subject),
              _text_block(draft.salutation.strip())]
    for paragraph in draft.paragraphs:
        blocks.extend(_ad_blocks(paragraph))
    blocks.append(_text_block(closing))
    return blocks


def letter_text(blocks: list[dict]) -> str:
    """The page as plain text, for the dry run and the saved JSON."""
    return "\n\n".join("".join(t["text"]["content"] for t in b[b["type"]]["rich_text"])
                       for b in blocks if b["type"] != "divider")


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="draft and print, write nothing to Notion")
    parser.add_argument("--page-id", action="append", default=[],
                        help="draft for this board row whatever its Decision (repeatable)")
    parser.add_argument("--limit", type=int, help="draft at most this many letters")
    args = parser.parse_args(argv)
    load_dotenv(Path(__file__).parent.parent / ".env")

    notion = Client(auth=os.environ["NOTION_API_KEY"])
    if args.page_id:
        pages = [notion.pages.retrieve(pid) for pid in args.page_id]
    else:
        ds_id = os.environ["NOTION_DATA_SOURCE_ID"].replace("-", "").strip()
        pages = apply_rows(notion, ds_id)
    todo, skipped = select_rows(notion, pages)
    if args.limit is not None:
        todo = todo[:args.limit]
    print(f"[INFO] {len(pages)} rows read, {len(todo)} to draft, {len(skipped)} skipped")
    for row, reason in skipped:
        print(f"□ skipped: {row['title']} @ {row['company']}: {reason}")
    if not todo:
        return 0

    profile, facts = load_profile_text(), load_letter_facts()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    missing = [name for name, value in (("FIT_PROFILE", profile), ("LETTER_FACTS", facts),
                                        ("ANTHROPIC_API_KEY", api_key)) if not value]
    if missing:
        # Without the facts the letter would make them up; fail so the run shows red.
        print(f"[ERROR] {', '.join(missing)} missing: no letters drafted")
        return 1

    client = anthropic.Anthropic(api_key=api_key)
    frame = letter_frame(facts)
    today = date.today()
    results, failed = [], 0
    for row in todo:
        label = f"{row['title']} @ {row['company']}"
        try:
            draft = draft_letter(client, row, profile, facts)
            blocks = letter_blocks(draft, row, today, frame)
            entry = {"page_id": row["page_id"], "title": row["title"], "company": row["company"],
                     "draft": draft.model_dump(), "text": letter_text(blocks)}
            if args.dry_run:
                print(f"\n■ {label}\n{entry['text']}\n")
            else:
                entry["letter_url"] = write_letter(notion, row, blocks)
                print(f"[OK] Letter drafted: {label} → {entry['letter_url']}")
            results.append(entry)
        except Exception as e:
            failed += 1
            print(f"[ERROR] Letter failed for '{label}': {e}")

    DATA_DIR.mkdir(exist_ok=True)
    out = DATA_DIR / f"letters_{today}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    verb = "drafted (dry run, nothing written)" if args.dry_run else "written"
    print(f"[SUMMARY] {len(results)} letters {verb}, {failed} failed. Saved: {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
