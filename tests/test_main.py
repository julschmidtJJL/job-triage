"""End-to-end tests for run_pipeline with every scraper, Notion and Claude mocked.
All state files go to tmp_path."""
import json
import sys
from functools import partial
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import main as m
import utils.holdover as holdover
import utils.pipeline as pl
import utils.query_stats as qs
import utils.run_summary as rs
import utils.stoplist as sl

CONFIG = {
    "queries": [{"q": "AI Engineer", "active": True}, {"q": "KI Manager", "active": True},
                {"q": "Old Query", "active": False}],
    "role_terms": ["ai", "ki", "data"],
}

SCRAPERS = ["scrape_google_jobs", "scrape_hamburg_karriere",
            "scrape_good_jobs", "scrape_green_jobs", "scrape_digital_rapids",
            "scrape_arbeitsagentur"]


def _job(title="AI Engineer", company="Company A", url="https://example.com/1",
         location="Hamburg, Deutschland", source="hamburg_karriere", **extra):
    return {"title": title, "company": company, "url": url, "location": location,
            "description": "", "source": source, "date_posted": "", **extra}


@pytest.fixture
def env(tmp_path, monkeypatch):
    for var in ("NOTION_API_KEY", "NOTION_DATA_SOURCE_ID", "NOTION_JOB_BOARD_DB_ID",
                "ANTHROPIC_API_KEY", "FIT_PROFILE", "SERPAPI_KEY",
                "MAIL_USER", "MAIL_APP_PASSWORD", "MAIL_TO", "SEND_REPORT"):
        monkeypatch.delenv(var, raising=False)
    for mod in (pl, m, sl):
        monkeypatch.setattr(mod, "DATA_DIR", tmp_path)
    monkeypatch.setattr(pl, "SEEN_URLS_FILE", tmp_path / "jobs_seen_urls.json")
    monkeypatch.setattr(sl, "SEEN_URLS_FILE", tmp_path / "jobs_seen_urls.json")
    monkeypatch.setattr(m, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(rs, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(qs, "DATA_DIR", tmp_path)
    monkeypatch.setattr(m, "load_stoplist", partial(sl.load_stoplist, tmp_path / "stoplist.json"))
    monkeypatch.setattr(m, "save_stoplist", partial(sl.save_stoplist, path=tmp_path / "stoplist.json"))
    monkeypatch.setattr(m, "load_holdover", partial(holdover.load_holdover, tmp_path / "holdover.json"))
    monkeypatch.setattr(m, "save_holdover", partial(holdover.save_holdover, path=tmp_path / "holdover.json"))
    monkeypatch.setattr(m, "load_stats", partial(qs.load_stats, tmp_path / "query_stats.json"))
    monkeypatch.setattr(m, "save_stats", partial(qs.save_stats, path=tmp_path / "query_stats.json"))
    monkeypatch.setattr(m, "load_search_config", lambda: CONFIG)
    monkeypatch.setattr(m, "load_profile_text", lambda: "")
    monkeypatch.setattr(m, "load_dotenv", lambda *a, **k: None)
    return tmp_path


def _run(scraped: dict | None = None, dry_run=True, sync_result=None):
    """Run the pipeline. `scraped` maps scraper name → return value or exception."""
    scraped = scraped or {}
    patches = []
    for name in SCRAPERS:
        value = scraped.get(name, [])
        if isinstance(value, Exception):
            patches.append(patch(f"main.{name}", side_effect=value))
        else:
            patches.append(patch(f"main.{name}", return_value=value))
    sync = patch("main.sync_jobs", return_value=sync_result or {"added": 0, "skipped": 0})
    for p in patches:
        p.start()
    mock_sync = sync.start()
    try:
        return m.run_pipeline(dry_run=dry_run), mock_sync
    finally:
        sync.stop()
        for p in patches:
            p.stop()


def test_writes_scored_output_json(env):
    result, _ = _run({"scrape_hamburg_karriere": [_job()]})
    files = list(env.glob("new_jobs_*.json"))
    assert len(files) == 1
    written = json.loads(files[0].read_text(encoding="utf-8"))
    assert len(written) == len(result) == 1
    assert "relevance_score" in written[0]


def test_only_active_queries_are_searched(env):
    others = [patch(f"main.{name}", return_value=[]) for name in SCRAPERS if name != "scrape_arbeitsagentur"]
    for p in others:
        p.start()
    try:
        with patch("main.scrape_arbeitsagentur", return_value=[]) as aa:
            m.run_pipeline(dry_run=True)
    finally:
        for p in others:
            p.stop()
    assert aa.call_count == 1
    assert aa.call_args.args[0] == ["AI Engineer", "KI Manager"]


def test_combines_sources(env):
    result, _ = _run({
        "scrape_hamburg_karriere": [_job(url="https://example.com/1", company="A")],
        "scrape_good_jobs": [_job(url="https://example.com/2", company="B", source="good_jobs")],
        "scrape_google_jobs": [_job(url="https://example.com/3", company="C", source="google_jobs")],
    })
    assert len(result) == 3


def test_cross_platform_dedup_merges_identical_jobs(env):
    result, _ = _run({
        "scrape_hamburg_karriere": [_job(url="https://example.com/1", source="hamburg_karriere")],
        "scrape_good_jobs": [_job(url="https://example.com/2", source="good_jobs")],
    })
    assert len(result) == 1


def test_location_keeps_hamburg_drops_munich(env):
    result, _ = _run({"scrape_hamburg_karriere": [
        _job(url="https://example.com/hh", location="Hamburg"),
        _job(url="https://example.com/muc", company="B", location="München"),
    ]})
    assert [j["url"] for j in result] == ["https://example.com/hh"]


def test_remote_jobs_must_name_a_dach_place():
    assert m.keep_location({"location": "Wien, Österreich", "remote": True})
    assert not m.keep_location({"location": "New York, US", "remote": True})
    assert not m.keep_location({"location": "Remote", "remote": True})


def test_jobs_are_tagged_with_their_query(env):
    result, _ = _run({"scrape_hamburg_karriere": [_job(title="KI Manager (m/w/d)")]})
    assert result[0]["query"] == "KI Manager"


def test_query_stats_are_recorded(env):
    _run({"scrape_hamburg_karriere": [_job(title="AI Engineer")]})
    stats = json.loads((env / "query_stats.json").read_text(encoding="utf-8"))
    (day,) = stats["days"].values()
    assert day["AI Engineer"]["found"] == 1
    assert day["AI Engineer"]["new"] == 1
    assert day["AI Engineer"]["passed"] == 1


def test_scraper_exception_does_not_stop_the_run(env, capsys):
    result, _ = _run({
        "scrape_google_jobs": RuntimeError("blocked"),
        "scrape_hamburg_karriere": [_job()],
    })
    assert len(result) == 1
    assert "[ERROR] Google Jobs scraper: blocked" in capsys.readouterr().out


def test_no_active_queries_stops_early(env, monkeypatch, capsys):
    monkeypatch.setattr(m, "load_search_config", lambda: {"queries": [], "role_terms": []})
    result, _ = _run()
    assert result is None
    assert "[ERROR]" in capsys.readouterr().out


def test_dry_run_skips_notion(env, capsys):
    _, mock_sync = _run({"scrape_hamburg_karriere": [_job()]}, dry_run=True)
    mock_sync.assert_not_called()
    assert "[DRY-RUN] Notion upload skipped" in capsys.readouterr().out


def test_normal_run_syncs_to_notion(env, capsys):
    _, mock_sync = _run({"scrape_hamburg_karriere": [_job()]}, dry_run=False,
                        sync_result={"added": 3, "skipped": 1})
    mock_sync.assert_called_once()
    out = capsys.readouterr().out
    assert "[SYNC] Notion: 3 added, 1 skipped" in out
    assert "[QUERIES]" in out


def test_run_summary_is_written(env):
    _run({"scrape_hamburg_karriere": [_job()]})
    (summary_file,) = (env / "logs").glob("feedback_run_*.jsonl")
    summary = json.loads(summary_file.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert summary["jobs_in"] == 1
    assert any(row["query"] == "AI Engineer" for row in summary["query_scoreboard"])


# ---------------------------------------------------------------- failure handling

def test_run_failures_flags_claude_scoring_nothing():
    jobs = [{"prefilter_score": 40, "scoring_method": "keyword_fallback"}]
    assert m.run_failures(jobs, "PROFILE", 20, None) == [
        "Claude scored none of the 1 jobs that passed the prefilter"]
    # Without a profile, keyword-only scoring is the intended fallback
    assert m.run_failures(jobs, "", 20, None) == []
    # One success is enough: partial batch failures are warnings, not a failed run
    jobs.append({"prefilter_score": 40, "scoring_method": "claude"})
    assert m.run_failures(jobs, "PROFILE", 20, None) == []


def test_run_failures_flags_notion_rejecting_everything():
    assert m.run_failures([], "", 20, {"added": 0, "skipped": 0, "failed": 30}) == [
        "Notion rejected all 30 uploads"]
    assert m.run_failures([], "", 20, {"added": 29, "skipped": 0, "failed": 1}) == []
    assert m.run_failures([], "", 20, {"added": 0, "skipped": 5, "failed": 0}) == []


def test_failed_notion_sync_fails_the_run(env, capsys):
    with pytest.raises(m.PipelineFailed):
        _run({"scrape_hamburg_karriere": [_job()]}, dry_run=False,
             sync_result={"added": 0, "skipped": 0, "failed": 1})
    assert "[FAILED] Notion rejected all 1 uploads" in capsys.readouterr().out


def test_dry_run_never_fails_on_missing_delivery(env):
    result, _ = _run({"scrape_hamburg_karriere": [_job()]}, dry_run=True)
    assert len(result) == 1


# ---------------------------------------------------------------- holdover

def test_holdover_jobs_come_back_next_run(env, monkeypatch):
    """Jobs that miss the top-N cut must be re-scored in the next run, not
    discarded as already seen (a bug in the original version of this pipeline)."""
    monkeypatch.setenv("JOB_HUNT_TOP_N", "1")
    jobs = [_job(title="AI Engineer", url="https://example.com/1", company="A"),
            _job(title="AI Data Engineer", url="https://example.com/2", company="B")]
    first, _ = _run({"scrape_hamburg_karriere": jobs})
    held_url = first[1]["url"]  # sorted by score; index 1 missed the top-1 cut
    result, _ = _run()  # second run, nothing new scraped
    assert [j["url"] for j in result] == [held_url]


def test_failed_uploads_are_retried_next_run(env):
    job = _job(title="AI Engineer", url="https://example.com/1")
    with patch("main.sync_jobs", return_value={"added": 1, "skipped": 0, "failed": 1, "failed_jobs": [job]}):
        others = [patch(f"main.{n}", return_value=([job] if n == "scrape_hamburg_karriere" else [])) for n in SCRAPERS]
        for p in others:
            p.start()
        try:
            m.run_pipeline(dry_run=False)
        finally:
            for p in others:
                p.stop()
    result, _ = _run()  # nothing new scraped; the failed upload must come back
    assert [j["url"] for j in result] == ["https://example.com/1"]



# ---------------------------------------------------------------- minimum score

def test_jobs_below_the_minimum_score_never_reach_notion(env, monkeypatch):
    monkeypatch.setenv("MIN_NOTION_SCORE", "40")
    jobs = [_job(title="AI Engineer", url="https://example.com/ai", company="A"),       # 40
            _job(title="Hausmeister", url="https://example.com/caretaker", company="B")]  # 0
    _, mock_sync = _run({"scrape_hamburg_karriere": jobs}, dry_run=False,
                        sync_result={"added": 1, "skipped": 0, "failed": 0, "failed_jobs": []})
    sent = mock_sync.call_args.args[0]
    assert [j["url"] for j in sent] == ["https://example.com/ai"]
    # and the low job does not linger in the holdover queue either
    result, _ = _run()
    assert result == []



# ---------------------------------------------------------------- weekly report

def test_report_is_sent_once_per_day_on_request(env, monkeypatch, capsys):
    monkeypatch.setenv("SEND_REPORT", "true")
    monkeypatch.setenv("MAIL_USER", "me@example.com")
    monkeypatch.setenv("MAIL_APP_PASSWORD", "abcdefghijklmnop")
    with patch("main.send_mail", return_value=(True, "sent to me@example.com")) as send:
        _run({"scrape_hamburg_karriere": [_job()]}, dry_run=False,
             sync_result={"added": 1, "skipped": 0, "failed": 0, "failed_jobs": []})
        _run(dry_run=False, sync_result={"added": 0, "skipped": 0, "failed": 0, "failed_jobs": []})
    assert send.call_count == 1
    subject = send.call_args.args[0]
    assert subject.startswith("jobhunt week ")
    assert "Already sent today" in capsys.readouterr().out


def test_report_not_sent_on_dry_run(env, monkeypatch):
    monkeypatch.setenv("SEND_REPORT", "true")
    monkeypatch.setenv("MAIL_USER", "me@example.com")
    monkeypatch.setenv("MAIL_APP_PASSWORD", "abcdefghijklmnop")
    with patch("main.send_mail") as send:
        _run({"scrape_hamburg_karriere": [_job()]}, dry_run=True)
    send.assert_not_called()


def test_mail_failure_does_not_fail_the_run(env, monkeypatch, capsys):
    monkeypatch.setenv("SEND_REPORT", "true")
    monkeypatch.setenv("MAIL_USER", "me@example.com")
    monkeypatch.setenv("MAIL_APP_PASSWORD", "abcdefghijklmnop")
    with patch("main.send_mail", return_value=(False, "SMTPAuthenticationError: nope")):
        result, _ = _run({"scrape_hamburg_karriere": [_job()]}, dry_run=False,
                         sync_result={"added": 1, "skipped": 0, "failed": 0, "failed_jobs": []})
    assert len(result) == 1
    assert "[WARN] Weekly report not sent: SMTPAuthenticationError" in capsys.readouterr().out
