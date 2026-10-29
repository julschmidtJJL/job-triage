import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import draft_letters
from draft_letters import LetterDraft, enforce_rules, letter_blocks, letter_frame, letter_text, never_write, select_rows


def _page(page_id: str, company: str = "Acme", language: str = "German", contact: str = "",
          unmet: str = "") -> dict:
    def rich(text: str) -> dict:
        return {"rich_text": [{"plain_text": text}] if text else []}
    return {
        "id": page_id,
        "properties": {
            "Job Title": {"title": [{"plain_text": "Disponent (m/w/d)"}]},
            "Company": rich(company),
            "Location": rich("Bremen"),
            "URL": {"url": f"https://example.com/{page_id}"},
            "Decision": {"select": {"name": "Apply"}},
            "Relevance Score": {"number": 70},
            "Ad Language": {"select": {"name": language}},
            "Contact Person": rich(contact),
            "Hard Requirements": rich("Ausbildung Logistik; 2+ Jahre Disposition"),
            "Unmet Requirements": rich(unmet),
            "Requirements Flexible": {"checkbox": False},
        },
    }


def _heading(text: str) -> dict:
    return {"type": "heading_3", "heading_3": {"rich_text": [{"plain_text": text}]}}


def _para(text: str) -> dict:
    return {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": text}]}}


def _child_page(title: str) -> dict:
    return {"type": "child_page", "child_page": {"title": title}}


AD_BODY = [_heading("AI Assessment"), _para("Fits."), {"type": "divider", "divider": {}},
           _heading("Requirements"), _para("Required: Ausbildung Logistik"), {"type": "divider", "divider": {}},
           _heading("Job Ad"), _para("Wir suchen eine Person für die Disposition."), _para("Ihre Aufgaben: Planung.")]


def _notion(bodies: dict) -> MagicMock:
    notion = MagicMock()
    notion.blocks.children.list.side_effect = lambda page_id, **kw: {"results": bodies[page_id], "has_more": False}
    return notion


def test_apply_row_without_letter_is_drafted():
    todo, skipped = select_rows(_notion({"a": AD_BODY}), [_page("a")])
    assert [r["page_id"] for r in todo] == ["a"]
    assert skipped == []
    assert todo[0]["job_ad"] == "Wir suchen eine Person für die Disposition.\n\nIhre Aufgaben: Planung."
    assert todo[0]["requirements_block"] == "Required: Ausbildung Logistik"


def test_row_with_a_letter_is_skipped():
    bodies = {"a": AD_BODY + [_child_page("Anschreiben – Acme")]}
    todo, skipped = select_rows(_notion(bodies), [_page("a")])
    assert todo == []
    assert skipped[0][1] == "already has an Anschreiben page"


def test_a_hand_made_letter_page_counts_too():
    bodies = {"a": AD_BODY + [_child_page("Anschreiben (meine Version)")], "b": AD_BODY + [_child_page("Notizen")]}
    todo, skipped = select_rows(_notion(bodies), [_page("a"), _page("b")])
    assert [r["page_id"] for r in todo] == ["b"]
    assert [r["page_id"] for r, _ in skipped] == ["a"]


def test_row_with_only_a_snippet_is_skipped():
    bodies = {"a": [_heading("Job Ad (search snippet only)"), _para("Kurz.")], "b": [_heading("AI Assessment"), _para("x")]}
    todo, skipped = select_rows(_notion(bodies), [_page("a"), _page("b")])
    assert todo == []
    assert skipped[0][1].startswith("only a search snippet")
    assert skipped[1][1].startswith("no Job Ad block")


def test_only_apply_rows_are_queried():
    notion = MagicMock()
    notion.data_sources.query.return_value = {"results": [], "has_more": False}
    draft_letters.apply_rows(notion, "ds")
    assert notion.data_sources.query.call_args.kwargs["filter"] == {"property": "Decision", "select": {"equals": "Apply"}}


def _draft(**kw) -> LetterDraft:
    base = dict(salutation="Guten Tag Erika Muster,",
                paragraphs=["[Warum Acme?] Als Analystin bringe ich Planungserfahrung mit.",
                            "Meine Ausbildung zur Speditionskauffrau.", "Ich stehe ab [Datum] zur Verfügung."],
                odds="Medium.", covered=["Planung"], not_covered=["Dispositions-Jahre"], checks=[])
    base.update(kw)
    return LetterDraft(**base)


def test_letter_opens_with_the_literal_placeholder():
    row = draft_letters.row_facts(_page("a"))
    draft = enforce_rules(_draft(), row)
    assert draft.paragraphs[0] == "[Warum Acme, in Ihren eigenen Worten.] Als Analystin bringe ich Planungserfahrung mit."
    assert draft.checks == []


def test_a_missing_start_date_is_added_and_a_money_amount_flagged():
    row = draft_letters.row_facts(_page("a"))
    draft = enforce_rules(_draft(paragraphs=["Zwei Runden über 15.000 € finanziert.", "Gern im Gespräch."]), row)
    assert draft.paragraphs[0].startswith("[Warum Acme, in Ihren eigenen Worten.] Zwei Runden")
    assert draft.paragraphs[-1] == "Ich stehe ab [Datum] zur Verfügung. Gern im Gespräch."
    assert any("[Datum]" in c for c in draft.checks)
    assert any("amount of money" in c for c in draft.checks)


@pytest.mark.parametrize("amount", ["15.000 €", "€15k", "EUR 15000", "$15,000", "15000 USD", "15k"])
def test_money_is_flagged_either_side_of_the_number(amount):
    row = draft_letters.row_facts(_page("a"))
    draft = enforce_rules(_draft(paragraphs=[f"Runde über {amount}.", "Ab [Datum]."]), row)
    assert any("amount of money" in c for c in draft.checks)


def test_plain_numbers_are_not_money():
    row = draft_letters.row_facts(_page("a"))
    draft = enforce_rules(_draft(paragraphs=["Wir planen 40 Touren mit 12 Fahrzeugen.", "Ab [Datum]."]), row)
    assert draft.checks == []


FACTS = """# Cover-letter facts

## Letter frame
- Name: Erika Beispiel
- City: Bremen

## Facts
- Operations analyst, 2023 to 2026. Never `Senior` there.

## Never write
- Titles I never held: `Head of`, `Teamleiterin`.
- My motive: "[Warum <Company>, in Ihren eigenen Worten.]" stays mine.
"""


def test_the_frame_and_banned_phrases_come_from_the_facts():
    assert letter_frame(FACTS) == {"name": "Erika Beispiel", "city": "Bremen"}
    assert never_write(FACTS) == ["Head of", "Teamleiterin"]
    assert letter_frame("## Facts\n- Name: not the frame") == {}
    assert never_write("") == []


def test_a_banned_phrase_is_flagged_whatever_its_case():
    row = draft_letters.row_facts(_page("a"))
    draft = enforce_rules(_draft(paragraphs=["Als teamleiterin plante ich.", "Ab [Datum]."]), row,
                          never_write(FACTS))
    assert draft.checks == ['The letter says "Teamleiterin", which the letter facts list under Never write.']


def test_an_empty_letter_is_an_error():
    row = draft_letters.row_facts(_page("a"))
    with pytest.raises(ValueError):
        enforce_rules(_draft(paragraphs=["  "]), row)


def test_write_rechecks_the_row_and_never_adds_a_second_letter():
    row = draft_letters.row_facts(_page("a"))
    notion = _notion({"a": AD_BODY + [_child_page("Anschreiben – Acme")]})
    with pytest.raises(ValueError, match="second"):
        draft_letters.write_letter(notion, row, [])
    notion.pages.create.assert_not_called()


def test_write_refuses_more_than_100_blocks():
    row = draft_letters.row_facts(_page("a"))
    notion = _notion({"a": AD_BODY})
    with pytest.raises(ValueError, match="100"):
        draft_letters.write_letter(notion, row, [{}] * 101)
    notion.pages.create.assert_not_called()


def test_write_creates_the_page_under_the_row():
    row = draft_letters.row_facts(_page("a"))
    notion = _notion({"a": AD_BODY})
    notion.pages.create.return_value = {"url": "https://notion.so/x"}
    assert draft_letters.write_letter(notion, row, [{"type": "divider"}]) == "https://notion.so/x"
    kwargs = notion.pages.create.call_args.kwargs
    assert kwargs["parent"] == {"page_id": "a"}
    assert kwargs["properties"]["title"]["title"][0]["text"]["content"] == "Anschreiben – Acme"


def test_page_has_callout_with_odds_and_website_note_then_the_letter():
    row = draft_letters.row_facts(_page("a", contact="Erika Muster, Recruiting"))
    blocks = letter_blocks(enforce_rules(_draft(), row), row, date(2026, 10, 29), letter_frame(FACTS))
    assert blocks[0]["type"] == "callout"
    callout = "".join(t["text"]["content"] for t in blocks[0]["callout"]["rich_text"])
    assert "Odds: Medium." in callout
    assert "Covers: Planung" in callout and "Doesn't cover: Dispositions-Jahre" in callout
    assert "Contact: Erika Muster, Recruiting" in callout
    assert callout.endswith("Company website not checked.")
    text = letter_text(blocks)
    assert "Bremen, 29. Oktober 2026" in text
    assert "Bewerbung als Disponent (m/w/d)" in text
    assert text.endswith("Mit freundlichen Grüßen\nErika Beispiel")


def test_without_a_frame_the_name_is_left_to_fill_in():
    row = draft_letters.row_facts(_page("a"))
    text = letter_text(letter_blocks(enforce_rules(_draft(), row), row, date(2026, 10, 29)))
    assert "\n\n29. Oktober 2026\n\n" in text
    assert text.endswith("Mit freundlichen Grüßen\n[Name]")


def test_a_long_callout_is_not_cut():
    row = draft_letters.row_facts(_page("a"))
    draft = enforce_rules(_draft(checks=["x" * 1500, "y" * 1500]), row)
    block = letter_blocks(draft, row, date(2026, 10, 29))[0]
    assert all(len(t["text"]["content"]) <= 2000 for t in block["callout"]["rich_text"])
    assert "".join(t["text"]["content"] for t in block["callout"]["rich_text"]).endswith("Company website not checked.")


def test_english_ad_gets_an_english_frame_and_placeholders():
    row = draft_letters.row_facts(_page("a", language="English"))
    draft = enforce_rules(_draft(paragraphs=["I bring numbers.", "Happy to talk."]), row)
    assert draft.paragraphs[0] == "[Why Acme, in your own words.] I bring numbers."
    assert draft.paragraphs[-1] == "I can start on [Start date]. Happy to talk."
    text = letter_text(letter_blocks(draft, row, date(2026, 10, 29), letter_frame(FACTS)))
    assert "Bremen, 29 October 2026" in text
    assert "Application for Disponent" in text
    assert "Still yours: [Why Acme, in your own words.] and [Start date]." in text
    assert text.endswith("Yours sincerely,\nErika Beispiel")


def test_dry_run_writes_nothing(monkeypatch, tmp_path):
    notion = MagicMock()
    notion.data_sources.query.return_value = {"results": [_page("a")], "has_more": False}
    notion.blocks.children.list.return_value = {"results": AD_BODY, "has_more": False}
    monkeypatch.setattr(draft_letters, "Client", lambda auth: notion)
    monkeypatch.setattr(draft_letters, "DATA_DIR", tmp_path)
    monkeypatch.setattr(draft_letters, "load_profile_text", lambda: "profile")
    monkeypatch.setattr(draft_letters, "load_letter_facts", lambda: "facts")
    monkeypatch.setattr(draft_letters, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(draft_letters.anthropic, "Anthropic", lambda api_key: MagicMock())
    monkeypatch.setattr(draft_letters, "draft_letter", lambda client, row, p, f: enforce_rules(_draft(), row))
    monkeypatch.setenv("NOTION_API_KEY", "x")
    monkeypatch.setenv("NOTION_DATA_SOURCE_ID", "ds")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert draft_letters.main(["--dry-run"]) == 0
    notion.pages.create.assert_not_called()
    assert (tmp_path / f"letters_{date.today()}.json").exists()


def test_no_letter_without_the_facts(monkeypatch, tmp_path):
    notion = MagicMock()
    notion.data_sources.query.return_value = {"results": [_page("a")], "has_more": False}
    notion.blocks.children.list.return_value = {"results": AD_BODY, "has_more": False}
    monkeypatch.setattr(draft_letters, "Client", lambda auth: notion)
    monkeypatch.setattr(draft_letters, "load_profile_text", lambda: "profile")
    monkeypatch.setattr(draft_letters, "load_letter_facts", lambda: "")
    monkeypatch.setattr(draft_letters, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("NOTION_API_KEY", "x")
    monkeypatch.setenv("NOTION_DATA_SOURCE_ID", "ds")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert draft_letters.main([]) == 1
    notion.pages.create.assert_not_called()
