"""Regenerates eval/golden_set.py's hardcoded UUIDs against the current
directory.db.

Why this is needed at all: seed.py assigns every employee a fresh
uuid.uuid4() on each run, which is NOT covered by seed.py's RNG_SEED (that
only seeds Python's `random`, not the OS-level UUID generator) -- so names,
teams, and skills regenerate identically across a reseed, but every id
changes. golden_set.py's ground truth hardcodes ids, so it goes stale on
every `python seed.py` run.

Every UUID in golden_set.py already carries its real name right next to
it, in one of three shapes:
  1. AuthenticatedUser(id="...", role="...", name="Real Name") -- the name
     is given directly.
  2. "uuid",  # Real Name -- an inline comment that IS the person's name
     (distinguished from a relationship-description comment like "Shaun
     Anderson's direct manager" by shape: just capitalized words, nothing
     else).
  3. REAL_NAME_CONSTANT = "uuid" -- the identifier itself, sometimes with a
     descriptive suffix (_RESTRICTED, _AWAY, _DELEGATE, _CEO) or a
     collision-disambiguating _1/_2 that isn't part of the name.

Conservative by design: anything this script can't confidently resolve to
exactly one current employee is left untouched and reported, never guessed
-- a wrong-but-plausible id would silently corrupt the eval's ground truth,
which is worse than leaving it visibly stale.

Rerun after any `python seed.py`:
    python scripts/regen_golden_uuids.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Employee  # noqa: E402
from app.org_chart import resolve_person_name  # noqa: E402

GOLDEN_SET_PATH = Path(__file__).resolve().parent.parent / "eval" / "golden_set.py"

UUID_RE = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

# Descriptive suffixes that show up in constant names but aren't part of
# the person's actual name.
_KNOWN_SUFFIXES = ("_RESTRICTED", "_AWAY", "_DELEGATE", "_CEO")


def _constant_name_to_person_name(identifier: str) -> str | None:
    """MICHELLE_DVORAK -> "Michelle Dvorak"; PRIYA_SHARMA_1 -> "Priya
    Sharma"; AIKO_SMITH_RESTRICTED -> "Aiko Smith". None for identifiers
    that aren't plausibly a single person's name (grouping constants like
    SEAN_WILSON_DIRECT_REPORTS are never matched by this function's caller
    in the first place -- see main()'s regex, which only matches bare
    `NAME = "uuid"` assignments, not set literals)."""
    name = identifier
    for suffix in _KNOWN_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    name = re.sub(r"_\d+$", "", name)  # trailing _1 / _2 disambiguation
    words = name.split("_")
    if not words or not all(w.isalpha() for w in words):
        return None
    return " ".join(w.capitalize() for w in words)


def _resolve_and_record(
    db, person_name: str, old_id: str, replacements: dict[str, str], unresolved: list[str],
) -> None:
    if old_id in replacements:
        return
    new_id = resolve_person_name(db, person_name)
    if new_id is None:
        unresolved.append(f'"{person_name}" ({old_id}) -- no unique current match')
        return
    replacements[old_id] = new_id


def main() -> None:
    text = GOLDEN_SET_PATH.read_text(encoding="utf-8")
    db = SessionLocal()
    replacements: dict[str, str] = {}
    unresolved: list[str] = []

    # Pass 1: AuthenticatedUser(id="...", role="...", name="Real Name")
    for m in re.finditer(rf'id="({UUID_RE})",\s*role="\w+",\s*name="([^"]+)"', text):
        _resolve_and_record(db, m.group(2), m.group(1), replacements, unresolved)

    # Pass 2: "uuid",  # Real Name[, trailing editorial note...] -- the name
    # is always the leading run of capitalized words right after `# `;
    # anything after it (", Tech Lead Backend", " -- missed earlier...")
    # is commentary, not part of the name, and is deliberately not anchored
    # to end-of-line so it doesn't have to be enumerated.
    for m in re.finditer(rf'"({UUID_RE})",\s*#\s*([A-Z][a-zA-Z\'-]+(?: [A-Z][a-zA-Z\'-]+)+)', text):
        _resolve_and_record(db, m.group(2).strip(), m.group(1), replacements, unresolved)

    # Pass 3: NAME_CONSTANT = "uuid", grouped by derived base name so a
    # genuine duplicate name (the seeded "Priya Sharma" pair) distributes
    # across its _1/_2 siblings instead of failing as "ambiguous" --
    # resolve_person_name refuses ambiguous names by design (org-chart
    # lookups need exactly one root), which is the right call there but
    # not what this script needs for a pair that's always used together.
    groups: dict[str, list[tuple[str, str]]] = {}
    for m in re.finditer(rf'^([A-Z][A-Z0-9_]*) = "({UUID_RE})"', text, re.MULTILINE):
        identifier, old_id = m.group(1), m.group(2)
        if old_id in replacements:
            continue
        base_name = _constant_name_to_person_name(identifier)
        if base_name is None:
            unresolved.append(f'{identifier} = "{old_id}" -- could not derive a name from the identifier')
            continue
        groups.setdefault(base_name, []).append((identifier, old_id))

    for base_name, members in groups.items():
        if len(members) == 1:
            _, old_id = members[0]
            _resolve_and_record(db, base_name, old_id, replacements, unresolved)
            continue
        matches = sorted(db.execute(
            select(Employee.id).where(Employee.full_name == base_name, Employee.is_active == True)
        ).scalars().all())
        members_sorted = sorted(members, key=lambda pair: pair[0])
        if len(matches) < len(members_sorted):
            unresolved.append(f'"{base_name}" -- {len(members_sorted)} constants but only {len(matches)} current matches')
            continue
        for (_identifier, old_id), new_id in zip(members_sorted, matches):
            replacements[old_id] = new_id

    if unresolved:
        print("UNRESOLVED -- left unchanged, review manually:")
        for line in unresolved:
            print(f"  {line}")
        print()

    new_text = text
    for old_id, new_id in replacements.items():
        new_text = new_text.replace(old_id, new_id)
    GOLDEN_SET_PATH.write_text(new_text, encoding="utf-8")

    print(f"Replaced {len(replacements)} id(s), {len(unresolved)} left unresolved.")
    db.close()


if __name__ == "__main__":
    main()
