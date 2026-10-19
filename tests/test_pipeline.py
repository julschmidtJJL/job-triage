import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import utils.pipeline as pl

SAMPLE_JOBS = [
    {
        "title": "Data Analyst",
        "company": "Company A",
        "url": "https://example.com/1",
        "description": "Python and SQL required",
        "source": "linkedin",
    },
    {
        "title": "Software Engineer",
        "company": "Company B",
        "url": "https://example.com/2",
        "description": "Java Spring Boot",
        "source": "linkedin",
    },
]


# ---------------------------------------------------------------------------
# deduplicate()
# ---------------------------------------------------------------------------


def test_deduplicate_removes_already_seen_url(tmp_path):
    # neues Dict-Format
    seen_file = tmp_path / "jobs_seen_urls.json"
    seen_file.write_text(
        json.dumps({"https://example.com/1": datetime.now().isoformat()}), encoding="utf-8"
    )

    with patch.object(pl, "SEEN_URLS_FILE", seen_file), patch.object(pl, "DATA_DIR", tmp_path):
        result = pl.deduplicate(SAMPLE_JOBS)

    assert len(result) == 1
    assert result[0]["url"] == "https://example.com/2"


def test_deduplicate_migrates_old_list_format(tmp_path):
    """The old list format is migrated to a dict automatically."""
    seen_file = tmp_path / "jobs_seen_urls.json"
    seen_file.write_text(json.dumps(["https://example.com/1"]), encoding="utf-8")

    with patch.object(pl, "SEEN_URLS_FILE", seen_file), patch.object(pl, "DATA_DIR", tmp_path):
        result = pl.deduplicate(SAMPLE_JOBS)

    # After migration: URL/1 already known → only URL/2 is new
    assert len(result) == 1
    assert result[0]["url"] == "https://example.com/2"

    # The saved format is now a dict
    written = json.loads(seen_file.read_text(encoding="utf-8"))
    assert isinstance(written, dict)


def test_deduplicate_prunes_expired_urls(tmp_path):
    """URLs older than 30 days are removed on load."""
    seen_file = tmp_path / "jobs_seen_urls.json"
    old_ts = (datetime.now() - timedelta(days=31)).isoformat()
    seen_file.write_text(
        json.dumps({"https://example.com/1": old_ts}), encoding="utf-8"
    )

    with patch.object(pl, "SEEN_URLS_FILE", seen_file), patch.object(pl, "DATA_DIR", tmp_path):
        result = pl.deduplicate(SAMPLE_JOBS)

    # Expired URL/1 is seen as new again
    assert any(j["url"] == "https://example.com/1" for j in result)


def test_deduplicate_keeps_recent_urls(tmp_path):
    """URLs within 30 days stay in the cache."""
    seen_file = tmp_path / "jobs_seen_urls.json"
    recent_ts = (datetime.now() - timedelta(days=10)).isoformat()
    seen_file.write_text(
        json.dumps({"https://example.com/1": recent_ts}), encoding="utf-8"
    )

    with patch.object(pl, "SEEN_URLS_FILE", seen_file), patch.object(pl, "DATA_DIR", tmp_path):
        result = pl.deduplicate(SAMPLE_JOBS)

    assert all(j["url"] != "https://example.com/1" for j in result)


def test_deduplicate_saves_timestamp_for_new_urls(tmp_path):
    """Newly seen URLs are saved with the current ISO timestamp."""
    seen_file = tmp_path / "jobs_seen_urls.json"

    with patch.object(pl, "SEEN_URLS_FILE", seen_file), patch.object(pl, "DATA_DIR", tmp_path):
        pl.deduplicate(SAMPLE_JOBS)

    written = json.loads(seen_file.read_text(encoding="utf-8"))
    assert isinstance(written, dict)
    for url, ts in written.items():
        # Timestamp must parse
        datetime.fromisoformat(ts)


def test_deduplicate_passes_new_urls(tmp_path):
    seen_file = tmp_path / "jobs_seen_urls.json"

    with patch.object(pl, "SEEN_URLS_FILE", seen_file), patch.object(pl, "DATA_DIR", tmp_path):
        result = pl.deduplicate(SAMPLE_JOBS)

    assert len(result) == 2


def test_deduplicate_writes_urls_to_file(tmp_path):
    seen_file = tmp_path / "jobs_seen_urls.json"

    with patch.object(pl, "SEEN_URLS_FILE", seen_file), patch.object(pl, "DATA_DIR", tmp_path):
        pl.deduplicate(SAMPLE_JOBS)

    written = json.loads(seen_file.read_text(encoding="utf-8"))
    assert "https://example.com/1" in written
    assert "https://example.com/2" in written


def test_deduplicate_accumulates_across_calls(tmp_path):
    seen_file = tmp_path / "jobs_seen_urls.json"

    with patch.object(pl, "SEEN_URLS_FILE", seen_file), patch.object(pl, "DATA_DIR", tmp_path):
        pl.deduplicate(SAMPLE_JOBS[:1])
        result = pl.deduplicate(SAMPLE_JOBS)

    assert len(result) == 1
    assert result[0]["url"] == "https://example.com/2"


def test_deduplicate_empty_input(tmp_path):
    seen_file = tmp_path / "jobs_seen_urls.json"

    with patch.object(pl, "SEEN_URLS_FILE", seen_file), patch.object(pl, "DATA_DIR", tmp_path):
        result = pl.deduplicate([])

    assert result == []
