"""Semantic-ish duplicate detection for enhancement proposals (overseer #33).

WHY THIS IS TF-IDF AND NOT EMBEDDINGS. The obvious build is an embedding index:
one vector per open issue, cosine against the proposal. It is also a new
dependency, a model download in a workflow that currently installs four small
packages, and — if the vectors come from an API — a per-run bill on a pipeline
whose entire review costs $0.34. The corpus here is ~70 issue titles. TF-IDF
cosine over that is stdlib arithmetic, runs in microseconds, and is testable
without a network. If the backlog ever reaches the thousands, swap the scorer;
the interface (`DuplicateIndex.query`) is the seam.

WHY IT EXISTS AT ALL. The ledger's `known_work_block` already puts the filed
backlog in the Idea Agent's prompt, and the pipeline still filed the same
schema-validation idea four times over seven weeks. A list in a prompt is a
request to remember; this is a check that runs. Same reasoning as the digest's
deterministic sections (invariant 7): anything that depends on an agent
remembering eventually goes quiet with nothing failing.

The index is built from the delivery ledger the orchestrator already fetches —
no extra API calls, and it refreshes every run because the ledger does.
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter

# Above this cosine, a proposal is treated as a RE-FILING and refused.
#
# MEASURED, not chosen. Issue #33 proposed 0.8, but a similarity threshold only
# means something against the scorer it was measured on. Scored every pair of the
# 90 issues this pipeline has filed against each other, title-weighted TF-IDF:
#
#   0.92  overseer #13 / #17 — the heartbeat idea, filed twice
#   0.76  overseer  #9 / #12 — the schema-validation idea, filed twice
#   0.61  overseer  #9 / #14 — the schema-validation idea, filed a THIRD time
#   0.51  the closest genuinely-distinct pair in the corpus
#
# So 0.75 catches both real re-filings with no false positive anywhere in the
# record, and 0.8 would have let #12 through — the exact incident CLAUDE.md
# names as the reason any of this exists. The third schema-validation filing
# lands in the advisory band below, where the agent is shown the match and left
# to judge, which is the right treatment for a rewrite that far from its
# original.
DUPLICATE_THRESHOLD = float(os.getenv("OVERSEER_DEDUPE_THRESHOLD", "0.75") or 0.75)

# Below this, matches aren't worth showing. Between the two the agent is told
# "this looks close, say what's new or pick another idea" and left to judge.
SIMILAR_THRESHOLD = float(os.getenv("OVERSEER_SIMILAR_THRESHOLD", "0.45") or 0.45)

# The rationale counts, but at a fraction of the title's weight. A proposal's
# title is the claim; its rationale is three sentences of shared vocabulary
# ("the pipeline", "the dashboard", "this would let us") that every proposal in
# the corpus also contains. Weighting them equally made two unrelated overseer
# ideas score 0.6 on boilerplate alone.
TITLE_WEIGHT = 3
RATIONALE_WEIGHT = 1

# Words that carry no signal in THIS corpus. Ordinary English stopwords plus the
# handful that appear in nearly every title the pipeline files — "add", "the
# overseer", "dashboard" — which is what pushed unrelated pairs into the similar
# band before they were dropped.
_STOPWORDS = frozenset("""
a an and are as at be by for from has have in into is it its of on or that the
this to with when which what while would could should can will not no be been
add adds added new use using support improve improved improvement enhancement
so than then there their they we you your it's per via over under across
""".split())

_WORD = re.compile(r"[a-z0-9_]+")


def tokenize(text) -> list[str]:
    """Words worth comparing: lowercased, stopped, and crudely singularised.

    The stemming is one rule — a trailing 's' comes off a word of five or more
    letters — because "proposals" and "proposal" are the same idea and a real
    stemmer is a dependency. It deliberately leaves "status" and "ci" alone.
    """
    out = []
    for word in _WORD.findall(str(text or "").lower()):
        if word in _STOPWORDS or len(word) < 3:
            continue
        if len(word) >= 6 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        if word not in _STOPWORDS:
            out.append(word)
    return out


def _weighted_tokens(title, rationale=None) -> Counter:
    counts = Counter()
    for token in tokenize(title):
        counts[token] += TITLE_WEIGHT
    for token in tokenize(rationale):
        counts[token] += RATIONALE_WEIGHT
    return counts


def _cosine(a: dict, b: dict) -> float:
    """Cosine of two already-L2-normalised sparse vectors."""
    if len(a) > len(b):
        a, b = b, a
    return sum(weight * b.get(term, 0.0) for term, weight in a.items())


class DuplicateIndex:
    """A TF-IDF index over issue titles, queried by a candidate proposal.

    Documents are dicts carrying at least `repo`, `number`, `title` and
    `status`. Everything else on them is passed back untouched on a match, so a
    caller can show state without a second lookup.
    """

    def __init__(self, documents=None):
        self.documents = list(documents or [])
        self._df = Counter()
        self._vectors = []
        for doc in self.documents:
            counts = _weighted_tokens(doc.get("title"), doc.get("rationale"))
            self._vectors.append(counts)
            self._df.update(counts.keys())
        self._n = len(self.documents)
        self._normalised = [self._normalise(c) for c in self._vectors]

    def __len__(self):
        return self._n

    def _idf(self, term) -> float:
        # Smoothed, and never negative: a term in every document contributes
        # 1.0 rather than 0, so an index of two near-identical issues still
        # scores them as near-identical instead of collapsing to zero.
        return math.log((self._n + 1) / (self._df.get(term, 0) + 1)) + 1.0

    def _normalise(self, counts) -> dict:
        vec = {term: (1 + math.log(n)) * self._idf(term) for term, n in counts.items()}
        norm = math.sqrt(sum(w * w for w in vec.values()))
        if not norm:
            return {}
        return {term: w / norm for term, w in vec.items()}

    def query(self, title, rationale=None, repo=None, limit=3) -> list[dict]:
        """The closest documents to this proposal, best first.

        `repo` scopes the comparison to one project when given. A coachvision
        idea and an overseer idea can share every word and still be different
        work, so scoring across repos would manufacture duplicates out of a
        shared vocabulary.
        """
        if not self._n:
            return []
        # TWO query vectors, and the score is the higher of the two.
        #
        # The documents are issue TITLES — the ledger records nothing else — so a
        # rationale in the query can only ever lengthen the query vector against
        # a document that has no rationale to match it. Blending it in unhelpfully
        # dropped the real "aging-backlog block" vs "aging backlog section"
        # re-filing from 0.77 to 0.66 purely because the caller was thorough.
        # Taking the max keeps the rationale useful — it can still surface a
        # proposal whose reasoning names an existing issue's title — while making
        # sure that explaining yourself can never help you past the gate.
        vectors = [v for v in (self._normalise(_weighted_tokens(title)),
                               self._normalise(_weighted_tokens(title, rationale)))
                   if v]
        if not vectors:
            return []
        scored = []
        for doc, doc_vector in zip(self.documents, self._normalised):
            if repo and doc.get("repo") != repo:
                continue
            score = max(_cosine(v, doc_vector) for v in vectors)
            if score < SIMILAR_THRESHOLD:
                continue
            match = dict(doc)
            match["score"] = round(score, 3)
            scored.append(match)
        scored.sort(key=lambda m: (-m["score"], m.get("number") or 0))
        return scored[:limit]


def verdict(score) -> str:
    """duplicate / similar / clear, from a single similarity score."""
    if score is None:
        return "clear"
    if score >= DUPLICATE_THRESHOLD:
        return "duplicate"
    if score >= SIMILAR_THRESHOLD:
        return "similar"
    return "clear"


# Outcomes worth comparing against. A proposal already SHIPPED is the most
# important thing to catch — re-proposing finished work is how the pipeline
# spent four weeks re-filing the same idea — and one closed as not-planned
# matters just as much: it was considered and declined, and filing it again
# re-asks a question that already has an answer.
INDEXED_STATUSES = ("open", "in_flight", "shipped", "duplicate", "not_planned")


def index_from_ledger(ledger, statuses=INDEXED_STATUSES) -> DuplicateIndex:
    """Build the index from the delivery ledger the run already fetched.

    Bugs are indexed alongside enhancements on purpose: "the ledger refresh
    races itself" filed as a bug and proposed as an enhancement are the same
    work arriving twice, and only the wording differs.
    """
    docs = []
    for entry in (ledger or {}).get("entries", []):
        if entry.get("status") not in statuses:
            continue
        docs.append({
            "repo": entry.get("repo"),
            "number": entry.get("number"),
            "title": entry.get("title"),
            "kind": entry.get("kind"),
            "status": entry.get("status"),
            "url": entry.get("url"),
        })
    return DuplicateIndex(docs)
