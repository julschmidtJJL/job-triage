import importlib
import json
import os
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel

try:
    from thefuzz import fuzz as _fuzz
    _THEFUZZ_AVAILABLE = True
except ImportError:
    _THEFUZZ_AVAILABLE = False

DATA_DIR = Path(__file__).parent.parent / "data"
SEEN_URLS_FILE = DATA_DIR / "jobs_seen_urls.json"

CACHE_EXPIRY_DAYS = 30
DEFAULT_SCORE_THRESHOLD = 20
DEFAULT_SCORING_MODEL = "claude-haiku-5-5"


# Read at call time, not import time: main.py imports this module before
# run_pipeline() loads .env, so module-level constants would ignore .env.
def score_threshold() -> int:
    return int(os.getenv("CLAUDE_SCORE_THRESHOLD", str(DEFAULT_SCORE_THRESHOLD)))


def scoring_model() -> str:
    return os.getenv("SCORING_MODEL", DEFAULT_SCORING_MODEL)


DEFAULT_MIN_NOTION_SCORE = 40


def min_notion_score() -> int:
    """Jobs below this final score never reach Notion (or the holdover queue)."""
    return int(os.getenv("MIN_NOTION_SCORE", str(DEFAULT_MIN_NOTION_SCORE)))

# Normalization helpers live in utils/text_normalize.py so utils/feedback_features.py
# can reuse the exact same logic. Divergent normalizers would silently break the
# link between cross-platform dedup keys and feedback-loop tokens.
from utils.text_normalize import (
    normalize_company as _normalize_company,
    normalize_title as _normalize_title,
)

# How the full ad is fetched, per source: "module:function" of a fetcher that
# takes the job URL and returns the ad as plain text ("" when it fails), or
# None to keep the search snippet. A source not listed gets DEFAULT_FETCHER,
# which reads the job page itself. Fetchers are imported when first used, so a
# new source only needs its own entry here (or register_fetcher() at runtime).
DEFAULT_FETCHER = "utils.job_ad:fetch_ad_page"
FETCHERS: dict = {
    # Search-result links to many boards: the snippet is all we read.
    "google_jobs": None,
    # The job page is a JS app; the description comes from the API, or from
    # the cooperation partner's page when the API text is short.
    "arbeitsagentur": "scrapers.arbeitsagentur:fetch_description",
}


def register_fetcher(source: str, fetcher) -> None:
    """Use `fetcher` (a callable url -> text, a "module:function" string, or
    None for snippet only) for jobs whose job["source"] is `source`."""
    FETCHERS[source] = fetcher


def _resolve_fetcher(source: str):
    fetcher = FETCHERS.get(source, DEFAULT_FETCHER)
    if fetcher is None or callable(fetcher):
        return fetcher
    module, _, name = fetcher.partition(":")
    return getattr(importlib.import_module(module), name)


# ---------------------------------------------------------------- feedback


# Score deltas per the design's Part 3 table.
# Caps prevent any single signal from running away.
_FB_POS_TOKEN_PER = 10
_FB_POS_TOKEN_CAP = 30
_FB_POS_COMPANY = 30
_FB_NEG_TOKEN_PER = 15
_FB_NEG_TOKEN_CAP = 45
_FB_NEG_COMPANY = 20
# Frequency threshold below which a single mention does not influence scoring
# (avoids one accidental click setting policy).
_FB_MIN_COUNT = 2


