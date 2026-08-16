import { useEffect, useState } from "react";
import {
  acceptProposedChange, ApiError, bulkAcceptProposedChanges, bulkRejectProposedChanges,
  editProposedChange, findPeople, listDocSubjectMatches, listProposedChanges,
  reassignProposedChange, rejectProposedChange, resolveDocSubjectMatch, uploadDoc,
} from "../api";
import type {
  DocSubjectMatchOut, Identity, PersonSummary, ProposedChangeGroup, ProposedChangeOut,
  UploadDocResult, ViewMode,
} from "../types";

// AI-assisted doc upload, IT-only, work mode only — see App.tsx's tab
// gating, the entire non-IT-invisibility guarantee on this side of the
// wire: for any other role/mode this page never renders and its calls
// never fire (the backend 403s them regardless, per app/proposals.py's
// _authorize / _authorize_commit).
//
// Deliberately a single scrolling pipeline (upload -> resolve people ->
// review fields), not tabs like ContinuityPage's three independent views
// of the same data -- this workflow is genuinely sequential, and losing
// the "people to resolve" list from view while looking at "proposed
// changes" would hide exactly the thing that's blocking more of them from
// appearing.

const CHANGE_TYPE_LABEL: Record<string, string> = {
  skill: "Skill", contribution: "Contribution", project_entry: "Project entry",
};

const FIELD_LABEL: Record<string, string> = {
  skill: "Skill", project: "Project", contribution: "Contribution", role: "Role",
  start_date: "Start", end_date: "End", evidence: "Evidence", level: "Level", source: "Source",
};

const MATCH_REASON_LABEL: Record<string, string> = {
  email_match: "email match", name_exact: "exact name match", name_fuzzy: "similar name",
  department_match: "department match",
};

function humanizeMatchReason(reason: string): string {
  return reason.split("+").map((r) => MATCH_REASON_LABEL[r] ?? r).join(" + ");
}

function errorMessage(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.message : fallback;
}

// ---------------------------------------------------------------------------
// Reused in two places: a subject's "search for someone else" fallback (the
// ranked candidate list is not guaranteed to contain the right person), and
// a single proposed_change row's /reassign. Backed by the same findPeople
// every other search surface in this app uses.
// ---------------------------------------------------------------------------

