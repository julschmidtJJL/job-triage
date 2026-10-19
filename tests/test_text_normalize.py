"""Tests for utils/text_normalize.py.

The normalize_company and normalize_title functions were extracted from
utils/pipeline.py during the feedback-loop PR. These tests guard against
regression — extracted output must match the original behavior so cross-platform
dedup keeps working identically.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.text_normalize import (
    LEGAL_SUFFIXES,
    SENIORITY_PREFIXES,
    STOPWORDS,
    TITLE_SYNONYMS,
    normalize_company,
    normalize_title,
    tokenize_title,
)


# ---------------------------------------------------------------- normalize_company

def test_strips_gmbh():
    assert normalize_company("Firma A GmbH") == "firma a"


def test_strips_ag():
    assert normalize_company("BMW AG") == "bmw"


def test_strips_gmbh_und_co_kg():
    assert normalize_company("Siemens GmbH & Co. KG") == "siemens"


def test_handles_punctuation():
    assert normalize_company("Acme, Inc.") == "acme"


def test_collapses_whitespace():
    assert normalize_company("  X    Y  GmbH  ") == "x y"


def test_company_empty_string():
    assert normalize_company("") == ""


# ---------------------------------------------------------------- normalize_title

def test_strips_senior():
    assert normalize_title("Senior Data Engineer") == "data engineer"


def test_german_to_english_synonym():
    # ingenieur → engineer (NOT leiter — that's in SENIORITY_PREFIXES which
    # strips first, leaving nothing for the synonym map to translate)
    assert "engineer" in normalize_title("Ingenieur für Wartung")


def test_title_lowercased():
    assert normalize_title("CFO (m/w/d)") == "cfo m w d"


def test_title_empty_string():
    assert normalize_title("") == ""


# ---------------------------------------------------------------- tokenize_title

def test_tokenize_basic():
    toks = tokenize_title("Senior Data Engineer")
    # senior stripped by normalize_title, data + engineer remain
    assert "data" in toks
    assert "engineer" in toks


def test_tokenize_drops_short_tokens():
    toks = tokenize_title("CFO m/w/d Finance")
    # "cfo" = 3 chars → dropped, "m", "w", "d" → dropped
    assert "finance" in toks
    assert "cfo" not in toks
    assert "m" not in toks


def test_tokenize_drops_stopwords():
    toks = tokenize_title("Manager für Buchhaltung und Controlling")
    assert "manager" in toks
    assert "buchhaltung" in toks
    assert "controlling" in toks
    assert "für" not in toks
    assert "und" not in toks


def test_tokenize_german_morphology_smoke():
    """Same root different ending — tokenize doesn't stem, but the same word
    in the same form should match across postings. This is a smoke test that
    the function doesn't choke on German morphology, NOT a claim of stemming."""
    toks_a = tokenize_title("Polierer")
    toks_b = tokenize_title("Polierer (m/w/d) gesucht")
    assert toks_a == {"polierer"}
    assert "polierer" in toks_b


def test_tokenize_handles_separators():
    """Slashes, dashes, parens all split tokens."""
    toks = tokenize_title("Maschinen-Bediener/Polierer (Vollzeit)")
    assert "maschinen" in toks
    assert "bediener" in toks
    assert "polierer" in toks
    assert "vollzeit" in toks


def test_tokenize_empty_string():
    assert tokenize_title("") == set()


def test_tokenize_returns_set_not_list():
    """Same token appearing twice is counted once per title (the Counter
    update aggregates ACROSS titles, but within one title we want unique)."""
    toks = tokenize_title("Polierer Polierer Polierer")
    assert toks == {"polierer"}


# ---------------------------------------------------------------- regression guard

def test_legal_suffixes_regex_includes_german_forms():
    """If this list shrinks, cross-platform dedup loses bucket merges."""
    # Original pipeline.py shipped these — assertion locks them in.
    for suffix in ["gmbh", "ag", "se", "kg", "ohg", "ltd", "inc", "llc"]:
        s = f"Test {suffix.upper()}"
        result = normalize_company(s)
        assert suffix not in result, f"'{suffix}' should be stripped from {s!r}, got {result!r}"


def test_seniority_prefixes_regex_includes_german_forms():
    """German seniority terms must still be stripped or dedup misses
    'Senior Polierer' vs 'Polierer' merges."""
    for prefix in ["senior", "junior", "leiter", "leiterin"]:
        s = f"{prefix.capitalize()} Engineer"
        result = normalize_title(s)
        assert prefix not in result, f"'{prefix}' should be stripped from {s!r}, got {result!r}"


def test_title_synonyms_map_de_to_en():
    """Synonym mapping is the link between 'Ingenieur' on Stepstone and
    'Engineer' on LinkedIn for the same role. Note: terms that ALSO appear
    in SENIORITY_PREFIXES (leiter, sachbearbeiter) get stripped before the
    synonym map runs — that's the original behavior, not a bug."""
    _STRIPPED_BY_SENIORITY = {"leiter", "leiterin", "sachbearbeiter", "sachbearbeiterin"}
    for de, en in TITLE_SYNONYMS.items():
        if de in _STRIPPED_BY_SENIORITY:
            continue
        result = normalize_title(de)
        assert en in result, f"{de!r} should map to {en!r}, got {result!r}"


def test_stopwords_excludes_meaningful_terms():
    """Sanity check: don't drop words that are actually job-title content."""
    for meaningful in ["finance", "engineer", "developer", "manager", "polierer", "berater"]:
        assert meaningful not in STOPWORDS, f"'{meaningful}' must not be a stopword"


def test_clean_title_drops_form_label_and_line_breaks():
    from utils.text_normalize import clean_title
    assert clean_title("Stellenbezeichnung\nKI-Entwickler für Sprach- und Signalverarbeitung (m/w/d)") == \
        "KI-Entwickler für Sprach- und Signalverarbeitung (m/w/d)"
    assert clean_title("Job title: AI Engineer") == "AI Engineer"
    assert clean_title("  AI   Engineer \n (m/w/d) ") == "AI Engineer (m/w/d)"
    # a title that is only the label stays as it is rather than becoming empty
    assert clean_title("Stellenbezeichnung") == "Stellenbezeichnung"
    # "Position" inside a title is not a label
    assert clean_title("Positionsmanager Data") == "Positionsmanager Data"