def _apply_feedback_adjustments(jobs: list, features) -> None:
    """Mutate each job's relevance_score and add a 'feedback_adjustments' list
    of human-readable reasons. Clamps final score to [0, 100].
    """
    from utils.text_normalize import tokenize_title
    for job in jobs:
        title_tokens = tokenize_title(job.get("title", ""))
        company_norm = _normalize_company(job.get("company", ""))

        delta = 0
        reasons: list[str] = []

        # Positive title tokens (matches your applied/interesting history).
        pos_matches = [t for t in title_tokens if features.positive_title_tokens.get(t, 0) >= _FB_MIN_COUNT]
        if pos_matches:
            pos_delta = min(_FB_POS_TOKEN_PER * len(pos_matches), _FB_POS_TOKEN_CAP)
            delta += pos_delta
            reasons.append(f"+{pos_delta} positive tokens: {','.join(sorted(pos_matches))}")

        # Positive company (you applied to this company before).
        if company_norm and company_norm in features.positive_companies:
            delta += _FB_POS_COMPANY
            reasons.append(f"+{_FB_POS_COMPANY} company applied: {company_norm}")

        # Negative title tokens (matches your rejected history).
        neg_matches = [t for t in title_tokens if features.negative_title_tokens.get(t, 0) >= _FB_MIN_COUNT]
        if neg_matches:
            neg_delta = min(_FB_NEG_TOKEN_PER * len(neg_matches), _FB_NEG_TOKEN_CAP)
            delta -= neg_delta
            reasons.append(f"-{neg_delta} negative tokens: {','.join(sorted(neg_matches))}")

        # Negative company (you rejected this company >=2 times).
        if company_norm and features.negative_companies.get(company_norm, 0) >= _FB_MIN_COUNT:
            delta -= _FB_NEG_COMPANY
            reasons.append(f"-{_FB_NEG_COMPANY} company rejected: {company_norm}")

        if delta:
            new_score = max(0, min(100, job.get("relevance_score", 0) + delta))
            job["relevance_score"] = new_score
        # Always set the field even if empty so downstream code can rely on its presence
        existing = job.get("feedback_adjustments", [])
        job["feedback_adjustments"] = existing + reasons


def _deduplicate_cross_platform(jobs: list) -> list:
    if not _THEFUZZ_AVAILABLE:
        print("[WARN] thefuzz not available — cross-platform dedup skipped")
        return jobs

    # bucket by normalized company name
    buckets: dict[str, list] = {}
    for job in jobs:
        key = _normalize_company(job.get("company", ""))
        buckets.setdefault(key, []).append(job)

    merged: list = []
    for _company_key, group in buckets.items():
        # absorbed[j] = True means job j was merged into an earlier canonical
        absorbed = [False] * len(group)
        # possible_dup[j] = True means job j should be flagged in Notion
        possible_dup = [False] * len(group)

        for i in range(len(group)):
            if absorbed[i]:
                continue
            canonical = group[i]
            sources = [canonical.get("url", "")]

            for j in range(i + 1, len(group)):
                if absorbed[j]:
                    continue
                other = group[j]
                if canonical.get("source") == other.get("source"):
                    continue
                title_a = _normalize_title(canonical.get("title", ""))
                title_b = _normalize_title(other.get("title", ""))
                ratio = _fuzz.token_sort_ratio(title_a, title_b)

                if ratio >= 85:
                    absorbed[j] = True
                    sources.append(other.get("url", ""))
                    if len(other.get("description", "")) > len(canonical.get("description", "")):
                        canonical = dict(other)
                    print(
                        f"[DEDUP] Merged: '{group[i].get('title')}' "
                        f"({group[i].get('source')}) + '{other.get('title')}' "
                        f"({other.get('source')})"
                    )
                elif ratio >= 70:
                    # flag both jobs; both will be synced to Notion
                    possible_dup[i] = True
                    possible_dup[j] = True

            out = dict(canonical)
            out["sources"] = [u for u in sources if u]
            if possible_dup[i]:
                out["duplicate_status"] = "Possible Duplicate"
            merged.append(out)

    print(f"[INFO] Cross-platform dedup: {len(jobs)} → {len(merged)} jobs")
    return merged


def _load_seen_cache() -> dict:
    if not SEEN_URLS_FILE.exists():
        return {}

    raw = json.loads(SEEN_URLS_FILE.read_text(encoding="utf-8"))

    if isinstance(raw, list):
        now_iso = datetime.now().isoformat()
        cache = {url: now_iso for url in raw}
        print(f"[INFO] Cache migrated: {len(cache)} URLs upgraded to dict format")
        return cache

    cache = {url: ts for url, ts in raw.items() if isinstance(ts, str)}
    cutoff = datetime.now() - timedelta(days=CACHE_EXPIRY_DAYS)
    expired = [url for url, ts in cache.items() if datetime.fromisoformat(ts) < cutoff]
    for url in expired:
        del cache[url]

    if expired:
        print(f"[INFO] {len(expired)} expired URLs removed from cache")

    return cache


