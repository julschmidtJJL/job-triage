"""The board as plain files: no account, any spreadsheet app (BOARD=csv).

    board/jobs.csv               one row per job, the Notion board's columns;
                                 you type the Decision
    board/ads/<key>.md           what the scorer read: assessment, requirements, full ad
    board/letters/<key>.md       a cover-letter draft; an existing file means the
                                 row has a letter, so it is never drafted twice

Every write reads jobs.csv first, appends the jobs it doesn't have yet (matched
by URL or by title + company, like Notion) and leaves the rest as you left it:
your Decisions, your own columns, your column order. The one cell it fills in
is an empty Decided, with the date it first sees your Decision. The file is
read in the separator and encoding your spreadsheet app saved it in.

On Actions the workflow commits board/ back to the repository. If you push an
edit to jobs.csv while a run is going, `python -m job_board.csv_board merge`
(registered as a git merge driver in the workflow) merges the two cell by cell.
"""
import csv
import hashlib
import io
import os
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from job_board import APPLY, MAX_AD_CHARS, LetterPage
from notion_sync.feedback import (
    DECISION_APPLIED, DECISION_AUTO_DROPPED, DECISION_DECLINED, DECISION_INTERESTING, DECISION_MAYBE,
    DECISION_NEW, DECISION_NOT_INTERESTING, LOOKBACK_DAYS_DEFAULT, PIPELINE, Decision, DecisionHistory, _bucket,
)
from notion_sync.sync import _GENDER_MARKER, SOURCE_MAP, _job_key, _requirements_lines, _write_sidecar

BOARD_DIR = Path(__file__).parent.parent / "board"

KEY = "Key"
COLUMNS = [
    "Decision", "Relevance Score", "Job Title", "Company", "Location", "URL", "AI Assessment",
    "Unmet Requirements", "Hard Requirements", "Requirements Flexible", "Required Years",
    "Seniority", "Ad Language", "Contact Person", "Portal", "Query", "Matched Keywords",
    "AI Matched Skills", "Sources", "Status", "Scoring Method", "Fetch Method", "Duplicate Status",
    "Stop Reason", "Added", "Decided", KEY,
]
DECISIONS = [DECISION_NEW, DECISION_INTERESTING, DECISION_MAYBE, APPLY, DECISION_APPLIED,
             DECISION_NOT_INTERESTING, DECISION_DECLINED, DECISION_AUTO_DROPPED]
_CANONICAL = {d.lower(): d for d in DECISIONS}
_TRUE = {"yes", "y", "true", "1", "x", "ja", "wahr"}
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "ae", "Ö": "oe", "Ü": "ue"})


def canonical_decision(value: str) -> str:
    """"apply " and "Apply" are the same verdict; anything unknown is kept as typed."""
    value = (value or "").strip()
    return _CANONICAL.get(value.lower(), value)


def slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", (text or "").translate(_UMLAUTS))
    text = text.encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def job_key(title: str, company: str, url: str = "") -> str:
    """Readable and stable: the same title + company give the same key on every
    run, which is also what names the ad file."""
    norm_title, norm_company = _job_key(title, company)
    basis = f"{norm_title}|{norm_company}" if norm_title else url
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:6]
    readable = slug(f"{company} {_GENDER_MARKER.sub(' ', title or '')}")[:60].rstrip("-")
    return f"{readable}-{digest}" if readable else digest


def _row_key(row: dict) -> str:
    return row.get(KEY) or job_key(row.get("Job Title", ""), row.get("Company", ""), row.get("URL", ""))


# ---------------------------------------------------------------- the file


def read_table(path: Path) -> tuple[str, list[str], list[dict]]:
    """(separator, header, rows). Spreadsheet apps save in their own way: Excel
    in a German locale with ";" and sometimes in Windows-1252."""
    if not path.exists():
        return ",", list(COLUMNS), []
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
    if not text.strip():
        return ",", list(COLUMNS), []
    first = text.splitlines()[0]
    delimiter = max((",", ";", "\t"), key=first.count)
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)
    header = [h for h in (reader.fieldnames or []) if h]
    rows = []
    for raw_row in reader:
        row = {k: (v or "") for k, v in raw_row.items() if k}
        if any(v.strip() for v in row.values()):
            row[KEY] = _row_key(row)
            rows.append(row)
    return delimiter, header, rows


