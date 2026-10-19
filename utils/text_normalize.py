"""Shared text-normalization helpers.

Used by:
  - utils/pipeline.py (cross-platform deduplication)
  - utils/feedback_features.py (feedback-loop token extraction)

Promoted from utils/pipeline.py (private _normalize_company / _normalize_title)
so both modules use a single source of truth. Divergent normalizers would silently
break the link between dedup keys and feedback-loop tokens.
"""
import re

LEGAL_SUFFIXES = re.compile(
    r"\b(gmbh\s*&\s*co\.?\s*kg|gmbh|ag|se|e\.v\.|ev|kg|ohg|gbr|ug|ltd|inc|llc|corp|s\.a\.)\b",
    re.IGNORECASE,
)
SENIORITY_PREFIXES = re.compile(
    r"\b(senior|sr\.?|junior|jr\.?|lead|principal|head\s+of|leiter(?:in)?|sachbearbeiter(?:in)?|"
    r"leitende[rn]?|stellvertretende[rn]?)\b",
    re.IGNORECASE,
)
TITLE_SYNONYMS = {
    "ingenieur": "engineer",
    "ingenieure": "engineer",
    "entwickler": "developer",
    "entwicklerin": "developer",
    "leiter": "head",
    "leiterin": "head",
    "sachbearbeiter": "specialist",
    "sachbearbeiterin": "specialist",
    "kaufmann": "manager",
    "kauffrau": "manager",
    "berater": "consultant",
    "beraterin": "consultant",
    "analytiker": "analyst",
    "analytikerin": "analyst",
}

# German + English stopwords for tokenize_title. Conservative list: only common
# function words that are clearly never the meaningful part of a job title.
STOPWORDS = frozenset({
    # German
    "der", "die", "das", "den", "dem", "des",
    "ein", "eine", "einen", "einer", "eines",
    "für", "fuer", "mit", "ohne", "und", "oder", "aber",
    "ist", "sind", "wird", "werden", "haben",
    "auch", "noch", "schon", "sehr", "mehr",
    "alle", "alles", "diese", "diesem", "dieser",
    # English
    "the", "and", "for", "with", "without", "from", "into", "onto",
    "this", "that", "these", "those",
    "have", "has", "had", "will", "would", "could", "should",
    "must", "may", "might", "shall",
    # job-title noise that's gender markers / common abbrev
    "mwd", "fmd", "divers",
})


def normalize_company(name: str) -> str:
    """Normalize a company name for matching: lowercase, strip legal suffixes, collapse whitespace."""
    name = name.lower()
    name = LEGAL_SUFFIXES.sub("", name)
    name = re.sub(r"[^\w\s]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def normalize_title(title: str) -> str:
    """Normalize a job title: lowercase, strip seniority prefixes, apply DE→EN synonyms, collapse whitespace."""
    title = title.lower()
    title = SENIORITY_PREFIXES.sub("", title)
    words = title.split()
    words = [TITLE_SYNONYMS.get(w, w) for w in words]
    title = " ".join(words)
    title = re.sub(r"[^\w\s]", " ", title)
    return re.sub(r"\s+", " ", title).strip()


_TOKEN_SPLIT = re.compile(r"[\s/_\-()]+")


def tokenize_title(title: str) -> set[str]:
    """Extract meaningful tokens from a job title for feedback-loop matching.

    Steps: normalize_title → split on whitespace + /-_() → drop tokens <4 chars
    → drop stopwords. Returns a set (each token counted once per title).
    """
    if not title:
        return set()
    normalized = normalize_title(title)
    raw_tokens = _TOKEN_SPLIT.split(normalized)
    return {
        tok for tok in raw_tokens
        if len(tok) >= 4 and tok not in STOPWORDS
    }


_TITLE_LABEL = re.compile(r"^(stellenbezeichnung|stellentitel|jobtitel|job title|position)\s*:?\s+", re.IGNORECASE)


def clean_title(title: str) -> str:
    """Display cleanup for scraped titles: collapse line breaks and runs of
    whitespace, and drop a form label some employers paste into the title
    field ("Stellenbezeichnung\nKI-Entwickler ...")."""
    title = " ".join((title or "").split())
    return _TITLE_LABEL.sub("", title, count=1) or title


# Words that say nothing about whether a job is wanted: they sit in almost
# every title, so a few rejections would turn them into blockers ("manager",
# "consultant", "data", "hamburg" all reached the stop-list after 19 clicks on
# 16 Oct 2026). The feedback loop never learns from these. The search
# vocabulary (queries + role terms) is excluded on top, at runtime.
GENERIC_TITLE_WORDS = frozenset({
    # role nouns and levels
    "manager", "management", "managerin", "consultant", "consulting", "berater",
    "beraterin", "engineer", "engineering", "ingenieur", "developer", "entwickler",
    "entwicklerin", "specialist", "spezialist", "spezialistin", "expert", "experte",
    "expertin", "analyst", "analystin", "associate", "lead", "leiter", "leiterin",
    "head", "director", "senior", "junior", "level", "team", "teamlead",
    "teamleiter", "referent", "referentin", "officer", "owner", "product",
    "project", "projekt", "projekte", "mitarbeiter", "mitarbeiterin",
    "sachbearbeiter", "sachbearbeiterin", "assistant", "assistenz", "staff",
    "principal", "partner", "intern", "werkstudent", "trainee",
    # employment and gender markers
    "genders", "gender", "vollzeit", "teilzeit", "remote", "hybrid", "befristet",
    "unbefristet",
    # places
    "hamburg", "berlin", "münchen", "munich", "frankfurt", "köln", "cologne",
    "düsseldorf", "stuttgart", "leipzig", "bremen", "hannover", "germany",
    "deutschland", "dach", "austria", "österreich", "schweiz", "switzerland",
    "europe", "emea",
})


def learnable_tokens(title: str, vocabulary: frozenset = frozenset()) -> set[str]:
    """Title tokens the feedback loop may learn from: tokenize_title minus
    generic title words minus the search vocabulary."""
    return tokenize_title(title) - GENERIC_TITLE_WORDS - vocabulary