def deduplicate(jobs: list) -> list:
    cache = _load_seen_cache()

    seen_this_run: set = set()
    new_jobs = []
    for job in jobs:
        url = job.get("url")
        if url and url not in cache and url not in seen_this_run:
            new_jobs.append(job)
            seen_this_run.add(url)

    now_iso = datetime.now().isoformat()
    for job in new_jobs:
        cache[job["url"]] = now_iso
        # also cache all merged source URLs so losers never re-appear
        for src_url in job.get("sources", []):
            if src_url and src_url not in cache:
                cache[src_url] = now_iso

    DATA_DIR.mkdir(exist_ok=True)
    SEEN_URLS_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(
        f"[INFO] {len(new_jobs)} new jobs "
        f"(of {len(jobs)} total, {len(jobs) - len(new_jobs)} deduplicated)"
    )
    return new_jobs


def fetch_full_description(url: str, source: str) -> tuple[str, str]:
    """Returns (description_text, fetch_method) where fetch_method is 'full_jd' or 'snippet'."""
    fetcher = _resolve_fetcher(source)
    if fetcher is None or not url:
        return "", "snippet"
    text = fetcher(url)
    return (text, "full_jd") if text else ("", "snippet")


def _attach_full_ad(job: dict) -> None:
    """Fetch the full ad once and keep it on the job, so the board shows what
    the scorer read. Held-over jobs already carry it and are not fetched again."""
    if job.get("fetch_method") == "full_jd" and job.get("description"):
        return
    full_text, fetch_method = fetch_full_description(job.get("url", ""), job.get("source", ""))
    job["fetch_method"] = fetch_method
    if full_text:
        job.setdefault("description_snippet", job.get("description", ""))
        job["description"] = full_text


# ---------------------------------------------------------------- Tier 2


class UnmetRequirement(BaseModel):
    requirement: str
    # tool = a named tool, vendor or platform (Azure, SAP, Power BI...)
    kind: Literal["years", "domain", "language", "degree", "licence", "tool", "other"]
    # A verbatim quote from the profile that shows the candidate lacks it; "" if none.
    profile_evidence: str


class JobScore(BaseModel):
    id: str
    score: int  # 0-10
    reason: str
    situation_fit: str
    skills: list[str]
    # Read from the ad itself, for the board. Required, so the model always
    # answers them (empty when a snippet does not say).
    ad_language: str
    contact_person: str
    seniority: str
    required_years: int | None
    # Years the profile shows in the field required_years is about.
    candidate_years: int | None
    hard_requirements: list[str]
    requirements_flexible: bool
    unmet_requirements: list[UnmetRequirement]


class ScoreBatch(BaseModel):
    results: list[JobScore]


# An unmet must-have (that the ad does not soften) caps the 0-10 score here:
# the job still reaches the board, flagged, but not near the top.
UNMET_REQUIREMENT_CAP = 5

