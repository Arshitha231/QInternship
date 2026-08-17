import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { ApiError, getOrgChart, getPerson, updateEmployee, updateOwnBio, updateOwnNamePronunciation } from "../api";
import type { Identity, OrgChainNode, PersonDetail, SkillOut, UpdateEmployeeChanges, ViewMode } from "../types";
import {
  AlertCircle, Briefcase, Building, Cake, Check, ChevronLeft, Clock, GraduationCap, Mail,
  LinkIcon, MapPin, Phone, Slack, UserReports, Users, Volume,
} from "../icons";

export interface ProfileStackEntry {
  id: string;
  name: string;
}

interface Props {
  personId: string;
  identity: Identity;
  viewMode: ViewMode;
  stack: ProfileStackEntry[];
  onNavigate: (id: string, name: string) => void;
  onBack: () => void;
  onBreadcrumb: (index: number) => void;
  onBackToSearch?: () => void;
}

function initials(name: string): string {
  return name.split(" ").map((p) => p[0]).join("").slice(0, 2).toUpperCase();
}

// Speaks the phonetic respelling, not the name itself -- "nuh-VAY-uh" said
// aloud by TTS lands much closer to the real pronunciation than a screen
// reader guessing at "Navaya" would. No fallback UI for browsers without
// SpeechSynthesis (Web Speech API has near-universal support); the button
// simply no-ops if it's missing, same as any other progressive-enhancement
// affordance on this page.
function speakPronunciation(text: string) {
  if (!("speechSynthesis" in window)) return;
  window.speechSynthesis.cancel();
  window.speechSynthesis.speak(new SpeechSynthesisUtterance(text));
}

const LEVEL_ORDER = ["Expert", "Working", "Learning"] as const;
const LEVEL_CLASS: Record<string, string> = { Expert: "pill-expert", Working: "pill-working", Learning: "pill-learning" };

function groupByLevel(skills: SkillOut[]): [string, SkillOut[]][] {
  const groups = new Map<string, SkillOut[]>();
  for (const s of skills) {
    if (!groups.has(s.level)) groups.set(s.level, []);
    groups.get(s.level)!.push(s);
  }
  return LEVEL_ORDER.filter((l) => groups.has(l)).map((l) => [l, groups.get(l)!]);
}

// Split rather than `new Date(iso)`: "1991-08-13" parses as UTC midnight, and
// formatting that in any negative-offset timezone renders the day before.
function formatDate(iso: string): string {
  const [y, m, d] = iso.split("-").map(Number);
  if (!y || !m || !d) return iso;
  return new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric" })
    .format(new Date(y, m - 1, d));
}

function formatSalary(amount: string, currency?: string): string {
  const value = Number(amount);
  if (!Number.isFinite(value)) return amount;
  try {
    return new Intl.NumberFormat(undefined, {
      style: "currency", currency: currency || "USD", maximumFractionDigits: 0,
    }).format(value);
  } catch {
    // Unknown/absent currency code — show the number rather than nothing.
    return `${value.toLocaleString()}${currency ? ` ${currency}` : ""}`;
  }
}