def write_table(path: Path, delimiter: str, header: list[str], rows: list[dict]) -> None:
    """UTF-8 with a byte-order mark, so Excel shows umlauts; written to a temp
    file first, so a failed write never leaves half a board."""
    header = list(header) + [c for c in COLUMNS if c not in header]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header, delimiter=delimiter, extrasaction="ignore",
                                lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def _number(value) -> str:
    if value is None or value == "":
        return ""
    return str(int(value)) if float(value).is_integer() else str(value)


def _parse_date(value: str) -> datetime | None:
    value = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value[:19], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _job_row(job: dict, key: str, today: str) -> dict:
    portal = SOURCE_MAP.get(job.get("source", ""), job.get("source", ""))
    flexible = ""
    if job.get("scoring_method") == "claude":
        flexible = "yes" if job.get("requirements_flexible") else "no"
    return {
        "Decision": DECISION_NEW,
        "Relevance Score": _number(job.get("relevance_score", 0)),
        "Job Title": job.get("title", ""),
        "Company": job.get("company", ""),
        "Location": job.get("location", ""),
        "URL": job.get("url", ""),
        "AI Assessment": (job.get("ai_assessment") or "").strip(),
        "Unmet Requirements": "; ".join(job.get("unmet_requirements") or []),
        "Hard Requirements": "; ".join(job.get("hard_requirements") or []),
        "Requirements Flexible": flexible,
        "Required Years": _number(job.get("required_years")),
        "Seniority": job.get("seniority", "") or "",
        "Ad Language": job.get("ad_language", "") or "",
        "Contact Person": job.get("contact_person", "") or "",
        "Portal": portal,
        "Query": job.get("query", "") or "",
        "Matched Keywords": "; ".join(kw for kw in job.get("matched_keywords", [])[:5] if kw),
        "AI Matched Skills": "; ".join(sk for sk in job.get("ai_matched_skills", [])[:3] if sk),
        "Sources": ", ".join(job.get("sources", []) or []),
        "Status": DECISION_NEW,
        "Scoring Method": job.get("scoring_method", "keyword_fallback") or "keyword_fallback",
        "Fetch Method": job.get("fetch_method", "snippet") or "snippet",
        "Duplicate Status": job.get("duplicate_status", "") or "",
        "Stop Reason": "",
        "Added": today,
        "Decided": "",
        KEY: key,
    }


def _ad_markdown(job: dict) -> str:
    """The Notion page body as Markdown. The Job Ad comes last and runs to the
    end of the file, so an ad with its own "##" lines still reads back whole."""
    parts = [f"# {job.get('title', '')} @ {job.get('company', '')}", ""]
    if job.get("url"):
        parts += [job["url"], ""]
    sections = [
        ("AI Assessment", (job.get("ai_assessment") or "").strip()),
        ("Situation Fit", (job.get("situation_fit") or "").strip()),
        ("Matched Skills", "  •  ".join(sk for sk in job.get("ai_matched_skills", [])[:10] if sk)),
        ("Matched Keywords", "  •  ".join(kw for kw in job.get("matched_keywords", []) if kw)),
        ("Requirements", "\n".join(_requirements_lines(job))),
    ]
    description = (job.get("description") or "").strip()
    if description:
        full = job.get("fetch_method") == "full_jd"
        sections.append(("Job Ad" if full else "Job Ad (search snippet only)", description))
    for heading, text in sections:
        if text:
            parts += [f"## {heading}", "", text, ""]
    return "\n".join(parts)