_SCORING_SYSTEM = (
    "You screen job postings for one specific candidate. Their profile follows.\n\n"
    "<profile>\n{profile}\n</profile>\n\n"
    "Score each posting 0-10 for how well it fits this candidate. Neither the industry "
    "nor the job title matters on its own: weigh what the profile says counts as a fit "
    "(for example the role's closeness to decision-makers, what the candidate would learn, "
    "the company's situation) above the sector and the title. If the profile lists "
    "questions that define a fit, score mainly on how the role answers them, regardless "
    "of the job title. Respect the hard constraints in the profile; a "
    "posting that requires a driver's licence, or is clearly outside the candidate's "
    "location rules, scores 3 or lower. If the company appears in the profile as "
    "already applied to or rejected by, say so in the reason.\n\n"
    "Most descriptions are the full ad; some are only a search-result snippet. Read the "
    "full ad for what the employer actually requires. Hard requirements are the ones the "
    "ad presents as must-haves (\"required\", \"vorausgesetzt\", \"zwingend\", "
    "\"mindestens 5 Jahre\", a degree or licence, experience in a specific field such as "
    "construction sites), not the nice-to-haves (\"ideally\", \"wünschenswert\", "
    "\"von Vorteil\", \"a plus\"). Many ads say outright that nobody is expected to bring "
    "every listed skill (\"you don't need to tick every box\", \"du musst nicht alles "
    "mitbringen\"); then set requirements_flexible and treat the list as a wish list, not a "
    "gate. A hard requirement is unmet only if the profile shows the candidate clearly lacks "
    "it; a skill the profile simply does not mention is not unmet. Weigh unmet hard "
    "requirements by how central they are: one the role cannot do without (years in a "
    "field the candidate never worked in, a domain they have no background in) puts the "
    "score at 5 or lower, unless the ad is flexible about it. Do not count keywords.\n\n"
    "A named tool, vendor or platform (Azure, AWS, GCP, Power BI, SAP, Salesforce, DATEV, "
    "Kubernetes...) that the profile does not list is learnable on the job: it is not an "
    "unmet requirement unless the profile explicitly says the candidate lacks it or will "
    "not work with it, and it lowers the score by at most 1. This holds even when the ad "
    "calls the tool a must-have. Years of experience, field or site experience (e.g. "
    "construction sites), language, degree and licence requirements are judged as above.\n\n"
    "For each posting return: id, score, reason (one sentence), situation_fit (one "
    "sentence on the company's situation and why it does or does not suit the "
    "candidate), and up to three skills from the profile the role would use, each a short "
    "skill name of one to four words (\"LLM integration\", \"Process automation\"), never a sentence. "
    "From the ad itself also return: ad_language (\"German\" or \"English\", the language "
    "the ad is written in), contact_person (the named contact or hiring manager with their "
    "role, e.g. \"Erika Mustermann, Recruiting\"; empty if none is named), "
    "seniority (one of Junior, Mid, Senior, Lead, Head, Executive), required_years (the "
    "minimum years of experience the ad asks for, or null), candidate_years (the years of "
    "experience the profile shows in the field those required years are about, such as logistics, "
    "construction or software engineering; count employed and founder work only where the "
    "profile says the candidate did that kind of work; null when required_years is null), "
    "hard_requirements (up to five "
    "short phrases such as \"5+ years in logistics\" or \"construction-site experience\"), "
    "requirements_flexible, and unmet_requirements (the hard requirements the candidate "
    "does not meet, same wording; empty if none), each with its kind (years, domain, "
    "language, degree, licence, tool, other) and profile_evidence: a verbatim quote from "
    "the profile that shows the candidate lacks it, or empty. Leave a field empty when the "
    "description is only a snippet and does not say."
)


def _short(items: list[str], limit: int, length: int = 80) -> list[str]:
    return [i.strip() for i in items if i and i.strip() and len(i.strip()) <= length][:limit]


# Belt and braces for a tool gap the model files under "domain" or "other".
_TOOL_NAMES = re.compile(
    r"\b(azure|aws|amazon web services|gcp|google cloud|power ?bi|tableau|sap|salesforce|"
    r"hubspot|datev|snowflake|databricks|kubernetes|k8s|docker|terraform|helm|jira|"
    r"confluence|excel|ms office|microsoft 365|servicenow|workday|oracle)\b",
    re.IGNORECASE,
)


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _split_unmet(items: list[UnmetRequirement], profile_text: str) -> tuple[list[str], list[str]]:
    """(unmet, tool_gaps). A tool, vendor or platform the profile does not list is a
    gap to mention, not an unmet requirement, unless the model quotes the profile
    line that rules it out and that quote really is in the profile."""
    profile = _squash(profile_text)
    unmet, tool_gaps = [], []
    for item in items:
        text = item.requirement.strip()
        if not text or len(text) > 80:
            continue
        is_tool = item.kind == "tool" or (
            item.kind in ("domain", "other") and _TOOL_NAMES.search(text)
        )
        evidence = _squash(item.profile_evidence)
        if is_tool and not (len(evidence) >= 10 and evidence in profile):
            tool_gaps.append(text)
        else:
            unmet.append(text)
    return unmet[:5], tool_gaps[:5]