function localTimeInfo(tz: string): string | null {
  try {
    const now = new Date();
    const time = new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit", hour12: true, timeZone: tz }).format(now);
    const hour = Number(new Intl.DateTimeFormat("en-US", { hour: "numeric", hour12: false, timeZone: tz }).format(now));
    const phase = hour < 8 ? "before working hours" : hour < 18 ? "working hours" : "after working hours";
    return `${time} local · ${phase}`;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// HR edit form (PATCH /employees/{id}, work mode, any profile but your own —
// see app/writes.py's update_employee for the actual enforcement, which this
// form's visibility only mirrors, never replaces). Every field here is a
// plain string in form state, including employment_type and the two date
// fields — HTML inputs only ever produce strings, and PersonDetail's own
// values already arrive as strings (salary in particular, deliberately, so
// the exact decimal from the backend is never round-tripped through a JS
// number — see PersonDetail's own comment on that field).
// ---------------------------------------------------------------------------

type EmployeeFormState = {
  full_name: string;
  preferred_name: string;
  job_title: string;
  work_email: string;
  work_phone: string;
  employment_type: string;
  salary: string;
  salary_currency: string;
  date_of_birth: string;
  hire_date: string;
  cost_centre: string;
  linkedin_profile: string;
};

// Fields where a touched-but-emptied input means "clear this" (sent as an
// explicit null) rather than "invalid" (see app/writes.py: "an explicit
// null clears the field... must stay distinguishable from omitting it
// entirely"). Everything NOT in this set is required — full_name, job_title,
// work_email, hire_date — and emptying one of those is caught by
// employeeFormValid before Save is ever clickable, so it never reaches the
// diff.
const NULLABLE_EMPLOYEE_FIELDS = new Set<keyof EmployeeFormState>([
  "preferred_name", "work_phone", "salary", "salary_currency", "date_of_birth", "cost_centre",
  "linkedin_profile",
]);

function employeeFormFromDetail(d: PersonDetail): EmployeeFormState {
  return {
    full_name: d.full_name ?? "",
    preferred_name: d.preferred_name ?? "",
    job_title: d.job_title ?? "",
    work_email: d.work_email ?? "",
    work_phone: d.work_phone ?? "",
    employment_type: d.employment_type ?? "fte",
    salary: d.salary ?? "",
    salary_currency: d.salary_currency ?? "",
    date_of_birth: d.date_of_birth ?? "",
    hire_date: d.hire_date ?? "",
    cost_centre: d.cost_centre ?? "",
    linkedin_profile: d.linkedin_profile ?? "",
  };
}

function employeeFormValid(f: EmployeeFormState): boolean {
  return f.full_name.trim() !== "" && f.job_title.trim() !== ""
    && f.work_email.trim() !== "" && f.hire_date.trim() !== "";
}

// Only fields that actually changed reach the server — the backend applies
// changes by key PRESENCE (model_dump(exclude_unset=True)), so an untouched
// field must be absent from the payload entirely, not just sent back
// unchanged. That's the whole reason this diffs against a separate
// `initial` snapshot instead of just serializing `current` wholesale.
function diffEmployeeForm(initial: EmployeeFormState, current: EmployeeFormState): UpdateEmployeeChanges {
  const changes: Record<string, string | null> = {};
  (Object.keys(current) as (keyof EmployeeFormState)[]).forEach((key) => {
    if (current[key] === initial[key]) return;
    const value = current[key].trim();
    if (value === "") {
      if (NULLABLE_EMPLOYEE_FIELDS.has(key)) changes[key] = null;
      return; // emptied a required field — employeeFormValid already blocked Save
    }
    changes[key] = value;
  });
  return changes as UpdateEmployeeChanges;
}

function EditField({ label, badge, children }: { label: string; badge?: string; children: ReactNode }) {
  return (
    <label className="edit-field">
      <span className="edit-label">
        {label}
        {badge && <span className="hr-badge">{badge}</span>}
      </span>
      {children}
    </label>
  );
}

export function ProfilePage({
  personId, identity, viewMode, stack, onNavigate, onBack, onBreadcrumb, onBackToSearch,
}: Props) {
  const [detail, setDetail] = useState<PersonDetail | null | undefined>(undefined);
  const [reports, setReports] = useState<OrgChainNode[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [editingBio, setEditingBio] = useState(false);
  const [bioDraft, setBioDraft] = useState("");
  const [savingBio, setSavingBio] = useState(false);
  const [bioError, setBioError] = useState<string | null>(null);

  const [editingPronunciation, setEditingPronunciation] = useState(false);
  const [pronunciationDraft, setPronunciationDraft] = useState("");
  const [savingPronunciation, setSavingPronunciation] = useState(false);
  const [pronunciationError, setPronunciationError] = useState<string | null>(null);

  const [editingEmployee, setEditingEmployee] = useState(false);
  const [employeeForm, setEmployeeForm] = useState<EmployeeFormState | null>(null);
  const [employeeInitial, setEmployeeInitial] = useState<EmployeeFormState | null>(null);
  const [savingEmployee, setSavingEmployee] = useState(false);
  const [employeeError, setEmployeeError] = useState<string | null>(null);

  const isOwnProfile = personId === identity.id;
  // Presentation only — mirrors app/writes.py's real gate (EDITABLE table +
  // the person_id == caller.id self-block), which is what actually decides
  // whether a PATCH succeeds. Showing this button to the wrong role/mode
  // would produce a 403 on Save, not a security hole; the server is the
  // only enforcement that matters.
  const canEditEmployee = identity.role === "hr" && viewMode === "work" && !isOwnProfile;

  useEffect(() => {
    let cancelled = false;
    setDetail(undefined);
    setReports([]);
    setError(null);
    setEditingBio(false);
    setBioError(null);
    setEditingPronunciation(false);
    setPronunciationError(null);
    // Closes any open edit form on a profile/identity/mode change, so a
    // draft never lingers pointed at the wrong person — same reasoning as
    // the bio-edit reset just above.
    setEditingEmployee(false);
    setEmployeeError(null);
    setEmployeeForm(null);
    setEmployeeInitial(null);
    Promise.all([getPerson(identity, personId, viewMode), getOrgChart(identity, personId, "down", 1)])
      .then(([person, chain]) => {
        if (cancelled) return;
        setDetail(person);
        setReports(chain);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e instanceof ApiError ? e.message : "Unknown error");
        setDetail(null);
      });
    return () => {
      cancelled = true;
    };
  }, [personId, identity, viewMode]);

  if (detail === undefined) {
    return (
      <div className="profile-page">
        <div className="skel skel-line" style={{ width: "40%" }} />
        <div className="skel" style={{ height: 76, width: 76, borderRadius: "50%" }} />
        <div className="skel skel-card" />
        <div className="skel skel-card" />
      </div>
    );
  }

  if (detail === null) {
    return (
      <div className="state-block error" style={{ padding: "60px 20px" }}>
        <strong>Couldn't load this profile</strong>
        <p>{error ?? "Not found, or not visible to your role."}</p>
      </div>
    );
  }

  const backTarget = stack.length > 1 ? stack[stack.length - 2] : null;

  function startEditingBio() {
    setBioDraft(detail!.bio ?? "");
    setBioError(null);
    setEditingBio(true);
  }

  async function saveBio() {
    setSavingBio(true);
    setBioError(null);
    try {
      const updated = await updateOwnBio(identity, bioDraft.trim());
      setDetail((prev) => (prev ? { ...prev, bio: updated.bio } : prev));
      setEditingBio(false);
    } catch (e) {
      setBioError(e instanceof ApiError ? e.message : "Couldn't save — try again.");
    } finally {
      setSavingBio(false);
    }
  }

  function startEditingPronunciation() {
    setPronunciationDraft(detail!.name_pronunciation ?? "");
    setPronunciationError(null);
    setEditingPronunciation(true);
  }

  async function savePronunciation() {
    setSavingPronunciation(true);
    setPronunciationError(null);
    try {
      const updated = await updateOwnNamePronunciation(identity, pronunciationDraft.trim());
      setDetail((prev) => (prev ? { ...prev, name_pronunciation: updated.name_pronunciation } : prev));
      setEditingPronunciation(false);
    } catch (e) {
      setPronunciationError(e instanceof ApiError ? e.message : "Couldn't save — try again.");
    } finally {
      setSavingPronunciation(false);
    }
  }

  function startEditingEmployee() {
    const initial = employeeFormFromDetail(detail!);
    setEmployeeInitial(initial);
    setEmployeeForm(initial);
    setEmployeeError(null);
    setEditingEmployee(true);
  }

  async function saveEmployee() {
    if (!employeeForm || !employeeInitial) return;
    if (!employeeFormValid(employeeForm)) {
      setEmployeeError("Name, job title, work email and hire date can't be empty.");
      return;
    }
    const changes = diffEmployeeForm(employeeInitial, employeeForm);
    if (Object.keys(changes).length === 0) {
      // Nothing actually changed — closing is the honest outcome, not a
      // no-op PATCH that would still write an audit_log row for no reason.
      setEditingEmployee(false);
      return;
    }
    setSavingEmployee(true);
    setEmployeeError(null);
    try {
      const updated = await updateEmployee(identity, personId, changes, viewMode);
      setDetail(updated);
      setEditingEmployee(false);
    } catch (e) {
      setEmployeeError(e instanceof ApiError ? e.message : "Couldn't save — try again.");
    } finally {
      setSavingEmployee(false);
    }
  }

  return (
    <div className="profile-page">
      {(backTarget || onBackToSearch) && (
        <div className="profile-nav">
          {onBackToSearch && (
            <button className="profile-back" onClick={onBackToSearch}>
              <ChevronLeft size={16} /> Back to search
            </button>
          )}
          {backTarget && (
            <button className="profile-back" onClick={onBack}>
              <ChevronLeft size={16} /> Back to {backTarget.name || "previous profile"}
            </button>
          )}
          {stack.length > 2 && (
            <nav className="profile-breadcrumb" aria-label="Profile navigation">
              {stack.map((entry, i) => (
                <span key={entry.id} className="crumb">
                  {i > 0 && <span className="crumb-sep">/</span>}
                  {i === stack.length - 1 ? (
                    <span className="crumb-current">{entry.name || "…"}</span>
                  ) : (
                    <button className="crumb-link" onClick={() => onBreadcrumb(i)}>{entry.name || "…"}</button>
                  )}
                </span>
              ))}
            </nav>
          )}
        </div>
      )}
      <div className="profile-header">
        <span className="avatar" aria-hidden="true">{initials(detail.full_name)}</span>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div className="p-name-line">
            <h2 className="p-name">{detail.preferred_name || detail.full_name}</h2>
            {!editingPronunciation && detail.name_pronunciation && (
              <button
                type="button"
                className="pronunciation-btn"
                onClick={() => speakPronunciation(detail.name_pronunciation!)}
                title="Hear pronunciation"
                aria-label={`Hear pronunciation: ${detail.name_pronunciation}`}
              >
                <Volume size={13} /> {detail.name_pronunciation}
              </button>
            )}
            {isOwnProfile && !editingPronunciation && (
              <button className="link-btn" style={{ fontSize: 12.5 }} onClick={startEditingPronunciation}>
                {detail.name_pronunciation ? "Edit pronunciation" : "Add pronunciation"}
              </button>
            )}
          </div>
          {editingPronunciation && (
            <div className="pronunciation-edit">
              <input
                className="edit-input"
                style={{ maxWidth: 220 }}
                value={pronunciationDraft}
                onChange={(e) => setPronunciationDraft(e.target.value)}
                maxLength={200}
                autoFocus
                placeholder="e.g. nuh-VAY-uh"
              />
              <button className="btn" onClick={() => setEditingPronunciation(false)} disabled={savingPronunciation}>
                Cancel
              </button>
              <button className="btn btn-primary" onClick={savePronunciation} disabled={savingPronunciation}>
                {savingPronunciation ? "Saving…" : "Save"}
              </button>
              {pronunciationError && <p className="bio-error" style={{ width: "100%" }}>{pronunciationError}</p>}
            </div>
          )}
          {detail.job_title && <p className="p-role">{detail.job_title}</p>}

          <ul className="p-facts">
            {detail.org_unit && (
              <li><Building size={15} />{detail.org_unit}</li>
            )}
            {detail.office && (
              <li><MapPin size={15} />{detail.office.name} &middot; {detail.office.city}, {detail.office.country}</li>
            )}
            {detail.effective_timezone && localTimeInfo(detail.effective_timezone) && (
              <li><Clock size={15} />{localTimeInfo(detail.effective_timezone)}</li>
            )}
            {detail.availability_status && (
              <li>
                <Clock size={15} />
                {detail.availability_status === "away" ? (
                  <span>
                    <span className="away-text">Away</span>
                    {detail.away_until_month ? ` · back ${detail.away_until_month}` : ""}
                    {detail.delegate && (
                      <> &middot; <button className="link-btn" onClick={() => onNavigate(detail.delegate!.id, detail.delegate!.full_name)}>{detail.delegate.full_name}</button> covering</>
                    )}
                  </span>
                ) : (
                  <span style={{ textTransform: "capitalize" }}>{detail.availability_status}</span>
                )}
              </li>
            )}
            {detail.manager && (
              <li>
                <UserReports size={15} />
                Reports to: <button className="link-btn" onClick={() => onNavigate(detail.manager!.id, detail.manager!.full_name)}>{detail.manager.full_name}</button>
              </li>
            )}
            {detail.tenure_band && (
              <li><Briefcase size={15} /><span>{detail.tenure_band} at Quadrant</span></li>
            )}
          </ul>
        </div>
        {canEditEmployee && !editingEmployee && (
          <button
            className="btn" style={{ alignSelf: "flex-start", flexShrink: 0 }}
            onClick={startEditingEmployee}
          >
            Edit profile
          </button>
        )}
      </div>

      {editingEmployee && employeeForm && (
        <section className="card edit-employee-card">
          <div className="card-head">
            <h2>Edit profile</h2>
            <span className="hr-badge">HR &middot; work mode</span>
          </div>
          <div className="edit-grid">
            <EditField label="Full name">
              <input
                className="edit-input" value={employeeForm.full_name}
                onChange={(e) => setEmployeeForm({ ...employeeForm, full_name: e.target.value })}
              />
            </EditField>
            <EditField label="Preferred name">
              <input
                className="edit-input" value={employeeForm.preferred_name}
                placeholder="(none)"
                onChange={(e) => setEmployeeForm({ ...employeeForm, preferred_name: e.target.value })}
              />
            </EditField>
            <EditField label="Job title">
              <input
                className="edit-input" value={employeeForm.job_title}
                onChange={(e) => setEmployeeForm({ ...employeeForm, job_title: e.target.value })}
              />
            </EditField>
            <EditField label="Employment type">
              <select
                className="edit-input" value={employeeForm.employment_type}
                onChange={(e) => setEmployeeForm({ ...employeeForm, employment_type: e.target.value })}
              >
                <option value="fte">FTE</option>
                <option value="contractor">Contractor</option>
                <option value="intern">Intern</option>
              </select>
            </EditField>
            <EditField label="Work email">
              <input
                className="edit-input" type="email" value={employeeForm.work_email}
                onChange={(e) => setEmployeeForm({ ...employeeForm, work_email: e.target.value })}
              />
            </EditField>
            <EditField label="Work phone">
              <input
                className="edit-input" value={employeeForm.work_phone}
                placeholder="(none)"
                onChange={(e) => setEmployeeForm({ ...employeeForm, work_phone: e.target.value })}
              />
            </EditField>
            <EditField label="Hire date">
              <input
                className="edit-input" type="date" value={employeeForm.hire_date}
                onChange={(e) => setEmployeeForm({ ...employeeForm, hire_date: e.target.value })}
              />
            </EditField>
            <EditField label="Date of birth" badge="HR only">
              <input
                className="edit-input" type="date" value={employeeForm.date_of_birth}
                onChange={(e) => setEmployeeForm({ ...employeeForm, date_of_birth: e.target.value })}
              />
            </EditField>
            <EditField label="Cost centre" badge="HR only">
              <input
                className="edit-input" value={employeeForm.cost_centre}
                placeholder="(none)"
                onChange={(e) => setEmployeeForm({ ...employeeForm, cost_centre: e.target.value })}
              />
            </EditField>
            <EditField label="Salary" badge="HR only">
              <input
                className="edit-input" inputMode="decimal" placeholder="e.g. 95000.00"
                value={employeeForm.salary}
                onChange={(e) => setEmployeeForm({ ...employeeForm, salary: e.target.value })}
              />
            </EditField>
            <EditField label="Salary currency" badge="HR only">
              <input
                className="edit-input" placeholder="USD" maxLength={3}
                value={employeeForm.salary_currency}
                onChange={(e) => setEmployeeForm({ ...employeeForm, salary_currency: e.target.value.toUpperCase() })}
              />
            </EditField>
            {/* No "HR only" badge: linkedin_profile is in permissions.BASE_FIELDS,
                readable by everyone — a LinkedIn page is already public. Only the
                EDIT surface is HR's, which the enclosing form already scopes. */}
            <EditField label="LinkedIn">
              <input
                className="edit-input" type="url" maxLength={500}
                placeholder="https://www.linkedin.com/in/..."
                value={employeeForm.linkedin_profile}
                onChange={(e) => setEmployeeForm({ ...employeeForm, linkedin_profile: e.target.value })}
              />
            </EditField>
          </div>
          {employeeError && <p className="bio-error">{employeeError}</p>}
          <div className="bio-actions">
            <button className="btn" onClick={() => setEditingEmployee(false)} disabled={savingEmployee}>
              Cancel
            </button>
            <button
              className="btn btn-primary" onClick={saveEmployee}
              disabled={savingEmployee || !employeeFormValid(employeeForm)}
            >
              {savingEmployee ? "Saving…" : "Save changes"}
            </button>
          </div>
        </section>
      )}

      <div className="profile-page-body">
        <div className="profile-page-col">
          {(detail.bio || isOwnProfile) && (
            <section className="card">
              <div className="card-head">
                <h2>About</h2>
                {isOwnProfile && !editingBio && (
                  <button className="link-btn" style={{ fontSize: 12.5 }} onClick={startEditingBio}>Edit</button>
                )}
              </div>
              {editingBio ? (
                <div className="bio-edit">
                  <textarea
                    className="bio-textarea"
                    value={bioDraft}
                    onChange={(e) => setBioDraft(e.target.value)}
                    maxLength={2000}
                    rows={4}
                    autoFocus
                    placeholder="Tell people a bit about yourself…"
                  />
                  {bioError && <p className="bio-error">{bioError}</p>}
                  <div className="bio-actions">
                    <button className="btn" onClick={() => setEditingBio(false)} disabled={savingBio}>Cancel</button>
                    <button className="btn btn-primary" onClick={saveBio} disabled={savingBio}>
                      {savingBio ? "Saving…" : "Save"}
                    </button>
                  </div>
                </div>
              ) : detail.bio ? (
                <p style={{ margin: 0, fontSize: 13.5, color: "var(--muted)" }}>{detail.bio}</p>
              ) : (
                <p style={{ margin: 0, fontSize: 13.5, color: "var(--muted)" }}>
                  You haven't added an about yet. <button className="link-btn" onClick={startEditingBio}>Add one</button>
                </p>
              )}
            </section>
          )}

          {detail.skills && detail.skills.length > 0 && (
            <section className="card">
              <h2>Skills</h2>
              {groupByLevel(detail.skills).map(([level, items]) => (
                <div className="skill-group" key={level}>
                  <p className="skill-label">{level}</p>
                  <div className="pills">
                    {items.map((s) => (
                      <span className={`pill ${LEVEL_CLASS[s.level] ?? ""}`} key={s.name}>
                        {s.name} <span className="src">&middot; {s.source}</span>
                      </span>
                    ))}
                  </div>
                </div>
              ))}
            </section>
          )}

          {detail.languages && detail.languages.length > 0 && (
            <section className="card">
              <h2>Languages</h2>
              <div className="pills">
                {detail.languages.map((l) => (
                  <span className="pill" key={l.name}>{l.name}</span>
                ))}
              </div>
            </section>
          )}

          {/* Absent for a caller who isn't the person, their reporting
              chain, or HR — and also absent when the provider couldn't be
              reached, which is why an empty list still renders the card
              (nothing required) but a missing key renders nothing at all. */}
          {detail.training_status && (
            <section className="card">
              <div className="card-head">
                <h2><GraduationCap size={15} className="reports-icon" />Training</h2>
                <span className="abac-badge">Self/chain/HR</span>
              </div>
              {detail.training_status.length === 0 ? (
                <p style={{ margin: 0, fontSize: 13.5, color: "var(--muted)" }}>
                  No courses are expected for this role.
                </p>
              ) : (
                <ul className="training-list">
                  {detail.training_status.map((t) => {
                    const completed = t.display_status === "completed";
                    return (
                      <li key={t.course_code}>
                        <span className={`training-icon ${completed ? "ok" : "warn"}`} aria-hidden="true">
                          {completed ? <Check /> : <AlertCircle />}
                        </span>
                        <div className="training-text">
                          <p className="training-name">
                            {t.course_name}
                            {!t.expected && <span className="training-tag">Not required</span>}
                          </p>
                          <p className="training-meta">
                            {/* display_label is the backend's copy, not ours — the
                                four-value status behind it never reaches the client. */}
                            <span className={completed ? "training-ok" : "training-warn"}>{t.display_label}</span>
                            {completed && t.completed_month ? ` · ${t.completed_month}` : ""}
                            {!completed && t.attempted_month ? ` · last attempt ${t.attempted_month}` : ""}
                          </p>
                        </div>
                      </li>
                    );
                  })}
                </ul>
              )}
            </section>
          )}

          {/* Rendered whenever the field is present at all — every caller who
              can see the record gets project_history (it's in BASE_FIELDS).
              A genuinely empty portfolio still gets the section, with its
              own message, rather than the card disappearing: an absent
              section reads as "you can't see this", which isn't true here —
              the honest state is "nothing on file yet", the same
              absent-vs-null distinction salary and project_desc follow. */}
          {detail.project_history && (
            <section className="card">
              <h2>Project history</h2>
              {detail.project_history.length > 0 ? (
                <ul className="timeline">
                  {detail.project_history.map((p, i) => (
                    <li key={i} className={p.current ? "current" : ""}>
                      <p className="job">{p.project_name}</p>
                      <p className="job-meta">
                        {p.role} &middot; {p.project_type} &middot; {p.start_month} &ndash; {p.current ? "Present" : p.end_month}
                      </p>
                      {/* Visible to every role/view_mode now — project_desc
                          moved into BASE_FIELDS. `!== undefined` stays
                          rather than becoming a truthiness check, so if a
                          future rule ever narrows visibility again this
                          still reads absent-means-not-permitted correctly
                          instead of silently changing meaning. */}
                      {p.project_desc !== undefined && (
                        <p className="job-desc">
                          {p.project_desc || <span className="muted">No description on file</span>}
                        </p>
                      )}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="muted">No projects on file yet.</p>
              )}
            </section>
          )}
        </div>

        <div className="profile-page-col">
          {reports.length > 0 && (
            <section className="card">
              <div className="card-head">
                <h2><Users size={15} className="reports-icon" />Direct reports</h2>
                <span className="link-sm" style={{ color: "var(--muted)", fontSize: 12.5 }}>{reports.length}</span>
              </div>
              <ul className="reports-list">
                {reports.map((r) => (
                  <li key={r.id}>
                    <button onClick={() => onNavigate(r.id, r.full_name)}>
                      <span>{r.full_name}</span>
                      <span className="sub">{r.job_title}</span>
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          )}

          <section className="card">
            <h2>Contact &amp; details</h2>
            <ul className="details">
              {detail.work_email && (
                <li><Mail size={16} /><span className="k">Email</span><span className="v"><a href={`mailto:${detail.work_email}`}>{detail.work_email}</a></span></li>
              )}
              {detail.work_phone && (
                <li><Phone size={16} /><span className="k">Phone</span><span className="v">{detail.work_phone}</span></li>
              )}
              {detail.personal_mobile && (
                <li>
                  <Phone size={16} />
                  <span className="k">Mobile<span className="abac-badge">Manager/self</span></span>
                  <span className="v">{detail.personal_mobile}</span>
                </li>
              )}
              {detail.slack_handle && (
                <li><Slack size={16} /><span className="k">Slack</span><span className="v">{detail.slack_handle}</span></li>
              )}
              {detail.linkedin_profile && (
                <li>
                  <LinkIcon size={16} />
                  <span className="k">LinkedIn</span>
                  <span className="v">
                    {/* noreferrer alongside noopener: this is an outbound link to a
                        third-party site, and the referrer would otherwise leak the
                        internal directory URL (including the employee id in the path). */}
                    <a href={detail.linkedin_profile} target="_blank" rel="noopener noreferrer">
                      {detail.linkedin_profile.replace(/^https?:\/\/(www\.)?linkedin\.com\/in\//, "")}
                    </a>
                  </span>
                </li>
              )}
              {detail.employment_type && (
                <li><Briefcase size={16} /><span className="k">Employment</span><span className="v" style={{ textTransform: "capitalize" }}>{detail.employment_type}</span></li>
              )}
              {detail.hire_date && (
                <li>
                  <Briefcase size={16} />
                  <span className="k">Hire date<span className="hr-badge">HR only</span></span>
                  <span className="v">{detail.hire_date}</span>
                </li>
              )}
              {detail.cost_centre && (
                <li>
                  <Briefcase size={16} />
                  <span className="k">Cost centre<span className="hr-badge">HR only</span></span>
                  <span className="v">{detail.cost_centre}</span>
                </li>
              )}
              {detail.date_of_birth && (
                <li>
                  <Cake size={16} />
                  <span className="k">Date of birth<span className="abac-badge">HR/self</span></span>
                  <span className="v">{formatDate(detail.date_of_birth)}</span>
                </li>
              )}
              {detail.salary && (
                <li>
                  <Briefcase size={16} />
                  <span className="k">Salary<span className="abac-badge">HR/self</span></span>
                  <span className="v">{formatSalary(detail.salary, detail.salary_currency)}</span>
                </li>
              )}
            </ul>
          </section>
        </div>
      </div>
    </div>
  );
}
