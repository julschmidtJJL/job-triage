"""Draft a base cover letter for every Apply row that has none yet.

For each board row with Decision = Apply and no letter yet: read the full ad
stored with the row (the Requirements and Job Ad sections, Hard Requirements,
Unmet Requirements, Contact Person, Ad Language), draft a letter in the ad's
language against the fit profile and the letter facts, and put it on the board.
On Notion (BOARD=notion, the default) it is a child page "Anschreiben – <Company>"
of the row; with BOARD=csv it is board/letters/<company>.md. A short note above
the letter gives the honest odds and which ad bullets the letter covers and which
it doesn't.

The letter is a draft: it opens with a placeholder for your motive
([Warum <Company>, in Ihren eigenen Worten.] / [Why <Company>, in your own words.])
and leaves the start date as [Datum] / [Start date]. Rows whose ad was never
fetched (no "Job Ad" section) are skipped: a letter from a search snippet misses
the language, the contact and the real requirements.

The letter facts (profile/letter_facts.md or the LETTER_FACTS secret; shape in
profile/letter_facts.example.md) are the only facts a letter may state. Their
"Letter frame" section gives the name and city for the signature and date line;
any phrase in backticks under "Never write" is checked in every draft.

    python scripts/draft_letters.py                  # draft and write
    python scripts/draft_letters.py --dry-run        # draft and print, write nothing
    python scripts/draft_letters.py --row <id>       # one row, whatever its Decision
                                                     # (a Notion page ID or a jobs.csv Key)
"""
import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel

from job_board import LetterPage, get_board
from job_board.notion_board import page_blocks
from utils.profile import load_letter_facts, load_profile_text

DATA_DIR = Path(__file__).parent.parent / "data"
DEFAULT_LETTER_MODEL = "claude-sonnet-5-5"
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


def letter_page(draft: LetterDraft, row: dict, today: date, frame: dict | None = None) -> LetterPage:
    """frame: name and city from the letter facts (letter_frame())."""
    frame = frame or {}
    english = _english(row)
    subject = (f"Application for {row['title']}" if english else f"Bewerbung als {row['title']}")
    name = frame.get("name") or "[Name]"
    closing = f"Yours sincerely,\n{name}" if english else f"Mit freundlichen Grüßen\n{name}"
    return LetterPage(company=row["company"], callout=callout_lines(draft, row, today),
                      date_line=_date_line(english, today, frame.get("city", "")), subject=subject,
                      salutation=draft.salutation.strip(), paragraphs=list(draft.paragraphs), closing=closing)


def letter_blocks(draft: LetterDraft, row: dict, today: date, frame: dict | None = None) -> list[dict]:
    """The letter as Notion blocks."""
    return page_blocks(letter_page(draft, row, today, frame))


def letter_text(blocks: list[dict]) -> str:
    """The page as plain text, for the dry run and the saved JSON."""
    return "\n\n".join("".join(t["text"]["content"] for t in b[b["type"]]["rich_text"])
                       for b in blocks if b["type"] != "divider")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="draft and print, write nothing to the board")
    parser.add_argument("--row", "--page-id", dest="rows", action="append", default=[],
                        help="draft for this board row whatever its Decision: a Notion page ID "
                             "or a jobs.csv Key (repeatable)")
    parser.add_argument("--limit", type=int, help="draft at most this many letters")
    args = parser.parse_args(argv)
    load_dotenv(Path(__file__).parent.parent / ".env")

    board = get_board()
    read, todo, skipped = board.letter_rows(args.rows)
    if args.limit is not None:
        todo = todo[:args.limit]
    print(f"[INFO] {read} rows read, {len(todo)} to draft, {len(skipped)} skipped")
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
            page = letter_page(draft, row, today, frame)
            entry = {"row_id": row["row_id"], "title": row["title"], "company": row["company"],
                     "draft": draft.model_dump(), "text": page.text()}
            if args.dry_run:
                print(f"\n■ {label}\n{entry['text']}\n")
            else:
                entry["letter_url"] = board.add_letter(row, page)
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
