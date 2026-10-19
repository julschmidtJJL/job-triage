"""Search configuration and the private fit profile.

Two layers, kept apart on purpose:

  - profile/queries.json (committed): the search queries and the Tier-1 role
    terms. This is the experimental variable — change it freely and watch the
    per-query scoreboard (utils/query_stats.py).
  - the fit profile (private): a distilled description of who the candidate
    is and which situations fit. Read from the FIT_PROFILE env var (a GitHub
    secret in Actions) or, locally, from profile/profile.md (gitignored).
"""
import json
import os
import re
from pathlib import Path

PROFILE_DIR = Path(__file__).parent.parent / "profile"
QUERIES_FILE = PROFILE_DIR / "queries.json"
PROFILE_FILE = PROFILE_DIR / "profile.md"
PROFILE_ENV = "FIT_PROFILE"


def load_search_config(path: Path = QUERIES_FILE) -> dict:
    """Return {"queries": [...all query dicts...], "role_terms": [...]}."""
    if not path.exists():
        return {"queries": [], "role_terms": []}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        "queries": [q for q in raw.get("queries", []) if q.get("q")],
        "role_terms": [t.lower() for t in raw.get("role_terms", []) if t],
    }


def active_queries(config: dict) -> list[str]:
    return [q["q"] for q in config.get("queries", []) if q.get("active", True)]


def load_profile_text(path: Path = PROFILE_FILE) -> str:
    """The fit profile as plain text. Env var wins over the local file.
    Returns "" if neither exists — the caller decides how to degrade."""
    from_env = os.environ.get(PROFILE_ENV, "").strip()
    if from_env:
        return from_env
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


def _term_pattern(term: str) -> re.Pattern:
    # Short terms need word boundaries so "ai" does not hit "detail" and "ki"
    # does not hit "Kiel". Longer terms match anywhere, because German
    # compounds hide them: "Digitalisierungsmanager", "Prozessautomatisierung".
    if len(term) <= 4:
        return re.compile(r"(?<![a-zäöüß0-9])" + re.escape(term) + r"(?![a-zäöüß0-9])")
    return re.compile(re.escape(term))


def matched_role_terms(text: str, role_terms: list[str]) -> list[str]:
    text = (text or "").lower()
    return [t for t in role_terms if _term_pattern(t).search(text)]


def search_vocabulary(path: Path = QUERIES_FILE) -> frozenset:
    """Every token of the configured queries and role terms. Jobs are found
    BY these words, so they cannot also be evidence against a job."""
    from utils.text_normalize import tokenize_title
    config = load_search_config(path)
    words: set = set()
    for q in config["queries"]:
        words |= tokenize_title(q["q"])
    for term in config["role_terms"]:
        words |= tokenize_title(term)
    return frozenset(words)