def ad_sections(text: str) -> dict[str, str]:
    """The ad file as {heading: text}, one entry per "## " section."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading and not (current or "").startswith("Job Ad"):
            current = heading.group(1)
            sections[current] = []
        elif current:
            sections[current].append(line)
    return {name: "\n".join(lines).strip() for name, lines in sections.items()}


def _letter_markdown(page: LetterPage) -> str:
    note = "\n>\n".join(f"> {line}" for line in page.callout)
    closing = "\n\n".join(page.closing.splitlines())
    return "\n\n".join([f"# {page.title}", note, "---", page.date_line, f"**{page.subject}**",
                        page.salutation, *page.paragraphs, closing]) + "\n"


# ---------------------------------------------------------------- the board


class CsvBoard:
    label = "jobs.csv"

    def __init__(self, root: Path | str | None = None):
        self.root = Path(root or os.getenv("BOARD_DIR") or BOARD_DIR)
        self.jobs_file = self.root / "jobs.csv"
        self.ads_dir = self.root / "ads"
        self.letters_dir = self.root / "letters"

    def _stamp(self, rows: list[dict], today: str) -> None:
        for row in rows:
            decision = canonical_decision(row.get("Decision", ""))
            if decision and decision != DECISION_NEW and not row.get("Decided", "").strip():
                row["Decided"] = today

    def _add(self, jobs: list, build, match_keys: bool) -> tuple[list, list, int]:
        """Append the jobs the board doesn't have. Returns (added, failed, skipped)."""
        delimiter, header, rows = read_table(self.jobs_file)
        urls = {r.get("URL", "") for r in rows if r.get("URL")}
        keys = {r[KEY] for r in rows}
        today = date.today().isoformat()
        added, skipped = [], 0
        for job in jobs:
            key = job_key(job.get("title", ""), job.get("company", ""), job.get("url", ""))
            if (job.get("url") and job["url"] in urls) or (match_keys and key in keys):
                print(f"[INFO] Duplicate skipped: {job.get('title', '')} @ {job.get('company', '')}")
                skipped += 1
                continue
            base, n = key, 2
            while key in keys:  # an Auto-Dropped repost under a new URL: same title, own row
                key, n = f"{base}-{n}", n + 1
            rows.append(build(job, key, today))
            urls.add(job.get("url", ""))
            keys.add(key)
            added.append((job, key))
        self._stamp(rows, today)
        try:
            write_table(self.jobs_file, delimiter, header, rows)
        except OSError as e:
            # Excel on Windows locks a file it has open.
            print(f"[ERROR] Could not write {self.jobs_file}: {e}")
            return [], [job for job, _ in added], skipped
        return added, [], skipped

    def sync_jobs(self, jobs: list) -> dict:
        added, failed, skipped = self._add(jobs, _job_row, match_keys=True)
        for job, key in added:
            try:
                self.ads_dir.mkdir(parents=True, exist_ok=True)
                (self.ads_dir / f"{key}.md").write_text(_ad_markdown(job), encoding="utf-8")
            except OSError as e:
                print(f"[WARN] Could not write the ad for '{job.get('title', '')}': {e}")
            print(f"[OK] Board row added: {job.get('title', '')} @ {job.get('company', '')}")
        print(f"[INFO] Sync done: {len(added)} new, {skipped} skipped, {len(failed)} failed")
        return {"added": len(added), "skipped": skipped, "failed": len(failed), "failed_jobs": failed}

    def sync_auto_dropped(self, jobs: list) -> dict:
        """Auto-Dropped rows, once per URL, with the Stop Reason; the sidecar
        lets a rescue (Decision → Interesting) find the dropped word."""
        if not jobs:
            return {"added": 0, "skipped": 0}
        _write_sidecar(jobs)

        def build(job: dict, key: str, today: str) -> dict:
            row = {c: "" for c in COLUMNS}
            row.update({
                "Decision": DECISION_AUTO_DROPPED, "Status": DECISION_AUTO_DROPPED,
                "Relevance Score": _number(job.get("relevance_score", 0)),
                "Job Title": job.get("title", ""), "Company": job.get("company", ""),
                "Location": job.get("location", ""), "URL": job.get("url", ""),
                "Portal": SOURCE_MAP.get(job.get("source", ""), job.get("source", "")),
                "Stop Reason": job.get("stop_reason", "") or "", "Added": today, KEY: key,
            })
            return row

        added, _, skipped = self._add(jobs, build, match_keys=False)
        return {"added": len(added), "skipped": skipped}

    def fetch_decisions(self, lookback_days: int = LOOKBACK_DAYS_DEFAULT) -> DecisionHistory:
        history = DecisionHistory(source="csv")
        try:
            _, _, rows = read_table(self.jobs_file)
        except (OSError, csv.Error) as e:
            print(f"[WARN] Could not read {self.jobs_file}: {e}")
            return history
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=lookback_days)
        for row in rows:
            decision = canonical_decision(row.get("Decision", ""))
            if not decision or decision == DECISION_NEW:
                continue
            # No date yet: you typed it since the last run, so it counts as today.
            decided_at = _parse_date(row.get("Decided", "")) or now
            if decided_at < cutoff:
                continue
            _bucket(history, Decision(
                title=row.get("Job Title", ""), company=row.get("Company", ""), decision=decision,
                decided_at=decided_at, pipeline=PIPELINE, url=row.get("URL", ""),
                stop_reason=row.get("Stop Reason", ""), query=row.get("Query", ""),
            ))
        return history

    def letter_file(self, key: str) -> Path:
        """Named after the row's Key, which starts with the company: one letter
        per row, like the Notion child page, even for two roles at one company."""
        return self.letters_dir / f"{key}.md"

    def _row_facts(self, row: dict) -> dict:
        try:
            years = float(row.get("Required Years", "").replace(",", ".")) if row.get("Required Years") else None
        except ValueError:
            years = None
        return {
            "row_id": row[KEY],
            "title": row.get("Job Title", ""),
            "company": row.get("Company", "").strip(),
            "location": row.get("Location", ""),
            "url": row.get("URL", ""),
            "decision": canonical_decision(row.get("Decision", "")),
            "score": row.get("Relevance Score", ""),
            "ad_language": row.get("Ad Language", "").strip() or "German",
            "seniority": row.get("Seniority", ""),
            "required_years": years,
            "contact_person": row.get("Contact Person", ""),
            "hard_requirements": row.get("Hard Requirements", ""),
            "requirements_flexible": row.get("Requirements Flexible", "").strip().lower() in _TRUE,
            "unmet_requirements": row.get("Unmet Requirements", ""),
        }

    def letter_rows(self, row_ids: list[str] = ()) -> tuple[int, list[dict], list[tuple[dict, str]]]:
        _, _, rows = read_table(self.jobs_file)
        if row_ids:
            by_key = {r[KEY]: r for r in rows}
            for missing in [k for k in row_ids if k not in by_key]:
                print(f"[WARN] No row with Key {missing} in {self.jobs_file}")
            picked = [by_key[k] for k in row_ids if k in by_key]
        else:
            picked = [r for r in rows if canonical_decision(r.get("Decision", "")) == APPLY]
        todo, skipped = [], []
        for raw in picked:
            row = self._row_facts(raw)
            if not row["company"]:
                skipped.append((row, "no company on the row"))
                continue
            letter = self.letter_file(row["row_id"])
            if letter.exists():
                skipped.append((row, f"already has a letter ({letter.relative_to(self.root.parent)})"))
                continue
            ad_file = self.ads_dir / f"{row['row_id']}.md"
            sections = ad_sections(ad_file.read_text(encoding="utf-8")) if ad_file.exists() else {}
            if not sections.get("Job Ad"):
                reason = ("only a search snippet in its ad file" if "Job Ad (search snippet only)" in sections
                          else "no Job Ad section in its ad file")
                skipped.append((row, f"{reason}; paste the full ad under a '## Job Ad' heading at the end "
                                     f"of {ad_file.relative_to(self.root.parent)} to get a letter"))
                continue
            row["job_ad"] = sections["Job Ad"][:MAX_AD_CHARS]
            row["requirements_block"] = sections.get("Requirements", "")
            todo.append(row)
        return len(picked), todo, skipped

    def add_letter(self, row: dict, page: LetterPage) -> str:
        path = self.letter_file(row["row_id"])
        if path.exists():
            raise ValueError(f"{path.name} appeared during this run; not writing a second")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_letter_markdown(page), encoding="utf-8")
        return str(path)


