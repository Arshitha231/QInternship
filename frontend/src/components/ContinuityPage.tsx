import { Fragment, useEffect, useRef, useState } from "react";
import {
  acknowledgeHrReview, ApiError, confirmAuthorizationRecord, findPeople, getContinuityOverview,
  getEmployeeContinuity, getEngagementExposure, getHrReviewQueue, rejectAuthorizationRecord,
  submitAuthorizationRecord, type ContinuityFilters, type HrReviewQueueFilters,
  type SubmitAuthorizationRecordBody,
} from "../api";
import type {
  AuthorizationRecordOut, ContinuityOverview, EmployeeContinuityDetail, EngagementExposure, HrReviewQueueItem,
  Identity, PersonSummary, ViewMode,
} from "../types";

function errorMessage(e: unknown, fallback: string): string {
  return e instanceof ApiError ? e.message : fallback;
}

// HR-only. Only ever mounted when identity.role === "hr" — see App.tsx's
// tab gating, which is the entire non-HR-invisibility guarantee on this
// side of the wire: for any other role, this component never renders and
// its API calls never fire (the backend 403s them regardless).

type SubView = "overview" | "engagements" | "queue";

const SEVERITY_LABEL: Record<string, string> = { high: "High", medium: "Medium", low: "Low", none: "None" };

const AUTH_TYPE_LABEL: Record<string, string> = {
  citizen: "Citizen", permanent_resident: "Permanent Resident", cpt: "CPT", opt: "OPT",
  stem_opt: "STEM OPT", h1b: "H-1B", l1: "L-1", other: "Other",
};

function SeverityBadge({ exposure }: { exposure: string }) {
  return <span className={`pill continuity-pill-${exposure}`}>{SEVERITY_LABEL[exposure] ?? exposure}</span>;
}

