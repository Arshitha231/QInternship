"""Types a free-text query into typed entities -- role, seniority, skill,
office, org_unit -- using only vocabulary that actually exists in this
database. SEARCH_RANKING_PROPOSAL.md's fix for multi-part searches like
"senior data engineer with react, java" starts here: before anything can be
ranked or shown as a removable chip, the text has to be read as more than
one flat job_title-contains-word guess.

Why one candidate list across every label, not five separate per-field
passes. A per-field pass would ask "is there a role word in this text?"
independently of "is there a seniority word in this text?", and both could
answer yes on the same letters -- "Staff Engineer" is a real job title AND
"staff" is a real seniority band word, and "VP of Engineering" is a real
job title AND "VP" is a real seniority band word. Scanning fields
separately would double-book those letters as two different entities.
Building ONE candidate list over every label and claiming non-overlapping
spans longest-first, across the whole list rather than per field, is what
lets the specific real title win the words before the generic band word
gets a turn at what's already spoken for -- the same reasoning
text_filters._match_longest already used for offices/org-units, generalized
to compete across labels instead of only within one.

Person names are deliberately not a label here -- the exact-identifier
short-circuit in app.unified_search already resolves a name before
plan_from_text is ever reached. Language and availability aren't labels
either: text_filters.py never read them before this module existed, and
nothing about the multi-part-query bug requires them (flagged as a
Tier-1-ish idea per CLAUDE.md §10, not built).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Employee, Office, OrgUnit, Skill

Label = Literal["office", "org_unit", "skill", "role", "seniority"]

# Closed vocabulary, not derived from the DB -- there is no "seniority"
# column to read it from. Order is low-to-high for readability only; it
# carries no ranking weight here (that's app/people_ranking.py's job).
SENIORITY_BANDS: tuple[str, ...] = (
    "junior", "mid", "senior", "staff", "principal", "lead", "head",
    "director", "vp", "chief",
)

# Words that appear in real job titles but carry no filtering signal on
# their own -- see text_filters.py's original docstring for "team" (51 of
# 124 distinct titles). Only gates bare one-word title candidates; a real
# multi-word title fragment containing one of these is still specific.
_TITLE_NOISE = {
    "and", "for", "the", "team", "with", "unit", "group", "department",
}

# A bare title word, or a leftover "unparsed" word, has to be this long to
# count -- filters short grammar/suffix words without enumerating them.
_MIN_WORD = 4

_MAX_TITLE_NGRAM = 4

_WORD = re.compile(r"[a-z]+")

# Small connector stoplist for the "unparsed" report -- words a user types
# that carry no entity signal and shouldn't be surfaced as an unresolved
# term. Most are already too short for _MIN_WORD; kept explicit anyway so
# the rule reads as "these words never count," not "these happen to be
# short."
_UNPARSED_STOPLIST = _TITLE_NOISE | {"who", "knows", "with", "of", "a", "an"}


@dataclass(frozen=True)
class Entity:
    label: Label
    span: tuple[int, int]
    text: str
    value: str | None
    confidence: float


@dataclass(frozen=True)
class Interpretation:
    entities: list[Entity]
    unparsed: list[str]


@dataclass(frozen=True)
class _Candidate:
    value: str
    label: Label
    confidence: float
    # True only for a bare (one-word) job-title candidate -- the one place
    # this ever matched a plural in the text ("engineers" -> "Engineer").
    # Multi-word titles and every other label match the literal phrase only.
    plural_ok: bool = False


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _singular_forms(word: str) -> set[str]:
    """Both forms, so a vocabulary holding the singular ("Engineer") is
    still reachable from the plural the user typed ("engineers"). Not a
    stemmer -- two suffix rules, applied only to produce candidates that
    then still have to match a real database value to survive.
    """
    forms = {word}
    if word.endswith("ies") and len(word) > 4:
        forms.add(word[:-3] + "y")
    elif word.endswith("es") and len(word) > 3:
        forms.add(word[:-2])
    if word.endswith("s") and len(word) > 3:
        forms.add(word[:-1])
    return forms


def _phrase_spans(text_lower: str, value: str) -> list[tuple[int, int]]:
    """Every whole-phrase, word-boundary occurrence of value in text_lower.

    Substring alone would let the office "New York" match "New Yorker" and
    the skill "Go" match half the sentences in English.
    """
    pattern = re.compile(rf"(?<![a-z0-9]){re.escape(value.lower())}(?![a-z0-9])")
    return [m.span() for m in pattern.finditer(text_lower)]


def _plural_aware_spans(text_lower: str, value: str) -> list[tuple[int, int]]:
    """Every word in text_lower whose singular form is value -- the plural
    path for bare job-title candidates only (see _Candidate.plural_ok)."""
    value_lower = value.lower()
    return [
        m.span()
        for m in _WORD.finditer(text_lower)
        if value_lower in _singular_forms(m.group())
    ]


def _office_org_skill_candidates(db: Session) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    offices = [n for (n,) in db.execute(select(Office.city).where(Office.city.is_not(None)))]
    offices += [n for (n,) in db.execute(select(Office.name).where(Office.name.is_not(None)))]
    candidates += [_Candidate(value=v, label="office", confidence=1.0) for v in dict.fromkeys(offices)]

    units = [n for (n,) in db.execute(select(OrgUnit.name))]
    candidates += [_Candidate(value=v, label="org_unit", confidence=1.0) for v in dict.fromkeys(units)]

    skills = [n for (n,) in db.execute(select(Skill.name))]
    candidates += [_Candidate(value=v, label="skill", confidence=1.0) for v in dict.fromkeys(skills)]
    return candidates


def _job_title_ngrams(db: Session) -> dict[str, int]:
    """Every distinct 1-4 word contiguous slice of a real job_title, mapped
    to its word count -- value is the real-cased slice as the title spells
    it, so "Data Engineer" stays "Data Engineer" rather than the lowercased
    single word text_filters._title_word used to produce.
    """
    grams: dict[str, int] = {}
    titles = [t for (t,) in db.execute(select(Employee.job_title).where(Employee.job_title.is_not(None)).distinct())]
    for title in titles:
        words = title.split()
        for size in range(1, min(_MAX_TITLE_NGRAM, len(words)) + 1):
            for start in range(0, len(words) - size + 1):
                gram = " ".join(words[start:start + size])
                grams.setdefault(gram, size)
    return grams


def _role_candidates(db: Session) -> list[_Candidate]:
    candidates: list[_Candidate] = []
    for gram, size in _job_title_ngrams(db).items():
        if size == 1:
            word = gram.lower()
            if len(word) < _MIN_WORD or word in _TITLE_NOISE:
                continue
            candidates.append(_Candidate(value=gram, label="role", confidence=0.5, plural_ok=True))
        else:
            candidates.append(_Candidate(value=gram, label="role", confidence=1.0))
    return candidates


def _seniority_candidates() -> list[_Candidate]:
    return [_Candidate(value=band, label="seniority", confidence=1.0) for band in SENIORITY_BANDS]


def _candidates(db: Session) -> list[_Candidate]:
    return _office_org_skill_candidates(db) + _role_candidates(db) + _seniority_candidates()


def _first_unclaimed_span(
    text_lower: str, candidate: _Candidate, claimed: list[tuple[int, int]]
) -> tuple[int, int] | None:
    spans = _plural_aware_spans(text_lower, candidate.value) if candidate.plural_ok \
        else _phrase_spans(text_lower, candidate.value)
    for span in spans:
        if not any(span[0] < c_end and c_start < span[1] for c_start, c_end in claimed):
            return span
    return None


def _unparsed_words(text_lower: str, claimed: list[tuple[int, int]]) -> list[str]:
    words: list[str] = []
    for m in _WORD.finditer(text_lower):
        start, end = m.span()
        if any(start < c_end and c_start < end for c_start, c_end in claimed):
            continue
        word = m.group()
        if len(word) < _MIN_WORD or word in _UNPARSED_STOPLIST:
            continue
        words.append(word)
    return list(dict.fromkeys(words))


def parse(db: Session, text: str) -> Interpretation:
    """The Interpretation of `text` -- every entity a real vocabulary
    value supports, plus whatever's left over.

    Greedy, longest-value-first, across the whole candidate list (not
    per label) -- see the module docstring for why that ordering, not a
    per-field pass, is what makes "Staff Engineer" and "VP of Engineering"
    win their words before "staff"/"vp" get a turn at the same letters.

    Ties in length also need a tiebreak, not just a stable-sort accident.
    A bare one-word job-title candidate ("Senior", confidence 0.5, from
    any real title that happens to start with a seniority band word) is
    exactly as long as the seniority band word itself ("senior",
    confidence 1.0) -- and on any real directory where titles commonly
    start with "Senior"/"Staff"/"Lead", that tie comes up constantly.
    Sorting ties by confidence descending, not by insertion order, is what
    lets the certain reading (a closed, exact seniority band word) win
    over the merely-plausible one (a title n-gram this module only ever
    trusts at half confidence) instead of the two-role-entities,
    zero-seniority reading it would otherwise get.
    """
    text_lower = text.lower()
    claimed: list[tuple[int, int]] = []
    entities: list[Entity] = []
    for candidate in sorted(_candidates(db), key=lambda c: (len(c.value), c.confidence), reverse=True):
        span = _first_unclaimed_span(text_lower, candidate, claimed)
        if span is None:
            continue
        claimed.append(span)
        entities.append(Entity(
            label=candidate.label,
            span=span,
            text=text[span[0]:span[1]],
            value=candidate.value,
            confidence=candidate.confidence,
        ))
    entities.sort(key=lambda e: e.span[0])
    return Interpretation(entities=entities, unparsed=_unparsed_words(text_lower, claimed))
