"""Model-safety helpers: what may go INTO a prompt, and what may come out.

Two rules, both shared by every surface in this app that puts a model in
front of workforce data (app/insight_narrative.py's dashboard summary,
app/workforce_reports.py's report). One implementation each, because two
copies of a safety rule is how they drift, and the copy that drifts is the
one that stops holding.

Extracted from app/insight_narrative.py when the workforce report generator
needed exactly the same guarantee. One implementation, because two copies of
"which numbers is the model allowed to write" is how they drift, and the one
that drifts is the one that stops catching fabrications.

THE RULE, one way only: every numeral in the model's output must already
appear in the facts it was given. The model is free to OMIT figures (a
summary that mentions two of six findings is doing its job) and free to
write small counts as words. What it may not do is produce a numeral that is
not in the input — which is exactly what summing, averaging, estimating or
inventing looks like from outside the model.

A failed check is not a repair opportunity. The caller drops the generated
text entirely and falls back to deterministic prose; there is no partial
acceptance, because a sentence with one made-up number in it is a sentence
that cannot be trusted anywhere.
"""
from __future__ import annotations

import re
from typing import Protocol


class _Scope(Protocol):
    kind: str
    label: str


def neutral_scope_label(scope: _Scope) -> str:
    """A scope description with no person's name in it.

    app/analytics.py labels a manager's scope "<their full name>'s team",
    which is the right thing on screen and the wrong thing in a prompt: it
    puts an employee name into the model's context for no benefit, and it
    makes "the model never sees a person" an approximation rather than a
    fact. Caught by a test asserting exactly that.

    Org and org-unit labels are already impersonal — a company name, a
    department name — so they pass through unchanged. Only the team case is
    rewritten, and the model loses nothing: it never had a use for whose
    team it was, only for how big.
    """
    return "the team in scope" if scope.kind == "team" else scope.label




def numerals(text: str) -> set[str]:
    """Every number in a string, normalised so 20.0 and 20 compare equal.

    Trailing-zero normalisation matters because facts carry percentages as
    floats ("20.2%", "45.0%") and a model writing "45%" for a fact that says
    "45.0%" is quoting it correctly, not inventing one.
    """
    out: set[str] = set()
    for raw in re.findall(r"\d+(?:\.\d+)?", text):
        value = float(raw)
        out.add(str(int(value)) if value == int(value) else str(value))
    return out


def is_grounded(text: str, allowed: set[str]) -> bool:
    """Is every numeral in `text` present in `allowed`?"""
    return numerals(text).issubset(allowed)
