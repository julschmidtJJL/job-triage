"""A job meant for Claude never reaches the board with a keyword score.

A failed batch once let jobs keep their keyword prefilter score (easily 100)
and most of that day's board rows carried it. These tests pin the fix: retry, circuit breaker, last Claude
result as the fallback, "unscored" jobs held in the queue, and no sync when
more than half went unscored."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import main as m
import utils.pipeline as pl
from utils.holdover import carry_unscored
from utils.pipeline import JobScore, ScoreBatch, score_jobs
from tests.test_main import _job, _run, env  # noqa: F401  (env is a fixture)

ROLE_TERMS = ["ai", "ki", "data"]


def _result(**fields) -> JobScore:
    defaults = dict(
        reason="r", situation_fit="s", skills=["Python"], ad_language="German", contact_person="Anna",
        seniority="Mid", required_years=3, candidate_years=6, hard_requirements=["SQL"],
        requirements_flexible=False, unmet_requirements=[], score=7,
    )
    return JobScore(**{**defaults, **fields})


def _client(fail_when=lambda postings, call_no: False, drop_ids=0, error=None):
    """messages.parse scores every posting 7, unless fail_when(postings, call_no)
    is true (then it raises `error`); drop_ids leaves that many ids out."""
    calls = {"n": 0}

    def parse(**kwargs):
        calls["n"] += 1
        postings = json.loads(kwargs["messages"][0]["content"])
        if fail_when(postings, calls["n"]):
            raise error or RuntimeError("credential validation failed")
        kept = postings[drop_ids:]
        return SimpleNamespace(parsed_output=ScoreBatch(results=[_result(id=p["id"]) for p in kept]),
                               stop_reason="end_turn", usage=None)

    client = MagicMock()
    client.messages.parse.side_effect = parse
    return client


def _job_ad(i: int, **extra) -> dict:
    return {"title": f"AI Engineer {i}", "company": f"Firma {i}", "location": "Hamburg",
            "url": f"https://example.com/{i}", "description": "AI data role", "source": "test",
            "fetch_method": "full_jd", **extra}


def _score(jobs, client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    with patch.object(pl.anthropic, "Anthropic", return_value=client):
        return score_jobs(jobs, role_terms=ROLE_TERMS, profile_text="PROFILE")


def _held(i: int, score=60) -> dict:
    """A job held over from an earlier run that Claude scored then."""
    last = {"relevance_score": score, "scoring_method": "claude", "seniority": "Junior",
            "unmet_requirements": [], "ai_assessment": "old but real"}
    return _job_ad(i, holdover_runs=1, last_claude=last, scoring_method="claude",
                   unmet_requirements=["8 years"], seniority="Senior", relevance_score=score)


# ---------------------------------------------------------------- pipeline.py

def test_fresh_job_in_failed_batch_is_unscored_not_keyword(monkeypatch):
    jobs = _score([_job_ad(1)], _client(fail_when=lambda p, n: True), monkeypatch)
    assert jobs[0]["scoring_method"] == "unscored"


def test_held_job_falls_back_to_its_last_claude_result(monkeypatch):
    jobs = _score([_held(1, score=60)], _client(fail_when=lambda p, n: True), monkeypatch)
    job = jobs[0]
    assert job["scoring_method"] == "claude"
    assert job["relevance_score"] == 60           # not the keyword score
    assert job["seniority"] == "Junior"           # from the snapshot, not the stale field
    assert job["unmet_requirements"] == []
    assert "contact_person" not in job            # absent from the snapshot stays absent
    assert job["score_from_previous_run"] is True
    assert not job.get("scored_this_run")


def test_held_job_without_snapshot_is_unscored_and_loses_stale_fields(monkeypatch):
    job = _job_ad(1, holdover_runs=1, scoring_method="claude", unmet_requirements=["8 years"])
    jobs = _score([job], _client(fail_when=lambda p, n: True), monkeypatch)
    assert jobs[0]["scoring_method"] == "unscored"
    assert "unmet_requirements" not in jobs[0]


def test_held_job_scored_again_gets_todays_result(monkeypatch):
    jobs = _score([_held(1, score=60)], _client(), monkeypatch)
    assert jobs[0]["relevance_score"] == 70 and jobs[0]["scored_this_run"] is True
    assert jobs[0]["last_claude"]["relevance_score"] == 70


def test_id_missing_from_the_answer_is_unscored(monkeypatch, capsys):
    jobs = _score([_job_ad(1), _job_ad(2)], _client(drop_ids=1), monkeypatch)
    methods = sorted(j["scoring_method"] for j in jobs)
    assert methods == ["claude", "unscored"]
    assert "1 missing from the answer" in capsys.readouterr().out


def test_one_retry_rescues_a_batch(monkeypatch):
    client = _client(fail_when=lambda p, n: n == 1, error=RuntimeError("overloaded"))
    jobs = _score([_job_ad(1)], client, monkeypatch)
    assert jobs[0]["scoring_method"] == "claude"
    assert client.messages.parse.call_count == 2


def test_auth_error_twice_stops_all_further_calls(monkeypatch, capsys):
    jobs = [_job_ad(i) for i in range(1, 33)]  # 4 batches
    client = _client(fail_when=lambda p, n: n >= 2)  # batch 1 fine, then the key is refused
    scored = _score(jobs, client, monkeypatch)
    assert client.messages.parse.call_count == 3  # batch 1, batch 2 + its retry, nothing after
    assert sum(j["scoring_method"] == "unscored" for j in scored) == 24
    assert pl.scoring_stats() == {"batches": 4, "failed": 1, "stopped_at": 2}
    assert "API refused the key twice at batch 2 of 4: stopped, 24 jobs" in capsys.readouterr().out


def test_other_errors_do_not_trip_the_breaker(monkeypatch):
    jobs = [_job_ad(i) for i in range(1, 17)]  # 2 batches
    client = _client(fail_when=lambda p, n: n <= 2, error=RuntimeError("boom"))
    scored = _score(jobs, client, monkeypatch)
    assert client.messages.parse.call_count == 3  # batch 1 twice, batch 2 once
    assert sum(j["scoring_method"] == "claude" for j in scored) == 8


def test_forced_failure_hook_fails_from_batch_n_plus_one(monkeypatch):
    monkeypatch.setenv("SCORING_FAIL_AFTER_BATCHES", "1")
    scored = _score([_job_ad(i) for i in range(1, 17)], _client(), monkeypatch)
    assert sum(j["scoring_method"] == "unscored" for j in scored) == 8


def test_claude_fields_cover_everything_apply_result_writes():
    job: dict = {}
    pl._apply_result(job, _result(id="x"), "PROFILE")
    written = set(job) - {"relevance_score", "last_claude", "scored_this_run"}
    assert written <= set(pl.CLAUDE_FIELDS), written - set(pl.CLAUDE_FIELDS)


# ---------------------------------------------------------------- routing

def _routed(jobs, profile="PROFILE", min_score=40, threshold=20):
    return m.route_scored(jobs, profile, threshold=threshold, min_score=min_score, top_n=30)


def test_unscored_jobs_wait_in_the_queue_not_on_the_board():
    good = {"url": "a", "prefilter_score": 40, "scoring_method": "claude", "relevance_score": 70}
    bad = {"url": "b", "prefilter_score": 100, "scoring_method": "unscored", "relevance_score": 100}
    r = _routed([good, bad])
    assert [j["url"] for j in r["top"]] == ["a"]
    assert [(j["url"], j["unscored_runs"]) for j in r["retry"]] == [("b", 1)]
    assert not r["outage"]


def test_keyword_only_job_never_reaches_the_board_with_a_profile():
    # MIN_NOTION_SCORE below the Claude threshold: still no keyword row on the board.
    kw = {"url": "k", "prefilter_score": 15, "scoring_method": "keyword_fallback", "relevance_score": 15}
    assert _routed([kw], min_score=10)["top"] == []
    assert [j["url"] for j in _routed([kw], profile="", min_score=10)["top"]] == ["k"]


def test_outage_above_half_unscored_only():
    def jobs(unscored, scored):
        return ([{"url": f"u{i}", "prefilter_score": 40, "scoring_method": "unscored"} for i in range(unscored)]
                + [{"url": f"s{i}", "prefilter_score": 40, "scoring_method": "claude", "relevance_score": 50}
                   for i in range(scored)])
    assert _routed(jobs(6, 4))["outage"] is True
    assert _routed(jobs(4, 6))["outage"] is False
    assert _routed(jobs(5, 5))["outage"] is False  # exactly half is not "more than half"


def test_unscored_jobs_give_up_after_three_runs():
    retry, given_up = carry_unscored([{"url": "a", "unscored_runs": 2}, {"url": "b"}])
    assert [j["url"] for j in given_up] == ["a"]
    assert retry == [{"url": "b", "unscored_runs": 1}]


def test_a_scored_job_loses_its_unscored_counter():
    job = {"url": "a", "prefilter_score": 40, "scoring_method": "claude", "scored_this_run": True,
           "relevance_score": 60, "unscored_runs": 2}
    assert "unscored_runs" not in _routed([job])["top"][0]


def test_scoring_line():
    jobs = [{"scored_this_run": True}, {"score_from_previous_run": True}, {}]
    line = m.format_scoring(jobs, 1, {"batches": 4, "failed": 1, "stopped_at": 2})
    assert line == ("[SCORING] claude 2 (this run 1, previous run 1) | unscored 1 | "
                    "batches failed 1 of 4 | stopped at batch 2")


# ---------------------------------------------------------------- end to end

@pytest.fixture
def scoring_env(env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(m, "load_profile_text", lambda: "PROFILE")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(pl, "fetch_full_description", lambda url, source: ("AI data role, full ad.", "full_jd"))
    return env


def _ten_jobs():
    return [_job(title=f"AI Engineer {i}", company=f"Firma {i}", url=f"https://example.com/{i}",
                 source="hamburg_karriere") for i in range(10)]


def _real_run(client, sync_result=None):
    with patch.object(pl.anthropic, "Anthropic", return_value=client), \
         patch("main.sync_auto_dropped", return_value={"added": 0}) as auto:
        try:
            _, sync = _run({"scrape_hamburg_karriere": _ten_jobs()}, dry_run=False, sync_result=sync_result)
            return sync, auto, None
        except m.PipelineFailed as e:
            return None, auto, e


def test_outage_syncs_nothing_and_fails_the_run(scoring_env, capsys):
    with patch("main.sync_jobs") as sync, patch.object(pl.anthropic, "Anthropic",
                                                       return_value=_client(fail_when=lambda p, n: True)), \
         patch("main.sync_auto_dropped") as auto:
        patches = [patch(f"main.{name}", return_value=_ten_jobs() if name == "scrape_hamburg_karriere" else [])
                   for name in __import__("tests.test_main", fromlist=["SCRAPERS"]).SCRAPERS]
        for p in patches:
            p.start()
        try:
            with pytest.raises(m.PipelineFailed, match="could not score 10 of 10"):
                m.run_pipeline(dry_run=False)
        finally:
            for p in patches:
                p.stop()
    sync.assert_not_called()
    auto.assert_not_called()
    assert "nothing written, run failed" in capsys.readouterr().out


def test_partial_failure_syncs_only_claude_rows_and_queues_the_rest(scoring_env):
    # 10 jobs = 2 batches; the second (2 jobs) fails: 20 % unscored, the run goes on.
    client = _client(fail_when=lambda postings, n: len(postings) == 2)
    sync, _, failed = _real_run(client)
    assert failed is None
    synced = sync.call_args.args[0]
    assert len(synced) == 8 and all(j["scoring_method"] == "claude" for j in synced)
    queue = json.loads((scoring_env / "holdover.json").read_text())
    assert sorted(j["unscored_runs"] for j in queue) == [1, 1]


def test_upload_failures_keep_the_unscored_jobs_in_the_queue(scoring_env):
    client = _client(fail_when=lambda postings, n: len(postings) == 2)
    with patch.object(pl.anthropic, "Anthropic", return_value=client), \
         patch("main.sync_auto_dropped", return_value={"added": 0}):
        def sync_fails(jobs):
            return {"added": 1, "skipped": 0, "failed": len(jobs) - 1, "failed_jobs": jobs[1:]}
        patches = [patch(f"main.{name}", return_value=_ten_jobs() if name == "scrape_hamburg_karriere" else [])
                   for name in __import__("tests.test_main", fromlist=["SCRAPERS"]).SCRAPERS]
        patches.append(patch("main.sync_jobs", side_effect=sync_fails))
        for p in patches:
            p.start()
        try:
            m.run_pipeline(dry_run=False)
        finally:
            for p in patches:
                p.stop()
    queue = json.loads((scoring_env / "holdover.json").read_text())
    assert sum(1 for j in queue if j.get("unscored_runs") == 1) == 2
    assert sum(1 for j in queue if j.get("scoring_method") == "claude") == 7


def test_forced_failure_hook_is_ignored_in_a_real_run(scoring_env, monkeypatch, capsys):
    monkeypatch.setenv("SCORING_FAIL_AFTER_BATCHES", "0")
    sync, _, failed = _real_run(_client())
    assert failed is None and len(sync.call_args.args[0]) == 10
    assert "dry-run test hook: ignored" in capsys.readouterr().out
