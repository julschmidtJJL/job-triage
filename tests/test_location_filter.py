import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from main import is_dach_location, is_german_location, keep_location  # noqa: E402 — imported after the sys.path patch


def test_hamburg_kept():
    assert is_german_location("Hamburg") is True


def test_berlin_germany_kept():
    assert is_german_location("Berlin, Deutschland") is True


def test_hamburg_twice_kept():
    assert is_german_location("Hamburg, Hamburg") is True


def test_london_removed():
    assert is_german_location("London, UK") is False


def test_remote_kept():
    assert is_german_location("Remote") is True


def test_empty_kept():
    assert is_german_location("") is True


def test_new_york_removed():
    assert is_german_location("New York, US") is False


def test_dach_requires_a_real_place():
    assert is_dach_location("Wien, Österreich") is True
    assert is_dach_location("Zürich, Switzerland") is True
    assert is_dach_location("Remote") is False
    assert is_dach_location("European Union (Remote)") is False
    assert is_dach_location("") is False


def test_remote_jobs_need_a_dach_place():
    assert keep_location({"remote": True, "location": "Berlin, Germany"}) is True
    assert keep_location({"remote": True, "location": "Remote"}) is False
    assert keep_location({"remote": True, "location": "European Union (Remote)"}) is False
    assert keep_location({"remote": True, "location": "New York, US"}) is False


def test_non_remote_jobs_use_the_hamburg_area():
    assert keep_location({"location": "Norderstedt"}) is True
    assert keep_location({"location": "München"}) is False
