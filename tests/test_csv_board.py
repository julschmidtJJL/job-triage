import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import job_board
import notion_sync.sync as notion_sync
from job_board import LetterPage, get_board
from job_board.csv_board import (
    CsvBoard, ad_sections, canonical_decision, job_key, merge_files, read_table, write_table,
)


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setattr(notion_sync, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(notion_sync, "SEEN_URLS_FILE", tmp_path / "data" / "jobs_seen_urls.json")
    return CsvBoard(tmp_path / "board")


def _job(title="Disponent (m/w/d)", company="Nordhafen Logistik GmbH", url="https://example.com/1", **extra):
    job = {"title": title, "company": company, "url": url, "location": "Bremen", "source": "arbeitsagentur",
           "relevance_score": 72, "scoring_method": "claude", "fetch_method": "full_jd",
           "ai_assessment": "Plans tours; matches the dispatch years.",
           "hard_requirements": ["Ausbildung Logistik", "2+ Jahre Disposition"],
           "unmet_requirements": [], "requirements_flexible": False, "ad_language": "German",
           "contact_person": "Frau Erika Muster", "required_years": 2.0, "query": "Disponent",
           "matched_keywords": ["disposition"], "ai_matched_skills": ["Tourenplanung"],
           "description": "Wir suchen eine Person für die Disposition.\n\n## Ihre Aufgaben\n\nPlanung."}
    job.update(extra)
    return job


def _set_decision(board: CsvBoard, key: str, decision: str, decided: str = "") -> None:
    delimiter, header, rows = read_table(board.jobs_file)
    for row in rows:
        if row["Key"] == key:
            row["Decision"] = decision
            row["Decided"] = decided
    write_table(board.jobs_file, delimiter, header, rows)


def _letter(company="Nordhafen Logistik GmbH") -> LetterPage:
    return LetterPage(company=company, callout=["Odds: fair.", "Company website not checked."],
                      date_line="Bremen, 29. Oktober 2026", subject="Bewerbung als Disponent",
                      salutation="Guten Tag Erika Muster,", paragraphs=["[Warum Nordhafen?] Erster Absatz."],
                      closing="Mit freundlichen Grüßen\nErika Beispiel")


# ---------------------------------------------------------------- the registry


def test_notion_is_the_default_board(monkeypatch):
    monkeypatch.delenv("BOARD", raising=False)
    assert job_board.board_name() == "notion"
    assert get_board().label == "Notion"


def test_board_csv_picks_the_file_board(monkeypatch, tmp_path):
    monkeypatch.setenv("BOARD", "CSV ")
    monkeypatch.setenv("BOARD_DIR", str(tmp_path / "b"))
    board = get_board()
    assert isinstance(board, CsvBoard)
    assert board.jobs_file == tmp_path / "b" / "jobs.csv"


def test_an_unknown_board_is_an_error(monkeypatch):
    monkeypatch.setenv("BOARD", "airtable")
    with pytest.raises(ValueError, match="notion, csv"):
        get_board()


# ---------------------------------------------------------------- jobs in, decisions out


def test_round_trip(board):
    result = board.sync_jobs([_job(), _job(title="Lagerleiter", url="https://example.com/2", relevance_score=55)])
    assert result == {"added": 2, "skipped": 0, "failed": 0, "failed_jobs": []}

    _, header, rows = read_table(board.jobs_file)
    assert header[:3] == ["Decision", "Relevance Score", "Job Title"]
    first = rows[0]
    assert first["Decision"] == "New"
    assert first["Job Title"] == "Disponent (m/w/d)"
    assert first["Relevance Score"] == "72"
    assert first["Hard Requirements"] == "Ausbildung Logistik; 2+ Jahre Disposition"
    assert first["Required Years"] == "2"
    assert first["Requirements Flexible"] == "no"
    assert first["Key"] == job_key("Disponent (m/w/d)", "Nordhafen Logistik GmbH")
    assert first["Key"].startswith("nordhafen-logistik-gmbh-disponent-")

    ad = (board.ads_dir / f"{first['Key']}.md").read_text(encoding="utf-8")
    sections = ad_sections(ad)
    assert sections["AI Assessment"] == "Plans tours; matches the dispatch years."
    assert sections["Requirements"].startswith("Required: Ausbildung Logistik")
    # The ad's own "##" heading does not cut the ad short.
    assert sections["Job Ad"] == "Wir suchen eine Person für die Disposition.\n\n## Ihre Aufgaben\n\nPlanung."

    _set_decision(board, first["Key"], "apply")
    _set_decision(board, rows[1]["Key"], "Not interesting")
    history = board.fetch_decisions()
    assert history.source == "csv"
    assert [(d.title, d.decision, d.query) for d in history.applied] == [("Disponent (m/w/d)", "Apply", "Disponent")]
    assert [d.title for d in history.rejected] == ["Lagerleiter"]


def test_a_decision_typed_by_the_user_survives_the_next_sync(board):
    board.sync_jobs([_job()])
    key = read_table(board.jobs_file)[2][0]["Key"]
    _set_decision(board, key, "Interesting")
    # A user column and a reordered header survive too.
    delimiter, header, rows = read_table(board.jobs_file)
    rows[0]["My Notes"] = "Ask about the night shifts"
    write_table(board.jobs_file, delimiter, ["My Notes"] + header, rows)

    result = board.sync_jobs([_job(), _job(title="Lagerleiter", url="https://example.com/2")])
    assert result["added"] == 1 and result["skipped"] == 1

    _, header, rows = read_table(board.jobs_file)
    assert header[0] == "My Notes"
    assert [r["Decision"] for r in rows] == ["Interesting", "New"]
    assert rows[0]["My Notes"] == "Ask about the night shifts"
    assert rows[0]["Decided"] == date.today().isoformat()


def test_a_repost_under_a_new_url_is_a_duplicate(board):
    board.sync_jobs([_job()])
    result = board.sync_jobs([_job(title="Disponent (w/m/d)", url="https://example.com/repost")])
    assert result["skipped"] == 1
    assert len(read_table(board.jobs_file)[2]) == 1


def test_a_board_saved_by_german_excel_is_read_and_kept(board):
    board.sync_jobs([_job()])
    _, _, rows = read_table(board.jobs_file)
    rows[0]["Decision"] = "Apply"
    # Excel in a German locale: ";" and Windows-1252.
    write_table(board.jobs_file, ";", list(rows[0]), rows)
    board.jobs_file.write_bytes(board.jobs_file.read_text(encoding="utf-8-sig").encode("cp1252"))

    board.sync_jobs([_job(title="Lagerleiter", url="https://example.com/2")])
    raw = board.jobs_file.read_text(encoding="utf-8-sig")
    assert raw.splitlines()[0].count(";") > 5
    _, _, rows = read_table(board.jobs_file)
    assert [r["Decision"] for r in rows] == ["Apply", "New"]
    assert rows[0]["Company"] == "Nordhafen Logistik GmbH"


def test_old_decisions_fall_out_of_the_lookback(board):
    board.sync_jobs([_job(), _job(title="Lagerleiter", url="https://example.com/2")])
    rows = read_table(board.jobs_file)[2]
    old = (datetime.now(timezone.utc) - timedelta(days=120)).date().isoformat()
    _set_decision(board, rows[0]["Key"], "Interesting", decided=old)
    _set_decision(board, rows[1]["Key"], "Interesting", decided="29.10.2026")
    history = board.fetch_decisions(lookback_days=90)
    assert [d.title for d in history.interested] == ["Lagerleiter"]


def test_no_board_yet_means_no_decisions(board):
    assert board.fetch_decisions().total() == 0


def test_a_locked_file_fails_the_jobs_so_they_are_retried(board, monkeypatch):
    def locked(*args, **kwargs):
        raise PermissionError("the file is open in Excel")
    monkeypatch.setattr("job_board.csv_board.write_table", locked)
    result = board.sync_jobs([_job()])
    assert result["added"] == 0 and result["failed"] == 1
    assert result["failed_jobs"][0]["title"] == "Disponent (m/w/d)"


def test_auto_dropped_rows_and_their_rescue(board):
    dropped = _job(title="Fahrer", url="https://example.com/3", stop_reason="title word 'fahrer' rejected 3x",
                   stop_token="fahrer", stop_pipeline="main")
    assert board.sync_auto_dropped([dropped]) == {"added": 1, "skipped": 0}
    assert board.sync_auto_dropped([dropped]) == {"added": 0, "skipped": 1}
    row = read_table(board.jobs_file)[2][0]
    assert (row["Decision"], row["Stop Reason"]) == ("Auto-Dropped", "title word 'fahrer' rejected 3x")
    assert board.fetch_decisions().total() == 0

    _set_decision(board, row["Key"], "Interesting")
    history = board.fetch_decisions()
    assert [d.url for d in history.auto_dropped_rescued] == ["https://example.com/3"]


def test_decisions_are_matched_whatever_their_case():
    assert canonical_decision(" not INTERESTING ") == "Not interesting"
    assert canonical_decision("Vielleicht") == "Vielleicht"


# ---------------------------------------------------------------- letters


def test_apply_rows_without_a_letter_are_drafted(board):
    board.sync_jobs([_job(), _job(title="Lagerleiter", url="https://example.com/2")])
    rows = read_table(board.jobs_file)[2]
    _set_decision(board, rows[0]["Key"], "Apply")
    read, todo, skipped = board.letter_rows()
    assert read == 1 and skipped == []
    row = todo[0]
    assert row["row_id"] == rows[0]["Key"]
    assert row["contact_person"] == "Frau Erika Muster"
    assert row["required_years"] == 2.0
    assert row["job_ad"].startswith("Wir suchen eine Person")
    assert row["requirements_block"].startswith("Required: Ausbildung Logistik")


def test_an_existing_letter_file_means_the_row_is_skipped(board):
    board.sync_jobs([_job()])
    key = read_table(board.jobs_file)[2][0]["Key"]
    _set_decision(board, key, "Apply")
    _, todo, _ = board.letter_rows()
    path = board.add_letter(todo[0], _letter())
    assert Path(path) == board.letters_dir / "nordhafen-logistik-gmbh.md"
    text = Path(path).read_text(encoding="utf-8")
    assert text.startswith("# Anschreiben – Nordhafen Logistik GmbH\n\n> Odds: fair.")
    assert text.endswith("Mit freundlichen Grüßen\n\nErika Beispiel\n")

    # Drafted here or written by hand: either way, never a second one.
    _, todo, skipped = board.letter_rows()
    assert todo == []
    assert skipped[0][1].startswith("already has a letter (board/letters/nordhafen-logistik-gmbh.md)")
    with pytest.raises(ValueError, match="not writing a second"):
        board.add_letter(skipped[0][0], _letter())


def test_a_snippet_only_row_gets_no_letter(board):
    board.sync_jobs([_job(fetch_method="snippet")])
    key = read_table(board.jobs_file)[2][0]["Key"]
    _set_decision(board, key, "Apply")
    _, todo, skipped = board.letter_rows()
    assert todo == []
    assert skipped[0][1].startswith("only a search snippet in its ad file")


def test_a_named_row_is_drafted_whatever_its_decision(board):
    board.sync_jobs([_job()])
    key = read_table(board.jobs_file)[2][0]["Key"]
    read, todo, _ = board.letter_rows([key, "no-such-key"])
    assert read == 1 and [r["row_id"] for r in todo] == [key]


def test_the_letter_script_writes_to_the_csv_board(board, monkeypatch, tmp_path):
    sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
    import draft_letters
    from draft_letters import LetterDraft, enforce_rules

    board.sync_jobs([_job()])
    _set_decision(board, read_table(board.jobs_file)[2][0]["Key"], "Apply")
    draft = LetterDraft(salutation="Guten Tag Erika Muster,", paragraphs=["Ich bringe Erfahrung mit.", "Ab [Datum]."],
                        odds="Fair.", covered=["Disposition"], not_covered=[], checks=[])
    monkeypatch.setenv("BOARD", "csv")
    monkeypatch.setenv("BOARD_DIR", str(board.root))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setattr(draft_letters, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(draft_letters, "load_profile_text", lambda: "profile")
    monkeypatch.setattr(draft_letters, "load_letter_facts", lambda: "## Letter frame\n- Name: Erika Beispiel\n")
    monkeypatch.setattr(draft_letters, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(draft_letters.anthropic, "Anthropic", lambda api_key: None)
    monkeypatch.setattr(draft_letters, "draft_letter", lambda client, row, p, f: enforce_rules(draft, row))

    assert draft_letters.main(["--dry-run"]) == 0
    assert not board.letters_dir.exists()
    assert draft_letters.main([]) == 0
    letter = (board.letters_dir / "nordhafen-logistik-gmbh.md").read_text(encoding="utf-8")
    assert "[Warum Nordhafen Logistik GmbH, in Ihren eigenen Worten.] Ich bringe Erfahrung mit." in letter
    assert letter.endswith("Erika Beispiel\n")
    assert draft_letters.main([]) == 0  # second run: the file is there, nothing to draft


# ---------------------------------------------------------------- the git merge driver


def test_merge_keeps_the_users_decision_and_the_runs_new_rows(tmp_path, board):
    board.sync_jobs([_job()])
    base = tmp_path / "base.csv"
    base.write_bytes(board.jobs_file.read_bytes())
    key = read_table(base)[2][0]["Key"]

    # The run adds a row; meanwhile the user pushes a Decision.
    board.sync_jobs([_job(title="Lagerleiter", url="https://example.com/2")])
    ours = tmp_path / "ours.csv"
    ours.write_bytes(board.jobs_file.read_bytes())
    board.jobs_file.write_bytes(base.read_bytes())
    _set_decision(board, key, "Apply")
    theirs = tmp_path / "theirs.csv"
    theirs.write_bytes(board.jobs_file.read_bytes())

    merge_files(base, ours, theirs)
    rows = read_table(ours)[2]
    assert [(r["Job Title"], r["Decision"]) for r in rows] == [("Disponent (m/w/d)", "Apply"), ("Lagerleiter", "New")]


def test_merge_drops_a_row_one_side_deleted_and_the_other_left_alone(tmp_path, board):
    board.sync_jobs([_job(), _job(title="Lagerleiter", url="https://example.com/2")])
    base = tmp_path / "base.csv"
    base.write_bytes(board.jobs_file.read_bytes())
    delimiter, header, rows = read_table(base)
    ours = tmp_path / "ours.csv"
    write_table(ours, delimiter, header, rows[:1])
    theirs = tmp_path / "theirs.csv"
    theirs.write_bytes(base.read_bytes())
    merge_files(base, ours, theirs)
    assert [r["Job Title"] for r in read_table(ours)[2]] == ["Disponent (m/w/d)"]
