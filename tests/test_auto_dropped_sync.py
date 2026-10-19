"""Tests for notion_sync.sync_auto_dropped + sidecar metadata writing."""
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from notion_sync.sync import _write_sidecar, sync_auto_dropped


def _dropped(url, token="polierer", pipeline="finance"):
    return {
        "url": url,
        "title": "Polierer Senior",
        "company": "X GmbH",
        "stop_reason": f"matched title token '{token}' (rejected 3x)",
        "stop_token": token,
        "stop_pipeline": pipeline,
    }


def test_write_sidecar_creates_structured_entry(tmp_path, monkeypatch):
    monkeypatch.setattr("notion_sync.sync.SEEN_URLS_FILE", tmp_path / "seen.json")
    monkeypatch.setattr("notion_sync.sync.DATA_DIR", tmp_path)
    _write_sidecar([_dropped("https://x.de/1")])
    cache = json.loads((tmp_path / "seen.json").read_text(encoding="utf-8"))
    entry = cache["https://x.de/1"]
    assert isinstance(entry, dict)
    assert entry["dropped_token"] == "polierer"
    assert entry["dropped_pipeline"] == "finance"
    assert "dropped_at" in entry


def test_write_sidecar_preserves_existing_entries(tmp_path, monkeypatch):
    seen = tmp_path / "seen.json"
    seen.write_text(json.dumps({"https://x.de/other": "2026-05-01T00:00:00"}), encoding="utf-8")
    monkeypatch.setattr("notion_sync.sync.SEEN_URLS_FILE", seen)
    monkeypatch.setattr("notion_sync.sync.DATA_DIR", tmp_path)
    _write_sidecar([_dropped("https://x.de/new")])
    cache = json.loads(seen.read_text(encoding="utf-8"))
    assert "https://x.de/other" in cache
    assert "https://x.de/new" in cache


def test_sync_auto_dropped_writes_sidecar_even_without_notion(tmp_path, monkeypatch):
    """No NOTION_API_KEY → still writes sidecar (enables local-only testing)."""
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    monkeypatch.delenv("NOTION_JOB_BOARD_DB_ID", raising=False)
    monkeypatch.setattr("notion_sync.sync.SEEN_URLS_FILE", tmp_path / "seen.json")
    monkeypatch.setattr("notion_sync.sync.DATA_DIR", tmp_path)
    result = sync_auto_dropped([_dropped("https://x.de/1")])
    assert result == {"added": 0, "skipped": 0}
    assert (tmp_path / "seen.json").exists()


def test_sync_auto_dropped_empty_noop():
    assert sync_auto_dropped([]) == {"added": 0, "skipped": 0}
