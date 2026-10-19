"""Extract a per-pipeline feature index from a DecisionHistory.

The feature index is the input the scoring loop consumes to apply +/- deltas
based on your past Notion clicks. Indices are per pipeline and never mixed;
today there is one pipeline.
"""
from collections import Counter
from dataclasses import dataclass, field

from notion_sync.feedback import DecisionHistory
from utils.text_normalize import learnable_tokens, normalize_company

# Weights per design Premise 1 + score adjustment table.
# declined ("Declined") = 0.5 weight of a rejection — noisy signal (mixes
# "they rejected me" with "I bailed"), so half-weight.
_DECLINED_WEIGHT = 0.5


@dataclass
class FeatureIndex:
    pipeline: str
    positive_title_tokens: Counter = field(default_factory=Counter)
    negative_title_tokens: Counter = field(default_factory=Counter)
    positive_companies: set[str] = field(default_factory=set)
    negative_companies: Counter = field(default_factory=Counter)

    def is_empty(self) -> bool:
        return (
            not self.positive_title_tokens
            and not self.negative_title_tokens
            and not self.positive_companies
            and not self.negative_companies
        )

    def summary(self) -> dict:
        return {
            "positive_tokens": len(self.positive_title_tokens),
            "negative_tokens": len(self.negative_title_tokens),
            "positive_companies": len(self.positive_companies),
            "negative_companies": len(self.negative_companies),
        }


def build_feature_index(history: DecisionHistory, pipeline: str, vocabulary: frozenset | None = None) -> FeatureIndex:
    """Build a FeatureIndex for one pipeline.

    Filters the history to decisions whose pipeline matches, then aggregates:
      - positive title tokens: applied + interested
      - negative title tokens: rejected + (declined × 0.5)
      - positive companies: exact normalized matches from applied
      - negative companies: from rejected (count threshold applied at scoring time)

    Empty/cold-start safe: returns an empty FeatureIndex if no decisions match.
    """
    if vocabulary is None:
        from utils.profile import search_vocabulary
        vocabulary = search_vocabulary()

    def tokens(title: str) -> set:
        return learnable_tokens(title, vocabulary)

    fi = FeatureIndex(pipeline=pipeline)

    for d in history.applied:
        if d.pipeline != pipeline:
            continue
        fi.positive_title_tokens.update(tokens(d.title))
        company = normalize_company(d.company)
        if company:
            fi.positive_companies.add(company)

    for d in history.interested:
        if d.pipeline != pipeline:
            continue
        fi.positive_title_tokens.update(tokens(d.title))
        # interested is a weaker positive — we do NOT add to companies (you
        # didn't apply, just flagged). Title tokens are still signal.

    for d in history.rejected:
        if d.pipeline != pipeline:
            continue
        fi.negative_title_tokens.update(tokens(d.title))
        company = normalize_company(d.company)
        if company:
            fi.negative_companies[company] += 1

    # Declined weighted at 0.5 — done by adding once then halving the count.
    # We use a separate counter to track the partial contributions, then merge.
    declined_buf: Counter = Counter()
    for d in history.declined:
        if d.pipeline != pipeline:
            continue
        declined_buf.update(tokens(d.title))
    for tok, n in declined_buf.items():
        # Half-weight: round(n / 2), at least 1, so a single Declined still counts
        # (otherwise a token with just one Declined would be lost to int truncation).
        fi.negative_title_tokens[tok] += max(1, int(round(n * _DECLINED_WEIGHT)))

    return fi
