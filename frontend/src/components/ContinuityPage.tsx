import { useEffect, useState } from "react";
import {
  findPeople, getContinuityOverview, getEmployeeContinuity, getEngagementExposure, getHrReviewQueue,
  type ContinuityFilters, type HrReviewQueueFilters,
} from "../api";
import type {
  ContinuityOverview, EmployeeContinuityDetail, EngagementExposure, HrReviewQueueItem, Identity, PersonSummary,
  ViewMode,
} from "../types";

// HR-only. Only ever mounted when identity.role === "hr" — see App.tsx's
// tab gating, which is the entire non-HR-invisibility guarantee on this
// side of the wire: for any other role, this component never renders and
// its API calls never fire (the backend 403s them regardless).

type SubView = "overview" | "engagements" | "queue";

const SEVERITY_LABEL: Record<string, string> = { high: "High", medium: "Medium", low: "Low", none: "None" };

const AUTH_TYPE_LABEL: Record<string, string> = {
  citizen: "Citizen", permanent_resident: "Permanent Resident", cpt: "CPT", opt: "OPT",
  stem_opt: "STEM OPT", h1b: "H1B", l1: "L1", other: "Other",
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
                      <td>{d.project_backup_count > 0 ? `${d.project_backup_count} on this engagement` : "Single-person"}</td>
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

function EmployeeDrillDown({ detail }: { detail: EmployeeContinuityDetail }) {
  return (
    <div className="card">
      <div className="card-head">
        <h2>{detail.employee.full_name}</h2>
      </div>
      {detail.current_record ? (
        <div className="continuity-current-record">
          <p>
            <strong>Current category:</strong> {detail.current_record.authorization_type}
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

      {detail.history.length > 0 && (
        <>
          <p className="skill-label">History</p>
          <ul className="timeline">
            {detail.history.map((h) => (
              <li key={h.id} className={h.is_current ? "current" : undefined}>
                <p className="job">{h.authorization_type}</p>
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

  const [lookupQuery, setLookupQuery] = useState("");
  const [lookupResults, setLookupResults] = useState<PersonSummary[]>([]);
  const [selectedPerson, setSelectedPerson] = useState<EmployeeContinuityDetail | null>(null);

  useEffect(() => {
    if (subView !== "overview") return;
    let cancelled = false;
    setLoadingOverview(true);
    getContinuityOverview(identity, windowDays).then((o) => {
      if (!cancelled) {
        setOverview(o);
        setLoadingOverview(false);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [identity, subView, windowDays]);

  useEffect(() => {
    if (subView !== "engagements") return;
    let cancelled = false;
    setLoadingEngagements(true);
    getEngagementExposure(identity, filters).then((e) => {
      if (!cancelled) {
        setEngagements(e);
        setLoadingEngagements(false);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [identity, subView, filters]);

  useEffect(() => {
    if (subView !== "queue") return;
    let cancelled = false;
    setLoadingQueue(true);
    setSelectedPerson(null);
    getHrReviewQueue(identity, { ...queueFilters, window_days: queueWindowDays }).then((q) => {
      if (!cancelled) {
        setQueue(q);
        setLoadingQueue(false);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [identity, subView, queueWindowDays, queueFilters]);

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
                  <button onClick={() => getEmployeeContinuity(identity, p.id).then(setSelectedPerson)}>
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
                </tr>
              </thead>
              <tbody>
                {queue.map((item) => (
                  <tr
                    key={item.employee.id}
                    className="continuity-queue-row"
                    onClick={() => getEmployeeContinuity(identity, item.employee.id).then(setSelectedPerson)}
                  >
                    <td>{item.employee.full_name}</td>
                    <td>{item.current_record.authorization_type}</td>
                    <td>
                      {item.days_until_hr_review} day{item.days_until_hr_review === 1 ? "" : "s"}
                    </td>
                    <td>{item.engagements_affected}</td>
                    <td>
                      <SeverityBadge exposure={item.highest_exposure} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {selectedPerson && <EmployeeDrillDown detail={selectedPerson} />}
        </div>
      )}
    </div>
  );
}