# ---------------------------------------------------------------- git merge driver


def _merge_cell(base: str, ours: str, theirs: str) -> str:
    if ours == theirs or theirs == base:
        return ours
    if ours == base:
        return theirs
    return ours or theirs


def merge_tables(base: list[dict], ours: list[dict], theirs: list[dict], header: list[str]) -> list[dict]:
    """Three-way merge by Key, cell by cell: a cell one side changed takes that
    side's value, so your Decision and the run's new rows both survive. A row one
    side deleted stays deleted unless the other side changed it."""
    b = {r[KEY]: r for r in base}
    o = {r[KEY]: r for r in ours}
    t = {r[KEY]: r for r in theirs}
    merged = []
    for key in list(o) + [k for k in t if k not in o]:
        if key in o and key in t:
            old = b.get(key, {})
            merged.append({c: _merge_cell(old.get(c, ""), o[key].get(c, ""), t[key].get(c, "")) for c in header})
        else:
            row = o.get(key) or t[key]
            if key in b and row == b[key]:
                continue
            merged.append(row)
    return merged


def merge_files(base: Path, ours: Path, theirs: Path) -> None:
    """git merge driver: `python -m job_board.csv_board merge %O %A %B`. Writes the result to `ours`."""
    _, base_header, base_rows = read_table(base)
    delimiter, ours_header, ours_rows = read_table(ours)
    _, theirs_header, theirs_rows = read_table(theirs)
    header = ours_header + [h for h in theirs_header if h not in ours_header]
    write_table(ours, delimiter, header, merge_tables(base_rows, ours_rows, theirs_rows, header))


if __name__ == "__main__":
    if len(sys.argv) == 5 and sys.argv[1] == "merge":
        merge_files(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
    else:
        sys.exit("usage: python -m job_board.csv_board merge BASE OURS THEIRS")
