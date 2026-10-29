"""The board: where scored jobs land, where your verdicts come from, and where
cover letters go.

Two boards ship, chosen by the BOARD variable:

    BOARD=notion   a Notion database (the default; README → Notion setup)
    BOARD=csv      board/jobs.csv plus Markdown files, no account needed

A new board only needs an entry in BOARDS: a "module:Class" whose class has the
methods of Board. The pipeline calls the module-level functions below, which
send each call to the board BOARD names.
"""
import importlib
import os
from dataclasses import dataclass
from typing import Protocol

from notion_sync.feedback import LOOKBACK_DAYS_DEFAULT, DecisionHistory

BOARDS = {
    "notion": "job_board.notion_board:NotionBoard",
    "csv": "job_board.csv_board:CsvBoard",
}
DEFAULT_BOARD = "notion"

# The letter step: rows with this Decision get a letter, and a Notion child page
# whose title starts with LETTER_TITLE counts as one.
APPLY = "Apply"
LETTER_TITLE = "Anschreiben"
# The longest ads run to ~20k characters; this only guards against a runaway page.
MAX_AD_CHARS = 40000


@dataclass
class LetterPage:
    """A drafted letter, ready for any board: the note to you above it, then the letter."""
    company: str
    callout: list[str]
    date_line: str
    subject: str
    salutation: str
    paragraphs: list[str]
    closing: str

    @property
    def title(self) -> str:
        return f"{LETTER_TITLE} – {self.company}"

    def text(self) -> str:
        """The page as plain text, for the dry run and the saved JSON."""
        return "\n\n".join(["\n".join(self.callout), self.date_line, self.subject,
                            self.salutation, *self.paragraphs, self.closing])


class Board(Protocol):
    label: str  # how the run log names the board

    def sync_jobs(self, jobs: list) -> dict:
        """Add the jobs not on the board yet. Returns added, skipped, failed, failed_jobs."""

    def sync_auto_dropped(self, jobs: list) -> dict:
        """Add the stop-list's drops as Auto-Dropped rows. Returns added, skipped."""

    def fetch_decisions(self, lookback_days: int = LOOKBACK_DAYS_DEFAULT) -> DecisionHistory:
        """Your verdicts of the last `lookback_days`, bucketed for the learning loop."""

    def letter_rows(self, row_ids: list[str] = ()) -> tuple[int, list[dict], list[tuple[dict, str]]]:
        """(rows read, rows to draft a letter for, (row, reason) skipped). Without
        row_ids: every Apply row that has no letter yet."""

    def add_letter(self, row: dict, page: LetterPage) -> str:
        """Put the letter on the board; returns where it went."""


def board_name() -> str:
    return os.getenv("BOARD", "").strip().lower() or DEFAULT_BOARD


def get_board(name: str | None = None) -> Board:
    name = name or board_name()
    spec = BOARDS.get(name)
    if not spec:
        raise ValueError(f"BOARD={name!r} is not a board; use one of: {', '.join(BOARDS)}")
    module, cls = spec.split(":")
    return getattr(importlib.import_module(module), cls)()


def board_label() -> str:
    return get_board().label


def sync_jobs(jobs: list) -> dict:
    return get_board().sync_jobs(jobs)


def sync_auto_dropped(jobs: list) -> dict:
    return get_board().sync_auto_dropped(jobs)


def fetch_decisions_from_env(lookback_days: int = LOOKBACK_DAYS_DEFAULT) -> DecisionHistory:
    return get_board().fetch_decisions(lookback_days)
