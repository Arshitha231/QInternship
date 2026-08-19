"""Derive a structured PeopleQuery from plain free text, using only values
that actually exist in this database.

Why this exists. "engineers in Austin" is a structured filter request
wearing plain English: no question mark, no described problem, and no
deterministic route, so app.unified_search's gate sends it down the direct
path -- where find_people's SQL fallback only ever matches literal
substrings of a NAME. Zero results, while 27 Austin engineers sit in the
table. The identical text with a "?" appended has always worked, which is
RC5 (ARCHITECTURE_2.md §2) resurfacing in a different spot: punctuation
still decides whether some queries get answered.

Why not just call the model on an empty result. Because three tests say
not to, deliberately -- "model must not be called" for ordinary free text
(tests/test_unified_search.py). Escalating every zero-result search to the
assistant is a real cost decision this codebase already made and rejected.
This module answers the same queries for no tokens and ~5ms instead.

The rule this follows, from ARCHITECTURE_2.md §3 decision 3: "Widening a
regex to catch a near-miss is the wrong fix." So there are no query-shape
patterns here at all -- nothing matches "<title> in <city>" or any other
sentence template. Every filter this produces comes from finding a real
office / org unit / skill / job-title word from THIS database inside the
text. A token that resolves to nothing produces nothing, and a text that
resolves to nothing at all returns None, which leaves the caller's flat
empty result exactly as it was. Same "return None rather than claim a
route you can't parse" contract the deterministic router already follows.

Scope note, which is what keeps this safe: app.unified_search only calls
this AFTER the direct path has already come back empty. It can therefore
never change the answer to a query that currently works -- only supply one
where there is currently nothing.
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Employee, Office, OrgUnit, Skill
from app.query_plan import Filter, PeopleQuery

# Words that appear in real job titles but carry no filtering signal --
# they'd match almost everyone ("team" is in 51 of 124 distinct titles) or
# are pure grammar. Kept deliberately short: this is a stoplist for noise,
# not a hand-tuned relevance list, and every word NOT here still has to
# occur in an actual job_title to match anything.
_TITLE_NOISE = {
    "and", "for", "the", "team", "with", "unit", "group", "department",
}

# A title word has to be this long to count. Filters out the "A"/"B"/"C"
# team suffixes and two-letter grammar without needing to enumerate them.
_MIN_TITLE_WORD = 4

_WORD = re.compile(r"[a-z]+")


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


def _contains_phrase(text_lower: str, phrase: str) -> bool:
    """Whole-phrase, word-boundary match. Substring alone would let the
    office "New York" match "New Yorker" and the skill "Go" match half the
    sentences in English.
    """
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase.lower())}(?![a-z0-9])", text_lower) is not None


def _match_longest(text_lower: str, values: list[str]) -> str | None:
    """The longest real value present in the text, or None.

    Longest-first matters: "Backend Team A" and "Backend Team" are both
    real org units, and the more specific one is the one the user named.
    """
    for value in sorted(values, key=len, reverse=True):
        if _contains_phrase(text_lower, value):
            return value
    return None


def _title_word(db: Session, tokens: set[str]) -> str | None:
    """A word that genuinely occurs in some employee's job_title.

    Built from the live column rather than a hardcoded list of professions,
    so a title the seed data grows tomorrow is matchable today, and a word
    that sounds like a job but isn't one here ("plumber") matches nothing
    instead of producing a confidently empty filter.
    """
    candidates = {
        form
        for token in tokens
        if len(token) >= _MIN_TITLE_WORD
        for form in _singular_forms(token)
        if len(form) >= _MIN_TITLE_WORD and form not in _TITLE_NOISE
    }
    if not candidates:
        return None

    title_words: set[str] = set()
    for (title,) in db.execute(select(Employee.job_title).where(Employee.job_title.is_not(None)).distinct()):
        title_words.update(_WORD.findall(title.lower()))

    matched = candidates & title_words
    if not matched:
        return None
    # Longest wins: "engineering" is a more specific ask than "engineer",
    # and both being present means the user typed the longer one.
    return max(matched, key=len)


def plan_from_text(db: Session, text: str, *, select_fields: list[str], limit: int) -> PeopleQuery | None:
    """A PeopleQuery built from whatever real vocabulary `text` names, or
    None when it names none.

    Returns a plan, not results -- app.people.search_people_by_plan runs it
    through the same validate -> snap -> enforce -> compile pipeline every
    other retrieval uses, so nothing here needs to know what a caller is or
    which fields they may see. This module is inert on its own, exactly
    like app/query_plan.py's docstring says a plan should be.
    """
    text_lower = text.lower().strip()
    if not text_lower:
        return None

    filters: list[Filter] = []

    # Office: city first -- "Austin" is how people refer to the office, and
    # "Austin Office" contains it, so matching the city covers both without
    # needing the formal name to appear.
    offices = [name for (name,) in db.execute(select(Office.city).where(Office.city.is_not(None)))]
    offices += [name for (name,) in db.execute(select(Office.name).where(Office.name.is_not(None)))]
    office = _match_longest(text_lower, offices)
    if office:
        filters.append(Filter(field="office", op="contains", value=office))

    unit = _match_longest(text_lower, [n for (n,) in db.execute(select(OrgUnit.name))])
    if unit:
        filters.append(Filter(field="org_unit", op="eq", value=unit))

    skill = _match_longest(text_lower, [n for (n,) in db.execute(select(Skill.name))])
    if skill:
        filters.append(Filter(field="skills", op="contains", value=skill))

    title = _title_word(db, _tokens(text_lower))
    if title:
        filters.append(Filter(field="job_title", op="contains", value=title))

    if not filters:
        return None
    return PeopleQuery(select=list(select_fields), filters=filters, limit=limit)
