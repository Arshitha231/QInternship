"""Mode 3 — semantic project search, and the project->employee hop.

Answers "I'm stuck on X, who can help?" -- a question the other three modes
structurally cannot. Mode 1 (structured lookup) needs a field and a value;
Mode 2 (find_mentor) needs a named skill; Mode 4 (org traversal) needs a
person. A described *problem* has none of those. The evidence that someone
can help is usually "they worked on a project that hit this", not "this noun
appears on their profile", so the unit retrieved here is the PROJECT, and
people are reached by hopping from it.

Retrieval is hybrid, fused with reciprocal rank fusion:

    semantic   cosine over stored project embeddings (app/models/
               project_embedding.py) -- catches "our Kubernetes networking
               is flaky" -> a project whose description never says
               "Kubernetes networking" in those words.
    keyword    LIKE over project name + description -- catches the exact
               proper noun ("Nightingale", "Payroll") that embeddings blur.

Neither half alone is enough, which is the same reason Azure AI Search
does RRF for the employee corpus. Doing it here in ~15 lines is the whole
cost of not spending a second Search index on 128 documents.

PERMISSIONS. Two independent mechanisms, deliberately not one:

  1. Confidential projects are never embedded and never keyword-matched.
     The corpus does not contain them, so there is no query that can reach
     one -- the same structural exclusion app/search_index.py applies to
     the employee corpus, not a filter someone has to remember.
  2. Every person this module returns is still passed through
     is_record_visible, and every project through
     can_see_confidential_project, on the way out. Defence in depth: (1) is
     the guarantee, (2) is what catches a future bug in (1).

No model call happens in this module. The embedding call is index-time
(build_project_embeddings.py) plus one query embedding per search; the
ranking and the hop are ordinary code. Phrasing the final answer is
app/unified_search.py's job, from already-filtered results.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from array import array
from datetime import date, datetime

from sqlalchemy.orm import Session

from app.auth import AuthenticatedUser
from app.models import (
    AuditLog, Employee, EmployeeProject, OrgUnit, Project, ProjectEmbedding,
)
from app.models.enums import ProjectClassification
from app.permissions import ViewMode, can_see_confidential_project, is_record_visible
from app.schemas import ProblemExpert
from app.search_client import embed_texts

# How many projects the fused ranking keeps before the hop. Small on
# purpose: past the first handful the projects stop being about the same
# problem, and each one contributes its whole membership to the candidate
# pool (median 9 people, max 52 -- a loose cutoff here returns a crowd).
MAX_PROJECTS = 5

# How many people come back. Same spirit as people.py's MAX_SEARCH_RESULTS:
# this is a ranked list, not an enumerable filter result, so a tight cutoff
# is honest rather than lossy.
MAX_EXPERTS = 8

# RRF's smoothing constant. 60 is the value from the original Cormack et al.
# paper and what Azure AI Search itself uses for hybrid fusion -- kept the
# same so this behaves like the employee-side ranking it sits next to.
RRF_K = 60

# Roles that indicate someone drove the work rather than contributed to it.
# Matched case-insensitively against employee_projects.role, which is free
# text -- an unrecognised role isn't an error, it just doesn't get the lead
# bonus.
_LEAD_ROLES = {"owner", "lead", "tech lead", "principal", "architect", "manager"}


class EmbeddingUnavailable(RuntimeError):
    """Raised when the corpus has no usable vectors -- nothing embedded yet,
    or the query itself couldn't be embedded. Callers degrade to the
    keyword half rather than surfacing an error; see find_experts()."""


# --- Text and vector plumbing -------------------------------------------

def build_project_text(db: Session, project: Project) -> str:
    """The text that gets embedded for one project. Name and owning unit are
    included alongside the description so a query naming the team ("the
    infra team's logging work") has something to match on -- the description
    alone often never names its own owner."""
    unit = db.get(OrgUnit, project.owning_unit_id) if project.owning_unit_id else None
    lines = [f"{project.name} ({project.type.value})"]
    if unit:
        lines.append(f"Owned by {unit.name}.")
    if project.description:
        lines.append(project.description)
    return "\n".join(lines)


def source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0:
        return vector
    return [v / norm for v in vector]


def pack_vector(vector: list[float]) -> bytes:
    """Store L2-normalised, so query-time cosine is a plain dot product."""
    return array("f", _normalise(vector)).tobytes()


def unpack_vector(blob: bytes) -> list[float]:
    arr = array("f")
    arr.frombytes(blob)
    return list(arr)


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def embeddable_projects(db: Session) -> list[Project]:
    """Every project that may enter the corpus. Confidential ones are
    excluded HERE, at the single point that decides what gets embedded, so
    no downstream caller has to know about the rule."""
    return (
        db.query(Project)
        .filter(Project.classification != ProjectClassification.confidential)
        .order_by(Project.id)
        .all()
    )


def rebuild_embeddings(db: Session, *, force: bool = False) -> dict[str, int]:
    """(Re)embed the project corpus. Idempotent: a project whose text hashes
    to what's already stored is skipped unless force=True, so re-running
    after editing one description costs one embedding call, not 128.

    Also deletes rows for projects that have since become confidential or
    been removed -- otherwise reclassifying a project would leave it
    searchable forever, which is exactly the leak the corpus-level
    exclusion exists to prevent.

    Returns counts for the operator script to print.
    """
    from app.search_client import EMBEDDING_DEPLOYMENT

    projects = embeddable_projects(db)
    existing = {e.project_id: e for e in db.query(ProjectEmbedding).all()}

    allowed_ids = {p.id for p in projects}
    stale_ids = set(existing) - allowed_ids
    for pid in stale_ids:
        db.delete(existing[pid])

    pending: list[tuple[Project, str, str]] = []
    skipped = 0
    for project in projects:
        text = build_project_text(db, project)
        digest = source_hash(text)
        current = existing.get(project.id)
        if current is not None and current.source_hash == digest and not force:
            skipped += 1
            continue
        pending.append((project, text, digest))

    embedded = 0
    if pending:
        vectors = embed_texts([text for _p, text, _h in pending])
        if vectors is None:
            db.rollback()
            raise EmbeddingUnavailable(
                "the embedding endpoint is unconfigured or unreachable — no vectors were written"
            )
        for (project, _text, digest), vector in zip(pending, vectors):
            row = existing.get(project.id)
            if row is None:
                row = ProjectEmbedding(project_id=project.id)
                db.add(row)
            row.model = EMBEDDING_DEPLOYMENT
            row.dimensions = len(vector)
            row.source_hash = digest
            row.vector = pack_vector(vector)
            row.updated_at = datetime.now()
            embedded += 1

    db.commit()
    return {"embedded": embedded, "skipped": skipped, "removed": len(stale_ids), "total": len(projects)}


def reindex_project(db: Session, project_id: int) -> bool:
    """Re-embed exactly one project, for the write path (someone edits a
    description). Returns False when the project is confidential or gone --
    in which case any existing row is removed rather than left stale."""
    project = db.get(Project, project_id)
    row = db.get(ProjectEmbedding, project_id)
    if project is None or project.classification == ProjectClassification.confidential:
        if row is not None:
            db.delete(row)
            db.commit()
        return False

    from app.search_client import EMBEDDING_DEPLOYMENT

    text = build_project_text(db, project)
    digest = source_hash(text)
    if row is not None and row.source_hash == digest:
        return True  # unchanged, nothing to do

    vectors = embed_texts([text])
    if vectors is None:
        return False  # degrade: the old vector stays, staleness is visible via source_hash
    if row is None:
        row = ProjectEmbedding(project_id=project_id)
        db.add(row)
    row.model = EMBEDDING_DEPLOYMENT
    row.dimensions = len(vectors[0])
    row.source_hash = digest
    row.vector = pack_vector(vectors[0])
    row.updated_at = datetime.now()
    db.commit()
    return True


# --- Ranking -------------------------------------------------------------

def _semantic_ranking(db: Session, query_text: str) -> list[int]:
    """Project ids, most similar first. Empty when nothing is embedded or
    the query can't be embedded -- the caller falls back to keyword only."""
    rows = db.query(ProjectEmbedding).all()
    if not rows:
        return []

    # Cosine across vectors from different models is meaningless. Rather
    # than silently ranking on it, drop to the largest single-model subset.
    models = {r.model for r in rows}
    if len(models) > 1:
        dominant = max(models, key=lambda m: sum(1 for r in rows if r.model == m))
        rows = [r for r in rows if r.model == dominant]

    query_vectors = embed_texts([query_text])
    if query_vectors is None:
        return []
    query_vector = _normalise(query_vectors[0])

    dims = rows[0].dimensions
    scored: list[tuple[float, int]] = []
    for row in rows:
        if row.dimensions != dims:
            continue
        scored.append((_dot(query_vector, unpack_vector(row.vector)), row.project_id))
    scored.sort(key=lambda s: -s[0])
    return [pid for _score, pid in scored]


# Function words only. Deliberately NOT a length cutoff, which is what this
# replaced: "SSO", "MFA", "AWS", "SQL", "ETL", "CI" are all three characters
# or fewer and are exactly the distinctive terms this arm exists to catch.
# Measured: with a >3-character filter, "nobody can log in, SSO and MFA keep
# failing" tokenised to ["nobody", "keep", "failing"] and ranked Global
# Mobility Policy first, dragging the semantically-correct answer (IT
# Operations Tooling Upgrade, which the vector arm ranked #1) down through
# RRF.
_STOPWORDS = frozenset({
    "a", "an", "and", "any", "are", "as", "at", "be", "been", "but", "by", "can", "cant",
    "for", "from", "had", "has", "have", "how", "i", "if", "in", "into", "is", "it", "its",
    "me", "my", "no", "not", "of", "on", "or", "our", "out", "over", "so", "some", "that",
    "the", "their", "them", "then", "there", "this", "to", "up", "us", "was", "we", "were",
    "what", "when", "which", "who", "why", "with", "you", "your",
})

# A query term matching most of the corpus carries no ranking signal --
# "failing", "team", "process" appear in a large share of descriptions and
# only add noise. Terms above this document frequency are dropped from the
# keyword arm (the vector arm still sees the full query). 0.25 keeps
# genuinely distinctive vocabulary (kubernetes, sso, etl, latency) and drops
# the connective tissue.
MAX_DOCUMENT_FREQUENCY = 0.25


def _words(text: str) -> list[str]:
    """Query tokens. Keeps +/#/. so "C++", "C#" and "Node.js" survive
    tokenisation instead of splintering into meaningless fragments."""
    return [w for w in re.split(r"[^a-z0-9+#.]+", text.lower()) if w]


def _keyword_terms(db: Session, query_text: str) -> list[str]:
    """The distinctive query words the keyword arm actually ranks on --
    factored out of _keyword_ranking() so the relevance-excerpt picker
    (below) uses this exact filtering, not a second copy that could drift
    from what the ranking itself did.
    """
    candidates = [w for w in dict.fromkeys(_words(query_text)) if w not in _STOPWORDS and len(w) > 1]
    if not candidates:
        return []

    projects = embeddable_projects(db)
    if not projects:
        return []

    haystacks = {p.id: f"{p.name} {p.description or ''}".lower() for p in projects}
    ceiling = max(1, int(len(projects) * MAX_DOCUMENT_FREQUENCY))
    return [w for w in candidates if sum(1 for hay in haystacks.values() if w in hay) <= ceiling]


def _keyword_ranking(db: Session, query_text: str) -> list[int]:
    """Project ids whose name or description contains distinctive words from
    the query, ranked by how many distinct words hit.

    Substring matching in Python rather than SQL LIKE-per-word: 126 rows is
    one cheap scan, and it behaves identically on SQLite and Azure SQL with
    no dialect surface at all.

    Precision-oriented on purpose. This arm's job is to catch the exact
    proper noun or acronym an embedding blurs; a ranking built out of words
    that appear everywhere is worse than no ranking at all, because RRF then
    fuses real signal with noise.
    """
    words = _keyword_terms(db, query_text)
    if not words:
        return []

    projects = embeddable_projects(db)
    if not projects:
        return []

    haystacks = {p.id: f"{p.name} {p.description or ''}".lower() for p in projects}
    scored: list[tuple[int, int]] = []
    for project in projects:
        hits = sum(1 for w in words if w in haystacks[project.id])
        if hits:
            scored.append((hits, project.id))
    scored.sort(key=lambda s: -s[0])
    return [pid for _hits, pid in scored]


def _rrf(rankings: list[list[int]]) -> list[int]:
    """Reciprocal rank fusion: score = sum over rankings of 1/(k + rank).
    Rank-based rather than score-based on purpose -- a cosine similarity and
    a keyword hit count are not on comparable scales, and RRF needs only the
    ordering from each."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, pid in enumerate(ranking, start=1):
            scores[pid] = scores.get(pid, 0.0) + 1.0 / (RRF_K + rank)
    return [pid for pid, _s in sorted(scores.items(), key=lambda kv: -kv[1])]


def rank_projects(db: Session, query_text: str) -> tuple[list[int], str]:
    """Fused project ranking plus which retrieval arms actually ran, so the
    caller can say so honestly instead of implying a semantic match when
    only the keyword half was available."""
    semantic = _semantic_ranking(db, query_text)
    keyword = _keyword_ranking(db, query_text)

    if semantic and keyword:
        mode = "semantic+keyword"
    elif semantic:
        mode = "semantic"
    elif keyword:
        mode = "keyword"
    else:
        return [], "none"

    rankings = [r for r in (semantic, keyword) if r]
    return _rrf(rankings)[:MAX_PROJECTS], mode


# --- The project -> employee hop ------------------------------------------

def _assignment_rank(assignment: EmployeeProject, today: date, is_owner: bool) -> tuple[int, int, int, int]:
    """Sort key for one person's link to one matched project, best first.

    Ordering, in priority order: currently assigned beats finished; the
    project's real owner (Project.owner_id) beats everyone else; a role
    matching _LEAD_ROLES beats an ordinary contributor; more recent
    involvement beats older. A person who finished a two-week stint three
    years ago is real evidence but weak evidence, and shouldn't outrank
    the current tech lead just because their project scored marginally
    higher.

    owner_id is a real FK, checked ahead of _LEAD_ROLES rather than folded
    into it: a genuine owner whose free-text role happens to read "Backend
    Engineer" (never in _LEAD_ROLES) must still outrank a non-owner whose
    role string happens to say "Tech Lead" — the structural field is the
    primary signal, the free-text match is a fallback for everyone else,
    same "structural field over free-text" reasoning already applied to
    confidential-project membership.
    """
    current = 0 if assignment.end_date is None else 1
    owner_tier = 0 if is_owner else 1
    role = (assignment.role or "").strip().lower()
    lead = 0 if role in _LEAD_ROLES else 1
    if assignment.end_date is None:
        recency = 0
    else:
        recency = max(0, (today - assignment.end_date).days)
    return (current, owner_tier, lead, recency)


def _reason(project: Project, assignment: EmployeeProject) -> str:
    """Factual, mechanical, and never speculative about the person -- same
    discipline as app/continuity.py's _dependency_reasons. It says what the
    record shows, not that they are the best person to ask."""
    tense = "works on" if assignment.end_date is None else "worked on"
    role = (assignment.role or "").strip() or "a team member"
    return f"{tense} {project.name} as {role}"


# --- Relevance excerpt: which part of the description actually matched ----
#
# _reason() above says what a PERSON's record shows. This says why the
# PROJECT matched the described problem -- and it's a selection, not a
# generation: the sentence returned always comes verbatim from that
# project's own `description` column. Nothing here can put words in
# anyone's mouth, because nothing here writes words; it only picks which
# existing ones to surface. The embedding call this uses (when available)
# is the same category of call _semantic_ranking() already makes -- a
# similarity score choosing among real options, not a completion producing
# new text -- so this doesn't cross the line the rest of this module was
# built to not cross (see the module docstring).

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


def _split_sentences(text: str) -> list[str]:
    """A plain regex splitter, not a sentence-boundary library -- these are
    2-4 sentence project descriptions, not arbitrary prose, so the
    occasional false split on an abbreviation costs nothing worth adding a
    dependency to avoid."""
    text = (text or "").strip()
    if not text:
        return []
    return [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]


def _best_keyword_sentence(sentences: list[str], words: list[str]) -> str | None:
    """The sentence containing the most surviving keyword-arm terms --
    reuses _keyword_terms()'s own filtering, never a separate word list."""
    scored = [(sum(1 for w in words if w in s.lower()), s) for s in sentences]
    scored = [(hits, s) for hits, s in scored if hits > 0]
    if not scored:
        return None
    scored.sort(key=lambda hs: -hs[0])
    return scored[0][1]


def _project_excerpts(db: Session, problem: str, projects: list[Project]) -> dict[int, str | None]:
    """One relevant sentence per project, chosen (not written) by whichever
    signal is available.

    Prefers the embedding-similarity pick when embeddings are configured:
    it survives paraphrase ("Fabric migration" matching a sentence that
    never uses those words), which the keyword pick structurally cannot.
    One batched call covers every candidate project's sentences plus the
    query itself -- not one call per project -- so a 5-project result
    costs the same single round trip a 1-project result does.

    Falls back to the keyword arm's own matched terms when embeddings are
    unconfigured or the call fails -- same degrade-not-fail contract as
    every other embedding use in this module -- so a keyword-only search
    still gets an excerpt instead of a silently blank field.
    """
    sentences_by_project = {p.id: _split_sentences(p.description or "") for p in projects}
    excerpts: dict[int, str | None] = {p.id: None for p in projects}

    flat_sentences: list[str] = []
    owners: list[int] = []
    for pid, sentences in sentences_by_project.items():
        for s in sentences:
            flat_sentences.append(s)
            owners.append(pid)

    vectors = embed_texts([problem] + flat_sentences) if flat_sentences else None
    if vectors is not None:
        query_vector = _normalise(vectors[0])
        scored_by_project: dict[int, list[tuple[float, str]]] = {}
        for pid, sentence, vector in zip(owners, flat_sentences, vectors[1:]):
            scored_by_project.setdefault(pid, []).append((_dot(query_vector, _normalise(vector)), sentence))
        for pid, scored in scored_by_project.items():
            scored.sort(key=lambda vs: -vs[0])
            excerpts[pid] = scored[0][1]
        return excerpts

    words = _keyword_terms(db, problem)
    if words:
        for pid, sentences in sentences_by_project.items():
            excerpts[pid] = _best_keyword_sentence(sentences, words)
    return excerpts


def find_experts(
    db: Session, caller: AuthenticatedUser, problem: str, view_mode: ViewMode = "work",
) -> list[ProblemExpert]:
    """Mode 3's public entry point: a described problem in, ranked people out.

    Returns [] rather than raising when nothing is embedded and no keyword
    matches -- an empty, honest answer, same as any other tool here.
    """
    results: list[ProblemExpert] = []
    try:
        text = (problem or "").strip()
        if not text:
            return results

        project_ids, retrieval_mode = rank_projects(db, text)
        if not project_ids:
            return results

        today = date.today()

        # One relevant sentence per matched, visible project -- computed
        # once here (one batched embedding call for everything, when
        # available) rather than once per person, since the excerpt
        # explains the PROJECT's match, not any individual's involvement.
        visible_projects: list[Project] = []
        for project_id in project_ids:
            project = db.get(Project, project_id)
            if project is None:
                continue
            if (project.classification == ProjectClassification.confidential
                    and not can_see_confidential_project(db, caller, project.id)):
                continue
            visible_projects.append(project)
        excerpts = _project_excerpts(db, text, visible_projects)

        # Best (project, assignment) per person, keyed by employee id, held
        # as (project_position, assignment_rank, project, assignment).
        #
        # project_position comes FIRST, ahead of how strong the person's own
        # involvement is. The project IS the relevance signal here -- it's
        # what actually matched the described problem -- so a contributor on
        # the best-matching project is a better answer than a Lead on the
        # fifth-best one. Ordering these the other way round is what made
        # "our test suite is so flaky" return Data & AI Tooling Upgrade's
        # leads above the Quality Engineering test-suite project the query
        # had actually retrieved first.
        best: dict[str, tuple[int, tuple[int, int, int], Project, EmployeeProject]] = {}

        for position, project_id in enumerate(project_ids):
            project = db.get(Project, project_id)
            if project is None:
                continue
            # Defence in depth -- embeddable_projects() already excluded
            # confidential work from the corpus, so reaching this branch at
            # all would mean a bug upstream, not a normal path.
            if (project.classification == ProjectClassification.confidential
                    and not can_see_confidential_project(db, caller, project.id)):
                continue

            assignments = (
                db.query(EmployeeProject)
                .filter(EmployeeProject.project_id == project.id)
                .all()
            )
            for assignment in assignments:
                employee = db.get(Employee, assignment.employee_id)
                if employee is None or not employee.is_active:
                    continue
                if not is_record_visible(caller, employee, view_mode):
                    continue
                key = _assignment_rank(assignment, today, assignment.employee_id == project.owner_id)
                candidate = (position, key, project, assignment)
                current = best.get(employee.id)
                # Someone on several matched projects is shown against the
                # best-matching one, then their strongest involvement in it.
                if current is None or (position, key) < (current[0], current[1]):
                    best[employee.id] = candidate

        ordered = sorted(best.items(), key=lambda kv: (kv[1][0], kv[1][1], kv[1][2].id))
        for employee_id, (_position, _key, project, assignment) in ordered[:MAX_EXPERTS]:
            employee = db.get(Employee, employee_id)
            unit = db.get(OrgUnit, employee.org_unit_id) if employee.org_unit_id else None
            results.append(ProblemExpert(
                id=employee.id,
                full_name=employee.full_name,
                job_title=employee.job_title,
                org_unit=unit.name if unit else "",
                availability_status=employee.availability_status.value,
                project_id=project.id,
                project_name=project.name,
                role=assignment.role or "",
                current=assignment.end_date is None,
                reason=_reason(project, assignment),
                retrieval=retrieval_mode,
                excerpt=excerpts.get(project.id),
            ))
        return results
    finally:
        _write_audit(db, caller, problem, len(results))


def _write_audit(db: Session, caller: AuthenticatedUser, problem: str, result_count: int) -> None:
    db.add(AuditLog(
        actor_id=caller.id, action="find_experts", query_text=problem, result_count=result_count,
        fields_returned=json.dumps(sorted({
            "id", "full_name", "job_title", "org_unit", "availability_status",
            "project_id", "project_name", "role", "current", "reason",
        })),
        timestamp=datetime.now(),
    ))
    db.commit()