function EmployeeSearchPicker({
  identity, viewMode, onSelect, placeholder,
}: { identity: Identity; viewMode: ViewMode; onSelect: (p: PersonSummary) => void; placeholder: string }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<PersonSummary[]>([]);

  useEffect(() => {
    if (!query.trim()) {
      setResults([]);
      return;
    }
    const controller = new AbortController();
    findPeople(identity, { name: query.trim() }, viewMode, controller.signal)
      .then(setResults)
      .catch(() => { /* aborted or transient — the input shows nothing found */ });
    return () => controller.abort();
  }, [identity, viewMode, query]);

  return (
    <div className="employee-picker">
      <input
        className="edit-input" placeholder={placeholder} value={query}
        onChange={(e) => setQuery(e.target.value)}
      />
      {results.length > 0 && (
        <ul className="employee-picker-results">
          {results.map((p) => (
            <li key={p.id}>
              <button
                onClick={() => {
                  onSelect(p);
                  setQuery("");
                  setResults([]);
                }}
              >
                {p.full_name}
                <span className="sub">{p.job_title}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// One doc_subject_matches row — the required-first-screen unit. Candidates
// are ALWAYS a ranked list to confirm from, never an assignment, even when
// there's exactly one — see app/doc_extraction.py's rank_candidates.
// ---------------------------------------------------------------------------

function SubjectCard({
  subject, identity, viewMode, onResolved,
}: { subject: DocSubjectMatchOut; identity: Identity; viewMode: ViewMode; onResolved: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showSearch, setShowSearch] = useState(false);

  async function confirm(employeeId: string) {
    setBusy(true);
    setError(null);
    try {
      await resolveDocSubjectMatch(identity, subject.id, { employee_id: employeeId }, viewMode);
      onResolved();
    } catch (e) {
      setError(errorMessage(e, "Couldn't resolve — try again."));
      setBusy(false);
    }
  }

  async function flagNewHire() {
    setBusy(true);
    setError(null);
    try {
      await resolveDocSubjectMatch(identity, subject.id, { new_hire: true }, viewMode);
      onResolved();
    } catch (e) {
      setError(errorMessage(e, "Couldn't flag — try again."));
      setBusy(false);
    }
  }

  const signalEntries = Object.entries(subject.extracted_signals);

  return (
    <div className="card review-subject-card">
      <div className="card-head">
        <h3>{subject.extracted_name}</h3>
        <span className="continuity-meta">Doc #{subject.source_doc_id}</span>
      </div>

      {signalEntries.length > 0 && (
        <div className="pills review-signal-pills">
          {signalEntries.map(([k, v]) => (
            <span key={k} className="pill">{k}: {v}</span>
          ))}
        </div>
      )}

      <p className="continuity-meta">
        {subject.proposed_change_count} proposed change{subject.proposed_change_count === 1 ? "" : "s"} waiting
      </p>

      {subject.candidates.length > 0 ? (
        <ul className="review-candidate-list">
          {subject.candidates.map((c) => (
            <li key={c.employee_id}>
              <div>
                <p className="job">{c.full_name}</p>
                <p className="job-meta">
                  {Math.round(c.confidence * 100)}% confidence · {humanizeMatchReason(c.match_reason)}
                </p>
              </div>
              <button className="btn btn-primary" disabled={busy} onClick={() => confirm(c.employee_id)}>
                Confirm
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="continuity-meta">No plausible match found in the directory.</p>
      )}

      {error && <p className="bio-error">{error}</p>}

      <div className="review-subject-actions">
        <button className="btn" disabled={busy} onClick={() => setShowSearch((v) => !v)}>
          {showSearch ? "Cancel search" : "Search for someone else"}
        </button>
        <button className="btn btn-danger-outline" disabled={busy} onClick={flagNewHire}>
          New hire — notify HR
        </button>
      </div>

      {showSearch && (
        <EmployeeSearchPicker
          identity={identity} viewMode={viewMode} onSelect={(p) => confirm(p.id)}
          placeholder="Search by name…"
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// A generic before/after renderer across all three change_types — the
// backend treats proposed_value/original_value as untyped JSON precisely so
// this doesn't need three hardcoded layouts. original_value absent means
// "new" (nothing on file for this field yet), not "unchanged".
// ---------------------------------------------------------------------------

function ChangeDiff({
  original, proposed,
}: { original: Record<string, unknown> | null; proposed: Record<string, unknown> }) {
  const keys = Array.from(new Set([...(original ? Object.keys(original) : []), ...Object.keys(proposed)]));
  return (
    <dl className="review-diff">
      {keys.map((key) => {
        const before = original?.[key];
        const after = proposed[key];
        const isNew = original === null || before === undefined;
        const changed = !isNew && String(before) !== String(after ?? "");
        return (
          <div key={key} className="review-diff-row">
            <dt>{FIELD_LABEL[key] ?? key}</dt>
            <dd>
              {changed && <span className="review-diff-before">{String(before)}</span>}
              <span className={changed || isNew ? "review-diff-after" : undefined}>
                {String(after ?? "—")}
              </span>
              {isNew && <span className="hr-badge">New</span>}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}

// ---------------------------------------------------------------------------
// One proposed_changes row: accept / edit / reassign / reject, plus the
// bulk-select checkbox. Only accept()/edit() ever commit anything real.
// ---------------------------------------------------------------------------

function ChangeRow({
  change, identity, viewMode, selected, onToggleSelect, onChanged,
}: {
  change: ProposedChangeOut; identity: Identity; viewMode: ViewMode;
  selected: boolean; onToggleSelect: () => void; onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [reassigning, setReassigning] = useState(false);
  const [draft, setDraft] = useState<Record<string, string>>({});

  function startEdit() {
    const initial: Record<string, string> = {};
    for (const [k, v] of Object.entries(change.proposed_value)) initial[k] = String(v ?? "");
    setDraft(initial);
    setError(null);
    setEditing(true);
  }

  async function run(action: () => Promise<unknown>, fallback: string) {
    setBusy(true);
    setError(null);
    try {
      await action();
      onChanged();
    } catch (e) {
      setError(errorMessage(e, fallback));
      setBusy(false);
    }
  }

  // Terminal statuses are read-only: accept()/edit()/reject() all 409 on a
  // row that isn't pending anymore, and re-showing live action buttons on
  // one would just be an invitation to hit that wall. This is the frontend
  // catching up with what the backend already enforces, not a new rule --
  // list_proposals returns every status, not just pending, precisely so a
  // reviewer can see what happened to a row after acting on it.
  if (change.status !== "pending") {
    return (
      <li className="review-change-row review-change-row-done">
        <span className={`pill review-status-pill review-status-${change.status}`}>
          {change.status === "accepted" ? "Accepted" : change.status === "edited" ? "Edited & committed" : "Rejected"}
        </span>
        <div className="review-change-body">
          <div className="review-change-head">
            <span className="pill review-type-pill">{CHANGE_TYPE_LABEL[change.change_type]}</span>
          </div>
          <ChangeDiff original={change.original_value} proposed={change.proposed_value} />
        </div>
      </li>
    );
  }

  return (
    <li className="review-change-row">
      <input
        type="checkbox" checked={selected} onChange={onToggleSelect} disabled={busy}
        aria-label={`Select ${CHANGE_TYPE_LABEL[change.change_type]} change`}
      />
      <div className="review-change-body">
        <div className="review-change-head">
          <span className="pill review-type-pill">{CHANGE_TYPE_LABEL[change.change_type]}</span>
          <span className="continuity-meta">{Math.round(change.confidence * 100)}% confidence</span>
        </div>

        {editing ? (
          <div className="bio-edit">
            {Object.entries(draft).map(([k, v]) => (
              <label key={k} className="edit-field">
                <span className="edit-label">{FIELD_LABEL[k] ?? k}</span>
                <input
                  className="edit-input" value={v}
                  onChange={(e) => setDraft((d) => ({ ...d, [k]: e.target.value }))}
                />
              </label>
            ))}
            {error && <p className="bio-error">{error}</p>}
            <div className="bio-actions">
              <button className="btn" disabled={busy} onClick={() => setEditing(false)}>Cancel</button>
              <button
                className="btn btn-primary" disabled={busy}
                onClick={() => run(async () => {
                  await editProposedChange(identity, change.id, draft, viewMode);
                  setEditing(false);
                }, "Couldn't save — try again.")}
              >
                {busy ? "Saving…" : "Save & commit"}
              </button>
            </div>
          </div>
        ) : (
          <>
            <ChangeDiff original={change.original_value} proposed={change.proposed_value} />
            {error && <p className="bio-error">{error}</p>}
            {reassigning ? (
              <EmployeeSearchPicker
                identity={identity} viewMode={viewMode} placeholder="Reassign to…"
                onSelect={(p) => run(async () => {
                  await reassignProposedChange(identity, change.id, p.id, viewMode);
                  setReassigning(false);
                }, "Couldn't reassign — try again.")}
              />
            ) : (
              <div className="review-change-actions">
                <button
                  className="btn btn-primary" disabled={busy}
                  onClick={() => run(() => acceptProposedChange(identity, change.id, viewMode), "Couldn't accept — try again.")}
                >
                  Accept
                </button>
                <button className="btn" disabled={busy} onClick={startEdit}>Edit</button>
                <button className="btn" disabled={busy} onClick={() => setReassigning(true)}>Reassign</button>
                <button
                  className="btn btn-danger-outline" disabled={busy}
                  onClick={() => run(() => rejectProposedChange(identity, change.id, viewMode), "Couldn't reject — try again.")}
                >
                  Reject
                </button>
              </div>
            )}
          </>
        )}
      </div>
    </li>
  );
}

export function ReviewPage({ identity, viewMode }: { identity: Identity; viewMode: ViewMode }) {
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [lastUpload, setLastUpload] = useState<UploadDocResult | null>(null);

  const [subjects, setSubjects] = useState<DocSubjectMatchOut[] | null>(null);
  const [loadingSubjects, setLoadingSubjects] = useState(false);
  const [groups, setGroups] = useState<ProposedChangeGroup[] | null>(null);
  const [loadingGroups, setLoadingGroups] = useState(false);

  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [refreshToken, setRefreshToken] = useState(0);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkError, setBulkError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoadingSubjects(true);
    listDocSubjectMatches(identity, viewMode).then((r) => {
      if (!cancelled) {
        setSubjects(r.subjects);
        setLoadingSubjects(false);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [identity, viewMode, refreshToken]);

  useEffect(() => {
    let cancelled = false;
    setLoadingGroups(true);
    listProposedChanges(identity, viewMode).then((r) => {
      if (!cancelled) {
        setGroups(r.groups);
        setLoadingGroups(false);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [identity, viewMode, refreshToken]);

  function refresh() {
    setSelected(new Set());
    setRefreshToken((t) => t + 1);
  }

  async function handleUpload(file: File) {
    setUploading(true);
    setUploadError(null);
    try {
      const result = await uploadDoc(identity, file, viewMode);
      setLastUpload(result);
      refresh();
    } catch (e) {
      setUploadError(errorMessage(e, "Upload failed — try again."));
    } finally {
      setUploading(false);
    }
  }

  function toggleSelect(id: number) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleGroupSelect(ids: number[]) {
    setSelected((prev) => {
      const next = new Set(prev);
      const allSelected = ids.every((id) => next.has(id));
      ids.forEach((id) => (allSelected ? next.delete(id) : next.add(id)));
      return next;
    });
  }

  async function bulkAccept() {
    setBulkBusy(true);
    setBulkError(null);
    try {
      await bulkAcceptProposedChanges(identity, viewMode, { ids: [...selected] });
      refresh();
    } catch (e) {
      setBulkError(errorMessage(e, "Bulk accept failed — try again."));
    } finally {
      setBulkBusy(false);
    }
  }

  async function bulkReject() {
    setBulkBusy(true);
    setBulkError(null);
    try {
      await bulkRejectProposedChanges(identity, viewMode, { ids: [...selected] });
      refresh();
    } catch (e) {
      setBulkError(errorMessage(e, "Bulk reject failed — try again."));
    } finally {
      setBulkBusy(false);
    }
  }

  const unresolvedSubjects = (subjects ?? []).filter((s) => s.resolution_status === "unresolved");
  const decidedSubjects = (subjects ?? []).filter((s) => s.resolution_status !== "unresolved");
  const hasAnyChanges = (groups ?? []).some((g) => g.changes.length > 0);

  return (
    <div className="review-page">
      <section className="card">
        <h2>Upload a document</h2>
        <p className="continuity-meta">
          A project status document or a resume (.docx or .pdf). Everything it proposes is staged for review
          here — nothing reaches a real profile until you accept or edit it.
        </p>
        <input
          type="file" accept=".docx,.pdf" disabled={uploading}
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) handleUpload(file);
            e.target.value = "";
          }}
        />
        {uploading && <p className="continuity-meta">Uploading and extracting…</p>}
        {uploadError && <p className="bio-error">{uploadError}</p>}
        {lastUpload && !uploading && (
          <p className="review-upload-summary">
            <strong>{lastUpload.filename}</strong> classified as{" "}
            {lastUpload.doc_type === "resume" ? "a resume" : "a project document"} —{" "}
            {lastUpload.people_mentioned} {lastUpload.people_mentioned === 1 ? "person" : "people"} mentioned,{" "}
            {lastUpload.proposed_changes} proposed change{lastUpload.proposed_changes === 1 ? "" : "s"} staged.
          </p>
        )}
      </section>

      <section className="card">
        <div className="card-head">
          <h2>People to resolve</h2>
          {subjects && <span className="continuity-meta">{unresolvedSubjects.length} unresolved</span>}
        </div>
        {loadingSubjects || subjects === null ? (
          <div className="skel skel-card" style={{ height: 100 }} />
        ) : unresolvedSubjects.length === 0 ? (
          <p className="continuity-meta">Nobody is waiting on resolution.</p>
        ) : (
          <div className="review-subject-list">
            {unresolvedSubjects.map((s) => (
              <SubjectCard key={s.id} subject={s} identity={identity} viewMode={viewMode} onResolved={refresh} />
            ))}
          </div>
        )}
        {decidedSubjects.length > 0 && (
          <details className="review-decided-subjects">
            <summary>{decidedSubjects.length} already decided</summary>
            <ul className="review-decided-list">
              {decidedSubjects.map((s) => (
                <li key={s.id}>
                  {s.extracted_name} —{" "}
                  {s.resolution_status === "new_hire_candidate" ? "flagged as new hire" : "resolved"}
                </li>
              ))}
            </ul>
          </details>
        )}
      </section>

      <section className="card">
        <div className="card-head">
          <h2>Proposed changes</h2>
          {hasAnyChanges && (
            <div className="review-bulk-actions">
              <button className="btn btn-primary" disabled={bulkBusy || selected.size === 0} onClick={bulkAccept}>
                Accept selected ({selected.size})
              </button>
              <button className="btn btn-danger-outline" disabled={bulkBusy || selected.size === 0} onClick={bulkReject}>
                Reject selected
              </button>
            </div>
          )}
        </div>
        {bulkError && <p className="bio-error">{bulkError}</p>}
        {loadingGroups || groups === null ? (
          <div className="skel skel-card" style={{ height: 140 }} />
        ) : groups.length === 0 ? (
          <p className="continuity-meta">Nothing is ready for review — resolve who a document is about first.</p>
        ) : (
          groups.map((g) => {
            const ids = g.changes.map((c) => c.id);
            return (
              <div key={g.employee_id} className="review-employee-group">
                <div className="card-head review-employee-head">
                  <h3>{g.employee_name ?? g.employee_id}</h3>
                  <button className="link-btn" onClick={() => toggleGroupSelect(ids)}>
                    Select all
                  </button>
                </div>
                <ul className="review-change-list">
                  {g.changes.map((c) => (
                    <ChangeRow
                      key={c.id} change={c} identity={identity} viewMode={viewMode}
                      selected={selected.has(c.id)} onToggleSelect={() => toggleSelect(c.id)}
                      onChanged={refresh}
                    />
                  ))}
                </ul>
              </div>
            );
          })
        )}
      </section>
    </div>
  );
}