function EngagementCard({ engagement }: { engagement: EngagementExposure }) {
  const [open, setOpen] = useState(false);
  const backupEntries = Object.entries(engagement.backups).filter(([, list]) => list.length > 0);

  return (
    <div className="card continuity-engagement-card">
      <div
        className="card-head continuity-engagement-head"
        role="button"
        tabIndex={0}
        onClick={() => setOpen((v) => !v)}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") setOpen((v) => !v);
        }}
      >
        <div>
          <h2>{engagement.project_name}</h2>
          <p className="continuity-meta">
            {engagement.exposure === "none"
              ? "No HR review currently intersects this engagement"
              : `Nearest review in ${engagement.days_until_hr_review} day${engagement.days_until_hr_review === 1 ? "" : "s"}` +
                (engagement.days_of_assignment_remaining_after_review !== null
                  ? ` · ${engagement.days_of_assignment_remaining_after_review} days of engagement remain after that`
                  : " · open-ended assignment")}
            {engagement.intersecting_review_count > 1 && ` · ${engagement.intersecting_review_count} people affected`}
          </p>
        </div>
        <SeverityBadge exposure={engagement.exposure} />
      </div>
      {open && (
        <div className="continuity-engagement-body">
          {engagement.reasons.length > 0 && (
            <ul className="continuity-reasons">
              {engagement.reasons.map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
          )}
          {engagement.dependencies.length > 0 && (
            <>
              <table className="continuity-table">
                <thead>
                  <tr>
                    <th>Capability / role</th>
                    <th>Who</th>
                    <th>Project redundancy</th>
                    <th>Org-wide backup</th>
                    <th>Basis</th>
                  </tr>
                </thead>
                <tbody>
                  {engagement.dependencies.map((d, i) => (
                    <tr key={i}>
                      <td>{d.name}</td>
                      <td>{d.employee.full_name}</td>
                      <td>
                        {d.redundancy_source === "project"
                          ? `${d.project_backup_count} on this engagement`
                          : d.redundancy_source === "org"
                            ? "Single-person — relies on redeployment"
                            : "Single-person — no backup identified"}
                      </td>
                      <td>{d.org_backup_count > 0 ? `${d.org_backup_count} identified` : "None identified"}</td>
                      <td>
                        <span
                          className={`pill continuity-source-${d.source}`}
                          title={
                            d.source === "declared"
                              ? "Recorded as a real project requirement or role"
                              : "No required-skill list recorded for this engagement — inferred from this person's skill profile, may overcount"
                          }
                        >
                          {d.source === "declared" ? "Declared" : "Inferred"}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {engagement.dependencies.some((d) => d.source === "inferred") && (
                <p className="continuity-inferred-note">
                  Some capabilities above are inferred from staff skill profiles, not a recorded project
                  requirement — they may overcount. Record required skills for this engagement for a precise
                  picture.
                </p>
              )}
            </>
          )}
          {backupEntries.length > 0 && (
            <div className="continuity-backups">
              <p className="skill-label">Potential internal capability matches</p>
              {backupEntries.map(([name, list]) => (
                <p key={name} className="continuity-backup-line">
                  <strong>{name}:</strong> {list.map((c) => c.full_name).join(", ")}
                </p>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function EngagementList({ engagements }: { engagements: EngagementExposure[] }) {
  return (
    <div className="continuity-engagement-list">
      {engagements.map((e) => (
        <EngagementCard key={e.project_id} engagement={e} />
      ))}
    </div>
  );
}

function recordLabel(r: AuthorizationRecordOut): string {
  return AUTH_TYPE_LABEL[r.authorization_type] ?? r.authorization_type;
}

// The three fields confirm actually changes what continuity computes from —
// same before/after shape as ReviewPage.tsx's ChangeDiff, scoped to this
// record type instead of a proposed_change's untyped JSON.
function AuthorizationRecordComparison({
  before, after, label,
}: { before: AuthorizationRecordOut | null; after: AuthorizationRecordOut; label: string }) {
  const rows: [string, string | null, string][] = [
    ["Category", before ? recordLabel(before) : null, recordLabel(after)],
    [
      "Effective",
      before ? `${before.effective_from}${before.effective_until ? ` – ${before.effective_until}` : " – open-ended"}` : null,
      `${after.effective_from}${after.effective_until ? ` – ${after.effective_until}` : " – open-ended"}`,
    ],
    [
      "Next HR review",
      before ? (before.next_hr_review_date ?? "None scheduled") : null,
      after.next_hr_review_date ?? "None scheduled",
    ],
  ];
  return (
    <div className="continuity-confirm-compare">
      <p className="skill-label">{label}</p>
      <dl className="review-diff">
        {rows.map(([field, from, to]) => (
          <div key={field} className="review-diff-row">
            <dt>{field}</dt>
            <dd>
              {from !== null && from !== to && <span className="review-diff-before">{from}</span>}
              <span className={from === null || from !== to ? "review-diff-after" : undefined}>{to}</span>
              {from === null && <span className="hr-badge">New</span>}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

// A pending_verification row: still just a submission until confirm makes it
// current. Confirm is deliberately two clicks, not one — the first opens a
// comparison of what's being superseded against what's replacing it, the
// second (Confirm supersede) is the only thing that actually calls the API.
// Reject stays a single click: it discards a submission, not the record
// continuity currently trusts.
function PendingRecordCard({
  record, currentRecord, identity, onChanged,
}: {
  record: AuthorizationRecordOut; currentRecord: AuthorizationRecordOut | null;
  identity: Identity; onChanged: () => void;
}) {
  const [reviewing, setReviewing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

  return (
    <li className="continuity-pending-card">
      <div className="continuity-pending-head">
        <span className="pill review-type-pill">{recordLabel(record)}</span>
        <span className="continuity-meta">
          {record.effective_from}
          {record.effective_until ? ` – ${record.effective_until}` : " – open-ended"}
        </span>
        <span className="pill review-status-pill review-status-pending">Pending verification</span>
      </div>

      {error && <p className="bio-error">{error}</p>}

      {reviewing ? (
        <>
          <AuthorizationRecordComparison
            before={currentRecord} after={record}
            label={currentRecord ? "Superseding the current record with:" : "Setting the first record on file:"}
          />
          <div className="bio-actions">
            <button className="btn" disabled={busy} onClick={() => setReviewing(false)}>Cancel</button>
            <button
              className="btn btn-primary" disabled={busy}
              onClick={() => run(() => confirmAuthorizationRecord(identity, record.id), "Couldn't confirm — try again.")}
            >
              {busy ? "Confirming…" : "Confirm supersede"}
            </button>
          </div>
        </>
      ) : (
        <div className="review-change-actions">
          <button className="btn btn-primary" disabled={busy} onClick={() => setReviewing(true)}>Confirm</button>
          <button
            className="btn btn-danger-outline" disabled={busy}
            onClick={() => run(() => rejectAuthorizationRecord(identity, record.id), "Couldn't reject — try again.")}
          >
            Reject
          </button>
        </div>
      )}
    </li>
  );
}

const NEW_RECORD_DEFAULTS: SubmitAuthorizationRecordBody = {
  authorization_type: "h1b", effective_from: "", effective_until: "", next_hr_review_date: "",
  source_document_type: "", internal_notes: "",
};

// Enters pending_verification only — see PendingRecordCard for the
// confirm/reject step that actually changes what continuity computes.
function SubmitRecordForm({
  employeeId, identity, onSubmitted,
}: { employeeId: string; identity: Identity; onSubmitted: () => void }) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<SubmitAuthorizationRecordBody>(NEW_RECORD_DEFAULTS);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!open) {
    return (
      <button className="btn" onClick={() => setOpen(true)}>+ Submit a new authorization record</button>
    );
  }

  function field<K extends keyof SubmitAuthorizationRecordBody>(key: K, value: string) {
    setDraft((d) => ({ ...d, [key]: value }));
  }

  async function submit() {
    if (!draft.effective_from) {
      setError("Effective date is required.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await submitAuthorizationRecord(identity, employeeId, {
        authorization_type: draft.authorization_type,
        effective_from: draft.effective_from,
        effective_until: draft.effective_until || null,
        next_hr_review_date: draft.next_hr_review_date || null,
        source_document_type: draft.source_document_type || null,
        internal_notes: draft.internal_notes || null,
      });
      setDraft(NEW_RECORD_DEFAULTS);
      setOpen(false);
      onSubmitted();
    } catch (e) {
      setError(errorMessage(e, "Couldn't submit — try again."));
      setBusy(false);
    }
  }

  return (
    <div className="bio-edit continuity-submit-form">
      <label className="edit-field">
        <span className="edit-label">Category</span>
        <select
          className="edit-input" value={draft.authorization_type}
          onChange={(e) => field("authorization_type", e.target.value)}
        >
          {Object.entries(AUTH_TYPE_LABEL).map(([value, label]) => (
            <option key={value} value={value}>{label}</option>
          ))}
        </select>
      </label>
      <label className="edit-field">
        <span className="edit-label">Effective from</span>
        <input
          className="edit-input" type="date" value={draft.effective_from ?? ""}
          onChange={(e) => field("effective_from", e.target.value)}
        />
      </label>
      <label className="edit-field">
        <span className="edit-label">Effective until (optional)</span>
        <input
          className="edit-input" type="date" value={draft.effective_until ?? ""}
          onChange={(e) => field("effective_until", e.target.value)}
        />
      </label>
      <label className="edit-field">
        <span className="edit-label">Next HR review (optional)</span>
        <input
          className="edit-input" type="date" value={draft.next_hr_review_date ?? ""}
          onChange={(e) => field("next_hr_review_date", e.target.value)}
        />
      </label>
      <label className="edit-field">
        <span className="edit-label">Source document (optional)</span>
        <input
          className="edit-input" value={draft.source_document_type ?? ""}
          onChange={(e) => field("source_document_type", e.target.value)}
          placeholder="e.g. I-797, EAD card"
        />
      </label>
      <label className="edit-field">
        <span className="edit-label">Internal notes (optional)</span>
        <input
          className="edit-input" value={draft.internal_notes ?? ""}
          onChange={(e) => field("internal_notes", e.target.value)}
        />
      </label>
      {error && <p className="bio-error">{error}</p>}
      <div className="bio-actions">
        <button className="btn" disabled={busy} onClick={() => { setOpen(false); setError(null); }}>Cancel</button>
        <button className="btn btn-primary" disabled={busy} onClick={submit}>
          {busy ? "Submitting…" : "Submit for review"}
        </button>
      </div>
    </div>
  );
}

function EmployeeDrillDown({
  detail, identity, onChanged,
}: { detail: EmployeeContinuityDetail; identity: Identity; onChanged: () => void }) {
  const pending = detail.history.filter((h) => h.verification_status === "pending_verification");
  // Current + superseded + rejected + expired — everything HR would call
  // "history": what's live now and what it replaced. Pending has its own
  // section above, with the actions that apply to it; showing it twice
  // (passively here too) would just be confusing.
  const resolved = detail.history.filter((h) => h.verification_status !== "pending_verification");

  return (
    <div className="card">
      <div className="card-head">
        <h2>{detail.employee.full_name}</h2>
      </div>
      {detail.current_record ? (
        <div className="continuity-current-record">
          <p>
            <strong>Current category:</strong> {recordLabel(detail.current_record)}
          </p>
          <p>
            <strong>Effective:</strong> {detail.current_record.effective_from}
            {detail.current_record.effective_until ? ` – ${detail.current_record.effective_until}` : ""}
          </p>
          <p>
            <strong>Next HR review:</strong> {detail.current_record.next_hr_review_date ?? "None scheduled"}
          </p>
          <p>
            <strong>Verification:</strong> {detail.current_record.verification_status}
          </p>
        </div>
      ) : (
        <p className="continuity-meta">No verified work-authorization record on file.</p>
      )}

      {/* Right after current-record context, always in the same place
          whether reached via a queue row or the name search — the only
          path to actually changing this data starts here, and burying it
          further down (e.g. after a variable-length pending list) is what
          makes a write path hard to find. */}
      <SubmitRecordForm employeeId={detail.employee.id} identity={identity} onSubmitted={onChanged} />

      {pending.length > 0 && (
        <>
          <p className="skill-label">Pending submissions</p>
          <ul className="continuity-pending-list">
            {pending.map((p) => (
              <PendingRecordCard
                key={p.id} record={p} currentRecord={detail.current_record}
                identity={identity} onChanged={onChanged}
              />
            ))}
          </ul>
        </>
      )}

      {resolved.length > 0 && (
        <>
          <p className="skill-label">History</p>
          <ul className="timeline">
            {resolved.map((h) => (
              <li key={h.id} className={h.is_current ? "current" : undefined}>
                <p className="job">{recordLabel(h)}</p>
                <p className="job-meta">
                  {h.effective_from}
                  {h.effective_until ? ` – ${h.effective_until}` : ""} · {h.verification_status}
                </p>
              </li>
            ))}
          </ul>
        </>
      )}

      <p className="skill-label">Client engagements</p>
      {detail.engagements.length === 0 ? (
        <p className="continuity-meta">No current client-engagement assignments.</p>
      ) : (
        <EngagementList engagements={detail.engagements} />
      )}
    </div>
  );
}

export function ContinuityPage({ identity, viewMode }: { identity: Identity; viewMode: ViewMode }) {
  const [subView, setSubView] = useState<SubView>("overview");

  const [windowDays, setWindowDays] = useState(90);
  const [overview, setOverview] = useState<ContinuityOverview | null>(null);
  const [loadingOverview, setLoadingOverview] = useState(false);

  const [filters, setFilters] = useState<ContinuityFilters>({});
  const [engagements, setEngagements] = useState<EngagementExposure[] | null>(null);
  const [loadingEngagements, setLoadingEngagements] = useState(false);

  const [queueWindowDays, setQueueWindowDays] = useState(90);
  const [queueFilters, setQueueFilters] = useState<HrReviewQueueFilters>({});
  const [queue, setQueue] = useState<HrReviewQueueItem[] | null>(null);
  const [loadingQueue, setLoadingQueue] = useState(false);
  const [ackBusyRecordId, setAckBusyRecordId] = useState<number | null>(null);
  const [ackError, setAckError] = useState<string | null>(null);

  const [lookupQuery, setLookupQuery] = useState("");
  const [lookupResults, setLookupResults] = useState<PersonSummary[]>([]);
  // The name-search path: reaches anyone, including someone with no due
  // review who'd never appear in the queue at all (a new hire with no
  // authorization record yet) — the drill-down it opens renders as its own
  // panel below the table, since there's no queue row to expand into.
  const [selectedPerson, setSelectedPerson] = useState<EmployeeContinuityDetail | null>(null);
  // The queue-row path: inline expansion, keyed by employee id so the
  // clicked row itself can carry the "expanded" styling. Kept separate from
  // selectedPerson rather than reusing one slot for both, since a search
  // result and a queue row are different pieces of UI that can't share a
  // home to expand into.
  const [expandedEmployeeId, setExpandedEmployeeId] = useState<string | null>(null);
  const [expandedDetail, setExpandedDetail] = useState<EmployeeContinuityDetail | null>(null);
  const [expandedLoading, setExpandedLoading] = useState(false);
  // Which employee id the in-flight fetch is actually for — a ref rather
  // than state because it has to be read synchronously inside a .then()
  // that may resolve after a second, faster click already moved
  // expandedEmployeeId on. Without this, clicking row A then quickly
  // clicking row B can let A's slower response land after B's and overwrite
  // B's detail with A's data.
  const expandedRequestId = useRef<string | null>(null);

  useEffect(() => {
    if (subView !== "overview") return;
    let cancelled = false;
    setLoadingOverview(true);
    getContinuityOverview(identity, viewMode, windowDays).then((o) => {
      if (!cancelled) {
        setOverview(o);
        setLoadingOverview(false);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [identity, viewMode, subView, windowDays]);

  useEffect(() => {
    if (subView !== "engagements") return;
    let cancelled = false;
    setLoadingEngagements(true);
    getEngagementExposure(identity, viewMode, filters).then((e) => {
      if (!cancelled) {
        setEngagements(e);
        setLoadingEngagements(false);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [identity, viewMode, subView, filters]);

  useEffect(() => {
    if (subView !== "queue") return;
    let cancelled = false;
    setLoadingQueue(true);
    setSelectedPerson(null);
    setExpandedEmployeeId(null);
    setExpandedDetail(null);
    expandedRequestId.current = null;
    getHrReviewQueue(identity, viewMode, { ...queueFilters, window_days: queueWindowDays }).then((q) => {
      if (!cancelled) {
        setQueue(q);
        setLoadingQueue(false);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [identity, viewMode, subView, queueWindowDays, queueFilters]);

  // Silences the reminder sweep for this record's due date — the row stays
  // in the queue afterward (still due, acknowledged or not), so this only
  // patches current_record in place rather than refetching or removing it.
  async function handleAcknowledge(recordId: number, e: React.MouseEvent) {
    e.stopPropagation();
    setAckBusyRecordId(recordId);
    setAckError(null);
    try {
      const updated = await acknowledgeHrReview(identity, recordId);
      setQueue((prev) =>
        prev?.map((item) =>
          item.current_record.id === recordId ? { ...item, current_record: updated } : item,
        ) ?? prev,
      );
    } catch (err) {
      setAckError(errorMessage(err, "Couldn't acknowledge this reminder — try again."));
    } finally {
      setAckBusyRecordId(null);
    }
  }

  // Submit/confirm/reject all change what the drill-down itself shows
  // (a new pending row, a record moving from pending to current/superseded)
  // — refetch that one person rather than patching fields in by hand the
  // way handleAcknowledge does, since a confirm changes two rows at once
  // (the newly-current one and whatever it superseded).
  function refreshSelectedPerson() {
    if (!selectedPerson) return;
    getEmployeeContinuity(identity, selectedPerson.employee.id, viewMode).then(setSelectedPerson);
  }

  // Click the same row again -> collapse. Click a different row -> the
  // previous one closes and this one opens (only one expanded at a time,
  // same reasoning EngagementCard would apply if it were list-wide instead
  // of per-card). Also collapses whatever the search box had open, so two
  // drill-downs are never visible at once.
  function toggleQueueRow(employeeId: string) {
    if (expandedEmployeeId === employeeId) {
      expandedRequestId.current = null;
      setExpandedEmployeeId(null);
      setExpandedDetail(null);
      return;
    }
    setSelectedPerson(null);
    setExpandedEmployeeId(employeeId);
    setExpandedDetail(null);
    setExpandedLoading(true);
    expandedRequestId.current = employeeId;
    getEmployeeContinuity(identity, employeeId, viewMode).then((d) => {
      if (expandedRequestId.current !== employeeId) return; // superseded by a later click
      setExpandedDetail(d);
      setExpandedLoading(false);
    });
  }

  function refreshExpandedRow() {
    if (!expandedEmployeeId) return;
    const employeeId = expandedEmployeeId;
    getEmployeeContinuity(identity, employeeId, viewMode).then((d) => {
      if (expandedRequestId.current !== employeeId) return;
      setExpandedDetail(d);
    });
  }

  useEffect(() => {
    if (!lookupQuery.trim()) {
      setLookupResults([]);
      return;
    }
    const controller = new AbortController();
    findPeople(identity, { name: lookupQuery.trim() }, viewMode, controller.signal)
      .then(setLookupResults)
      .catch(() => {
        /* aborted or transient — the input itself shows nothing found */
      });
    return () => controller.abort();
  }, [identity, viewMode, lookupQuery]);

  return (
    <div className="continuity-page">
      <div className="tabs" role="tablist" aria-label="Continuity view">
        {(["overview", "engagements", "queue"] as SubView[]).map((v) => (
          <button
            key={v}
            role="tab"
            aria-selected={subView === v}
            className={`tab ${subView === v ? "active" : ""}`}
            onClick={() => setSubView(v)}
          >
            {v === "overview" ? "Overview" : v === "engagements" ? "Engagements" : "HR Review Queue"}
          </button>
        ))}
      </div>

      {subView === "overview" && (
        <div className="continuity-section">
          <div className="continuity-window-control">
            <label htmlFor="cont-window">Lookahead window (days)</label>
            <input
              id="cont-window"
              type="number"
              min={1}
              value={windowDays}
              onChange={(e) => setWindowDays(Math.max(1, Number(e.target.value) || 1))}
            />
          </div>
          {loadingOverview || !overview ? (
            <div className="skel skel-card" style={{ height: 120 }} />
          ) : (
            <>
              <div className="continuity-summary-cards">
                {(["high", "medium", "low"] as const).map((sev) => (
                  <div key={sev} className={`card continuity-summary-card continuity-summary-${sev}`}>
                    <p className="continuity-summary-count">{overview.by_severity[sev] ?? 0}</p>
                    <p className="continuity-summary-label">{SEVERITY_LABEL[sev]} exposure</p>
                  </div>
                ))}
              </div>
              {overview.engagements.length === 0 ? (
                <div className="state-block">
                  <strong>No engagements need attention right now</strong>
                  <p>Nothing intersects an HR review within {overview.window_days} days.</p>
                </div>
              ) : (
                <EngagementList engagements={overview.engagements} />
              )}
            </>
          )}
        </div>
      )}

      {subView === "engagements" && (
        <div className="continuity-section">
          <div className="filter-bar">
            <select
              value={filters.exposure ?? ""}
              onChange={(e) => setFilters((f) => ({ ...f, exposure: e.target.value || undefined }))}
            >
              <option value="">Any exposure</option>
              <option value="high">High</option>
              <option value="medium">Medium</option>
              <option value="low">Low</option>
            </select>
            <input
              placeholder="Client or engagement name"
              value={filters.client ?? ""}
              onChange={(e) => setFilters((f) => ({ ...f, client: e.target.value || undefined }))}
            />
            <select
              value={filters.dependency_type ?? ""}
              onChange={(e) => setFilters((f) => ({ ...f, dependency_type: e.target.value || undefined }))}
            >
              <option value="">Any dependency type</option>
              <option value="skill">Skill</option>
              <option value="project_role">Project role</option>
            </select>
          </div>
          {loadingEngagements || engagements === null ? (
            <div className="skel skel-card" style={{ height: 120 }} />
          ) : engagements.length === 0 ? (
            <div className="state-block">
              <strong>No engagements match these filters</strong>
            </div>
          ) : (
            <EngagementList engagements={engagements} />
          )}
        </div>
      )}

      {subView === "queue" && (
        <div className="continuity-section">
          <div className="continuity-window-control">
            <label htmlFor="cont-queue-window">Lookahead window (days)</label>
            <input
              id="cont-queue-window"
              type="number"
              min={1}
              value={queueWindowDays}
              onChange={(e) => setQueueWindowDays(Math.max(1, Number(e.target.value) || 1))}
            />
          </div>

          <div className="filter-bar">
            <input
              placeholder="Search a name directly"
              value={lookupQuery}
              onChange={(e) => {
                setLookupQuery(e.target.value);
                setSelectedPerson(null);
              }}
            />
          </div>
          {lookupResults.length > 0 && !selectedPerson && (
            <ul className="reports-list">
              {lookupResults.map((p) => (
                <li key={p.id}>
                  <button
                    onClick={() => {
                      expandedRequestId.current = null;
                      setExpandedEmployeeId(null);
                      setExpandedDetail(null);
                      getEmployeeContinuity(identity, p.id, viewMode).then(setSelectedPerson);
                    }}
                  >
                    {p.full_name}
                    <span className="sub">{p.job_title}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}

          <div className="filter-bar">
            <select
              value={queueFilters.authorization_type ?? ""}
              onChange={(e) => setQueueFilters((f) => ({ ...f, authorization_type: e.target.value || undefined }))}
            >
              <option value="">Any current record</option>
              {Object.entries(AUTH_TYPE_LABEL).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
            <select
              value={queueFilters.exposure ?? ""}
              onChange={(e) => setQueueFilters((f) => ({ ...f, exposure: e.target.value || undefined }))}
            >
              <option value="">Any exposure</option>
              <option value="high">High</option>
              <option value="medium">Medium</option>
              <option value="low">Low</option>
              <option value="none">None</option>
            </select>
            <label className="continuity-window-control">
              Next review from
              <input
                type="date"
                value={queueFilters.next_review_from ?? ""}
                onChange={(e) => setQueueFilters((f) => ({ ...f, next_review_from: e.target.value || undefined }))}
              />
            </label>
            <label className="continuity-window-control">
              to
              <input
                type="date"
                value={queueFilters.next_review_to ?? ""}
                onChange={(e) => setQueueFilters((f) => ({ ...f, next_review_to: e.target.value || undefined }))}
              />
            </label>
            <input
              type="number"
              min={0}
              placeholder="Min engagements"
              value={queueFilters.engagements_min ?? ""}
              onChange={(e) =>
                setQueueFilters((f) => ({
                  ...f, engagements_min: e.target.value === "" ? undefined : Math.max(0, Number(e.target.value)),
                }))
              }
            />
            <input
              type="number"
              min={0}
              placeholder="Max engagements"
              value={queueFilters.engagements_max ?? ""}
              onChange={(e) =>
                setQueueFilters((f) => ({
                  ...f, engagements_max: e.target.value === "" ? undefined : Math.max(0, Number(e.target.value)),
                }))
              }
            />
          </div>

          {loadingQueue || queue === null ? (
            <div className="skel skel-card" style={{ height: 160 }} />
          ) : queue.length === 0 ? (
            <div className="state-block">
              <strong>Nobody is due for an HR review in this window</strong>
            </div>
          ) : (
            <table className="continuity-table continuity-queue-table">
              <thead>
                <tr>
                  <th>Employee</th>
                  <th>Current record</th>
                  <th>Next review</th>
                  <th>Engagements affected</th>
                  <th>Highest exposure</th>
                  <th>Reminder</th>
                </tr>
              </thead>
              <tbody>
                {queue.map((item) => {
                  const isExpanded = expandedEmployeeId === item.employee.id;
                  return (
                    <Fragment key={item.employee.id}>
                      <tr
                        className={`continuity-queue-row${isExpanded ? " continuity-queue-row-expanded" : ""}`}
                        role="button"
                        tabIndex={0}
                        aria-expanded={isExpanded}
                        onClick={() => toggleQueueRow(item.employee.id)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            toggleQueueRow(item.employee.id);
                          }
                        }}
                      >
                        <td>{item.employee.full_name}</td>
                        <td>{recordLabel(item.current_record)}</td>
                        <td>
                          {item.days_until_hr_review} day{item.days_until_hr_review === 1 ? "" : "s"}
                        </td>
                        <td>{item.engagements_affected}</td>
                        <td>
                          <SeverityBadge exposure={item.highest_exposure} />
                        </td>
                        <td>
                          {item.current_record.hr_review_acknowledged_at ? (
                            <span className="continuity-meta">Acknowledged</span>
                          ) : (
                            <button
                              type="button"
                              className="link-btn"
                              disabled={ackBusyRecordId === item.current_record.id}
                              onClick={(e) => handleAcknowledge(item.current_record.id, e)}
                              title="Stop the daily reminder for this due date — the review itself still needs to happen via a confirmed authorization record."
                            >
                              {ackBusyRecordId === item.current_record.id ? "Acknowledging…" : "Acknowledge reminder"}
                            </button>
                          )}
                        </td>
                      </tr>
                      {isExpanded && (
                        <tr className="continuity-queue-detail-row">
                          {/* One column per <th> above — table-layout: fixed on
                              .continuity-queue-table keeps those widths locked
                              regardless of how tall or wide this cell's content
                              gets (the confirm-diff panel included), so opening
                              it only pushes rows below down, never sideways. */}
                          <td colSpan={6}>
                            {expandedLoading || !expandedDetail ? (
                              <div className="skel skel-card" style={{ height: 160 }} />
                            ) : (
                              <EmployeeDrillDown
                                detail={expandedDetail} identity={identity} onChanged={refreshExpandedRow}
                              />
                            )}
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          )}
          {ackError && <p className="bio-error">{ackError}</p>}

          {selectedPerson && (
            <EmployeeDrillDown detail={selectedPerson} identity={identity} onChanged={refreshSelectedPerson} />
          )}
        </div>
      )}
    </div>
  );
}
