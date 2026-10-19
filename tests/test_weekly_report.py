"""Tests for utils/weekly_report.py and utils/mailer.py."""
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from notion_sync.feedback import Decision, DecisionHistory
from utils import mailer
from utils.weekly_report import build_weekly_report

TODAY = date(2026, 10, 19)  # a Monday
QUERIES = [{"q": "AI Engineer", "active": True}, {"q": "<b>KI</b>", "active": False}]
STATS = {"days": {
    "2026-10-15": {"AI Engineer": {"found": 50, "new": 20, "passed": 12, "scored": 10, "score_sum": 500}},
    "2026-09-30": {"AI Engineer": {"found": 40, "new": 10, "passed": 5, "scored": 5, "score_sum": 200}},
}}


def _decision(kind, when):
    return Decision(title="AI Engineer", company="X", decision=kind, pipeline="main",
                    decided_at=when, query="AI Engineer")


def test_report_numbers_and_subject():
    recent = datetime(2026, 10, 16, tzinfo=timezone.utc)
    old = datetime(2026, 9, 1, tzinfo=timezone.utc)
    history = DecisionHistory(applied=[_decision("Applied", recent), _decision("Applied", old),
                                       _decision("Apply", recent), _decision("Apply", recent)],
                              interested=[_decision("Interesting", recent)])
    subject, text, page = build_weekly_report(STATS, history, QUERIES, ["Old"], ["KI Referent"], today=TODAY)
    assert subject == "jobhunt week 43: 12 jobs past the filter, 1 applied"
    assert "Last 7 days: 50 jobs found, 12 past the prefilter." in text
    assert "1 applied, 2 on the apply list, 1 interesting" in text
    assert "Suggested new queries: KI Referent" in text
    assert "consider turning off: Old" in text
    # 28-day table includes the September 30 run
    assert "90" in text


def test_report_escapes_html():
    _, _, page = build_weekly_report(STATS, None, QUERIES, [], [], today=TODAY)
    assert "<b>KI</b>" not in page
    assert "&lt;b&gt;KI&lt;/b&gt;" in page


def test_mailer_refuses_bad_password_shape(monkeypatch):
    monkeypatch.setenv("MAIL_USER", "me@example.com")
    monkeypatch.setenv("MAIL_APP_PASSWORD", "abcd efgh ijkl mnoX")
    with patch("smtplib.SMTP") as smtp:
        sent, info = mailer.send_mail("s", "t", "<p>h</p>")
    assert not sent and "16 lowercase letters" in info
    smtp.assert_not_called()


def test_mailer_without_credentials(monkeypatch):
    monkeypatch.delenv("MAIL_USER", raising=False)
    monkeypatch.delenv("MAIL_APP_PASSWORD", raising=False)
    assert mailer.mail_configured() is False
    assert mailer.send_mail("s", "t", "h") == (False, "MAIL_USER or MAIL_APP_PASSWORD not set")


def test_mailer_sends_and_redacts(monkeypatch):
    monkeypatch.setenv("MAIL_USER", "me@example.com")
    monkeypatch.setenv("MAIL_APP_PASSWORD", "abcd efgh ijkl mnop")  # spaces are fine
    monkeypatch.setenv("MAIL_TO", "you@example.com")
    server = MagicMock()
    with patch("smtplib.SMTP") as smtp:
        smtp.return_value.__enter__.return_value = server
        assert mailer.send_mail("s", "t", "h") == (True, "sent to you@example.com")
        server.login.assert_called_once_with("me@example.com", "abcdefghijklmnop")
        msg = server.send_message.call_args.args[0]
        assert msg["To"] == "you@example.com"

        server.login.side_effect = RuntimeError("bad login for abcdefghijklmnop")
        sent, info = mailer.send_mail("s", "t", "h")
    assert not sent and "abcdefghijklmnop" not in info and "<redacted>" in info