def _apply_result(job: dict, result: JobScore, profile_text: str = "") -> None:
    score = max(0, min(10, result.score))
    unmet, tool_gaps = _split_unmet(result.unmet_requirements, profile_text)
    # Years are compared in code, so the same ad and profile give the same answer.
    required, has = result.required_years, result.candidate_years
    if (required and has is not None and 0 <= has < required <= 25
            and not any(item.kind == "years" for item in result.unmet_requirements)):
        unmet = (unmet + [f"{required}+ years asked for, profile shows {has}"])[:5]
    if unmet and not result.requirements_flexible and score > UNMET_REQUIREMENT_CAP:
        score = UNMET_REQUIREMENT_CAP
    job["relevance_score"] = score * 10
    job["ai_assessment"] = result.reason
    job["situation_fit"] = result.situation_fit
    # Short tags only: Claude sometimes copies a whole profile sentence.
    job["ai_matched_skills"] = [sk for sk in result.skills if len(sk) <= 40][:3]
    job["ad_language"] = result.ad_language.strip()
    job["contact_person"] = result.contact_person.strip()[:200]
    job["seniority"] = result.seniority.strip()
    if result.required_years is not None and 0 < result.required_years <= 25:
        job["required_years"] = result.required_years
    if result.candidate_years is not None and 0 <= result.candidate_years <= 50:
        job["candidate_years"] = result.candidate_years
    job["hard_requirements"] = _short(result.hard_requirements, 5)
    job["requirements_flexible"] = result.requirements_flexible
    job["unmet_requirements"] = unmet
    job["tool_gaps"] = tool_gaps
    job["scoring_method"] = "claude"


def _ad_facts_fallback(job: dict) -> None:
    """Language and required years without an LLM, for rows Claude did not read."""
    from utils.job_ad import detect_language, required_years
    if not job.get("ad_language"):
        job["ad_language"] = detect_language(job.get("description", ""))
    if job.get("required_years") is None:
        years = required_years(job.get("description", ""))
        if years is not None:
            job["required_years"] = years


def _score_jobs_claude(jobs: list, profile_text: str) -> list:
    """Tier 2: score jobs against the fit profile in batches of 8. Jobs keep
    their Tier-1 score if a batch fails."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("[WARN] ANTHROPIC_API_KEY missing — Claude scoring skipped")
        for job in jobs:
            _attach_full_ad(job)
            job["scoring_method"] = "keyword_fallback"
        return jobs

    client = anthropic.Anthropic(api_key=api_key)
    system = _SCORING_SYSTEM.format(profile=profile_text)
    batch_size = 8

    for start in range(0, len(jobs), batch_size):
        batch = jobs[start: start + batch_size]

        # fetch full JDs and assign temp IDs
        batch_input = []
        id_map: dict[str, int] = {}
        for idx, job in enumerate(batch):
            jid = str(uuid.uuid4())[:8]
            _attach_full_ad(job)
            id_map[jid] = idx
            batch_input.append({
                "id": jid,
                "title": job.get("title", ""),
                "company": job.get("company", ""),
                "location": job.get("location", ""),
                "description_kind": "full ad" if job["fetch_method"] == "full_jd" else "snippet",
                "description": job.get("description", ""),
            })

        try:
            response = client.messages.parse(
                model=scoring_model(),
                max_tokens=16000,
                output_config={"effort": "medium"},
                system=system,
                messages=[{"role": "user", "content": json.dumps(batch_input, ensure_ascii=False)}],
                output_format=ScoreBatch,
            )
            parsed = response.parsed_output
            if parsed is None:
                raise ValueError(f"no parsed output (stop_reason={response.stop_reason})")
            for result in parsed.results:
                idx = id_map.get(result.id)
                if idx is None:
                    continue
                _apply_result(batch[idx], result, profile_text)
            print(f"[OK] Claude scoring: {len(parsed.results)} jobs scored")
        except Exception as e:
            print(f"[WARN] Claude batch scoring failed — keeping Tier-1 scores: {e}")
            for job in batch:
                job.setdefault("scoring_method", "keyword_fallback")

    return jobs


def score_jobs(
    jobs: list,
    features=None,
    role_terms: list[str] | None = None,
    profile_text: str | None = None,
) -> list:
    """Score jobs and return them sorted by relevance.

    Tier 1 is a cheap prefilter: does the posting mention a role term at all
    (AI, data, automation, digitalisation...)? Title hits count more than
    description hits. Feedback-loop deltas from your Notion decisions apply on
    top. Jobs that clear score_threshold() go to Tier 2, which scores them
    against the fit profile. Without a profile, Tier 1 scores are final.
    """
    from utils.profile import load_profile_text, load_search_config, matched_role_terms

    if role_terms is None:
        role_terms = load_search_config()["role_terms"]
    if profile_text is None:
        profile_text = load_profile_text()

    # Tier 1: role-term prefilter
    for job in jobs:
        title_hits = matched_role_terms(job.get("title", ""), role_terms)
        desc_hits = matched_role_terms(job.get("description", ""), role_terms)
        score = min(len(title_hits) * 40, 80) + min(len(desc_hits) * 10, 20)
        if title_hits and not job.get("description"):
            score = max(score, 30)
        job["relevance_score"] = min(score, 100)
        job["matched_keywords"] = sorted(set(title_hits + desc_hits))

    # Feedback deltas apply BEFORE the Tier-2 threshold check — a boost from a
    # company you applied to, or a penalty from a token you rejected 3x, should
    # decide whether the job is worth a Claude call.
    if features is not None and not features.is_empty():
        _apply_feedback_adjustments(jobs, features)
    for job in jobs:
        job["prefilter_score"] = job["relevance_score"]

    threshold = score_threshold()
    tier2_jobs = [j for j in jobs if j["relevance_score"] >= threshold]
    if tier2_jobs and profile_text:
        print(f"[INFO] Claude scoring for {len(tier2_jobs)} jobs (Tier-1 score >= {threshold})")
        _score_jobs_claude(tier2_jobs, profile_text)
    elif tier2_jobs:
        print("[WARN] No fit profile (FIT_PROFILE / profile/profile.md) — Tier-1 scores only")

    for job in jobs:
        job.setdefault("scoring_method", "keyword_fallback")
        job.setdefault("fetch_method", "snippet")
        job.setdefault("ai_assessment", "")
        job.setdefault("situation_fit", "")
        job.setdefault("ai_matched_skills", [])
        _ad_facts_fallback(job)

    return sorted(jobs, key=lambda j: j["relevance_score"], reverse=True)


if __name__ == "__main__":
    dummy_jobs = [
        {
            "title": "Data Analyst",
            "company": "Company A GmbH",
            "url": "https://example.com/1",
            "description": "Python SQL Power BI",
            "source": "linkedin",
        },
        {
            "title": "Data Analyst",
            "company": "Company A",
            "url": "https://example.com/2",
            "description": "Python SQL Power BI Tableau",
            "source": "google_jobs",
        },
        {
            "title": "Software Engineer",
            "company": "Company B AG",
            "url": "https://example.com/3",
            "description": "Java Spring Boot",
            "source": "linkedin",
        },
    ]
    cross_deduped = _deduplicate_cross_platform(dummy_jobs)
    deduped = deduplicate(cross_deduped)
    scored = score_jobs(deduped)
    for job in scored:
        print(f"[{job['relevance_score']}] {job['title']} @ {job['company']} ({job.get('scoring_method')})")
