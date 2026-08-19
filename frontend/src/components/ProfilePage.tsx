import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import {
  addProjectHistory, ApiError, getOrgChart, getPerson, removeProjectHistory, requestDeactivation,
  requestRestriction, updateEmployee, updateOwnBio, updateOwnNamePronunciation, upsertProjectHistory,
} from "../api";
import type { ActionRequestResult, ActiveDirectReport } from "../api";
import type {
  Identity, OrgChainNode, PersonDetail, ProjectHistoryItem, SkillOut, UpdateEmployeeChanges,
  ViewMode,
} from "../types";
import {
  AlertCircle, Briefcase, Building, Cake, Check, ChevronLeft, Clock, GraduationCap, Mail,
  LinkIcon, MapPin, Phone, Slack, UserReports, Users, Volume,
} from "../icons";
import { EmployeeSearchPicker } from "./ReviewPage";

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

export function EditField({ label, badge, children }: { label: string; badge?: string; children: ReactNode }) {
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

// ---------------------------------------------------------------------------
// Add someone to a project. Named by hand rather than picked from a list:
// an unrecognised name creates the project, a matching one joins it (see
// app/writes.py's get_or_create_project, the same path accepting a
// document's project_entry has always taken).
// ---------------------------------------------------------------------------

function AddProjectForm({
  personId, identity, viewMode, onDone,
}: { personId: string; identity: Identity; viewMode: ViewMode; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [name, setName] = useState("");
  const [role, setRole] = useState("");
  const [startMonth, setStartMonth] = useState("");
  const [current, setCurrent] = useState(true);
  const [endMonth, setEndMonth] = useState("");
  const [contribution, setContribution] = useState("");
  const [projectDesc, setProjectDesc] = useState("");

  function reset() {
    setName(""); setRole(""); setStartMonth(""); setCurrent(true);
    setEndMonth(""); setContribution(""); setProjectDesc("");
    setError(null);
  }

  async function submit() {
    if (!name.trim() || !role.trim() || !startMonth) {
      setError("Project name, role and start month are required.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await addProjectHistory(identity, personId, {
        project_name: name.trim(),
        role: role.trim(),
        // Month precision in, first-of-month out — the read side only ever
        // publishes month/year, so there is no day here to preserve.
        start_date: `${startMonth}-01`,
        end_date: current ? null : (endMonth ? `${endMonth}-01` : null),
        // Omitted rather than sent empty: the backend writes only the keys
        // it receives, and an empty description would blank a real one on
        // a project that already exists.
        ...(contribution.trim() ? { contribution: contribution.trim() } : {}),
        ...(projectDesc.trim() ? { project_desc: projectDesc.trim() } : {}),
      }, viewMode);
      reset();
      setOpen(false);
      onDone();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't add — try again.");
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <button className="btn btn-primary project-add-btn" onClick={() => { reset(); setOpen(true); }}>
        Add project
      </button>
    );
  }

  return (
    <div className="bio-edit project-history-edit">
      <label className="edit-field">
        <span className="edit-label">Project name</span>
        <input
          className="edit-input" value={name} placeholder="e.g. Project Nightingale"
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <label className="edit-field">
        <span className="edit-label">Role</span>
        <input
          className="edit-input" value={role} placeholder="e.g. Platform Lead"
          onChange={(e) => setRole(e.target.value)}
        />
      </label>
      <label className="edit-field">
        <span className="edit-label">Started</span>
        <input
          className="edit-input" type="month" value={startMonth}
          onChange={(e) => setStartMonth(e.target.value)}
        />
      </label>
      <label className="edit-check">
        <input type="checkbox" checked={current} onChange={(e) => setCurrent(e.target.checked)} />
        <span>Still on this project</span>
      </label>
      {!current && (
        <label className="edit-field">
          <span className="edit-label">Ended</span>
          <input
            className="edit-input" type="month" value={endMonth}
            onChange={(e) => setEndMonth(e.target.value)}
          />
        </label>
      )}
      <label className="edit-field">
        <span className="edit-label">Contribution</span>
        <textarea
          className="edit-input" rows={3} value={contribution}
          placeholder="What this person did on the project"
          onChange={(e) => setContribution(e.target.value)}
        />
      </label>
      <label className="edit-field">
        <span className="edit-label">Project description</span>
        <textarea
          className="edit-input" rows={2} value={projectDesc}
          placeholder="Shared by everyone on this project — leave blank to keep what's there"
          onChange={(e) => setProjectDesc(e.target.value)}
        />
      </label>
      {error && <p className="bio-error">{error}</p>}
      <div className="bio-actions">
        <button className="btn" disabled={busy} onClick={() => setOpen(false)}>Cancel</button>
        <button className="btn btn-primary" disabled={busy} onClick={submit}>
          {busy ? "Adding…" : "Add project"}
        </button>
      </div>
    </div>
  );
}


// ---------------------------------------------------------------------------
// One project-history row, editable in place by IT in work mode.
//
// Scoped to correcting an EXISTING membership (and removing one), which is
// what "edit project history" means and what PUT/DELETE
// /people/{id}/projects/{project_id} address. Creating a membership from
// scratch is a supported backend call but has no control here: it needs a
// project to point at, and there is no endpoint that lists projects to
// pick from — a picker built on guessed ids would be worse than no picker.
//
// role and contribution are the editable fields. start is deliberately NOT
// editable here: ProjectHistoryItem publishes month precision only ("never
// exact dates", see app/schemas.py), so a month picked in this form would
// silently rewrite the stored day to the 1st. That is acceptable for an
// end date the reviewer is deliberately setting, and not acceptable for a
// start date they only meant to leave alone.
// ---------------------------------------------------------------------------

function ProjectHistoryRowEditor({
  item, personId, identity, viewMode, onDone,
}: {
  item: ProjectHistoryItem;
  personId: string;
  identity: Identity;
  viewMode: ViewMode;
  onDone: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [role, setRole] = useState(item.role);
  const [contribution, setContribution] = useState(item.contribution ?? "");
  const [current, setCurrent] = useState(item.current);
  const [endMonth, setEndMonth] = useState(item.end_month ?? "");

  function startEdit() {
    setRole(item.role);
    setContribution(item.contribution ?? "");
    setCurrent(item.current);
    setEndMonth(item.end_month ?? "");
    setError(null);
    setEditing(true);
  }

  async function save() {
    // Only what actually changed goes on the wire — the backend writes
    // exactly the keys it receives, so sending an unchanged field would
    // be a silent no-op write rather than a no-op.
    const changes: Parameters<typeof upsertProjectHistory>[3] = {};
    if (role !== item.role) changes.role = role;
    if (contribution !== (item.contribution ?? "")) changes.contribution = contribution || null;
    if (current !== item.current || (!current && endMonth !== (item.end_month ?? ""))) {
      // A month becomes the 1st of that month; "current" clears the date
      // outright, which is what null means on this column.
      changes.end_date = current ? null : (endMonth ? `${endMonth}-01` : null);
    }
    if (Object.keys(changes).length === 0) {
      setEditing(false);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await upsertProjectHistory(identity, personId, item.project_id, changes, viewMode);
      setEditing(false);
      onDone();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't save — try again.");
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!window.confirm(
      `Remove ${item.project_name} from this person's project history? This can't be undone.`,
    )) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await removeProjectHistory(identity, personId, item.project_id, viewMode);
      onDone();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't remove — try again.");
      setBusy(false);
    }
  }

  if (!editing) {
    return (
      <>
        {error && <p className="bio-error">{error}</p>}
        <div className="project-history-actions">
          <button className="link-btn" disabled={busy} onClick={startEdit}>Edit</button>
          <button className="link-btn link-btn-danger" disabled={busy} onClick={remove}>
            {busy ? "Removing…" : "Remove"}
          </button>
        </div>
      </>
    );
  }

  return (
    <div className="bio-edit project-history-edit">
      <label className="edit-field">
        <span className="edit-label">Role</span>
        <input className="edit-input" value={role} onChange={(e) => setRole(e.target.value)} />
      </label>
      <label className="edit-field">
        <span className="edit-label">Contribution</span>
        <textarea
          className="edit-input" rows={3} value={contribution}
          onChange={(e) => setContribution(e.target.value)}
        />
      </label>
      <label className="edit-check">
        <input type="checkbox" checked={current} onChange={(e) => setCurrent(e.target.checked)} />
        <span>Still on this project</span>
      </label>
      {!current && (
        <label className="edit-field">
          <span className="edit-label">Ended</span>
          <input
            className="edit-input" type="month" value={endMonth}
            onChange={(e) => setEndMonth(e.target.value)}
          />
        </label>
      )}
      {error && <p className="bio-error">{error}</p>}
      <div className="bio-actions">
        <button className="btn" disabled={busy} onClick={() => setEditing(false)}>Cancel</button>
        <button className="btn btn-primary" disabled={busy} onClick={save}>
          {busy ? "Saving…" : "Save"}
        </button>
      </div>
    </div>
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

  const [restrictBusy, setRestrictBusy] = useState(false);
  const [restrictError, setRestrictError] = useState<string | null>(null);
  // Set once a restrict request has been staged this session — restricting
  // is maker-checker now (see app.writes.request_restriction), so this is
  // never the applied state, only "waiting on approver_name."
  const [restrictRequest, setRestrictRequest] = useState<ActionRequestResult | null>(null);

  const [deactivateBusy, setDeactivateBusy] = useState(false);
  const [deactivateError, setDeactivateError] = useState<string | null>(null);
  // Set only on a 409 whose body names who's blocking it — the inline
  // reassignment panel renders from this, not from a second fetch.
  const [blockedReports, setBlockedReports] = useState<ActiveDirectReport[] | null>(null);
  // Same "staged, not applied" shape as restrictRequest above.
  const [deactivateRequest, setDeactivateRequest] = useState<ActionRequestResult | null>(null);

  // Re-fetches the profile after a project-history write. A refetch rather
  // than patching the row in place: the write returns exact dates while
  // this page renders month strings and a derived `current` flag, so
  // reconciling by hand would duplicate the backend's own derivation.
  const [historyToken, setHistoryToken] = useState(0);

  const isOwnProfile = personId === identity.id;
  // Presentation only — mirrors app/writes.py's real gate (EDITABLE table +
  // the person_id == caller.id self-block), which is what actually decides
  // whether a PATCH succeeds. Showing this button to the wrong role/mode
  // would produce a 403 on Save, not a security hole; the server is the
  // only enforcement that matters.
  const canEditEmployee = identity.role === "hr" && viewMode === "work" && !isOwnProfile;
  // Same presentation-only mirror, of app/writes.py's project-history gate:
  // EDITABLE grants "project_entry"/"contribution" to it/work, and
  // _refuse_own_record blocks the caller's own record. !isOwnProfile is the
  // visible half of that second rule — an IT person looking at their own
  // profile sees no edit controls at all, rather than buttons that 403.
  const canEditProjectHistory = identity.role === "it" && viewMode === "work" && !isOwnProfile;

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
    setRestrictError(null);
    setRestrictRequest(null);
    setDeactivateError(null);
    setBlockedReports(null);
    setDeactivateRequest(null);
    Promise.all([getPerson(identity, personId, viewMode), getOrgChart(identity, personId, "down", viewMode, 1)])
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
  }, [personId, identity, viewMode, historyToken]);

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

  // Unrestricting stays single-actor and immediate — the maker-checker
  // requirement is specifically about the transition INTO restricted (see
  // requestRestriction below), not out of it.
  async function handleUnrestrict() {
    if (!window.confirm(
      `Unrestrict ${detail!.full_name}'s profile? It becomes visible to everyone again, same as before it was restricted.`,
    )) return;
    setRestrictBusy(true);
    setRestrictError(null);
    try {
      const updated = await updateEmployee(identity, personId, { availability_status: "available" }, viewMode);
      setDetail(updated);
    } catch (e) {
      setRestrictError(e instanceof ApiError ? e.message : "Couldn't unrestrict — try again.");
    } finally {
      setRestrictBusy(false);
    }
  }

  // Stages a request — does not restrict anything itself. See
  // app.writes.request_restriction: the requester's OWN manager (escalating
  // up the chain, delegate-first, if they're away) has to approve first.
  async function handleRequestRestrict() {
    if (!window.confirm(
      `Request restriction of ${detail!.full_name}'s profile? It won't take effect until your manager approves it. Once approved, it becomes invisible everywhere — search, org charts, their own manager — to everyone except HR.`,
    )) return;
    setRestrictBusy(true);
    setRestrictError(null);
    try {
      const result = await requestRestriction(identity, personId, viewMode);
      setRestrictRequest(result);
    } catch (e) {
      setRestrictError(e instanceof ApiError ? e.message : "Couldn't request restriction — try again.");
    } finally {
      setRestrictBusy(false);
    }
  }

  // Stages a request — does not deactivate anyone itself. Still blocked
  // (409) up front while the target manages anyone active; see
  // app.writes.request_deactivation.
  async function handleRequestDeactivate() {
    if (!window.confirm(
      `Request deactivation of ${detail!.full_name}? This won't take effect until your manager approves it.`,
    )) return;

    setDeactivateBusy(true);
    setDeactivateError(null);
    setBlockedReports(null);
    try {
      const result = await requestDeactivation(identity, personId, viewMode);
      setDeactivateRequest(result);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.detail && typeof e.detail === "object"
          && "active_direct_reports" in e.detail) {
        setBlockedReports((e.detail as { active_direct_reports: ActiveDirectReport[] }).active_direct_reports);
      }
      setDeactivateError(e instanceof ApiError ? e.message : "Couldn't request deactivation — try again.");
    } finally {
      setDeactivateBusy(false);
    }
  }

  async function reassignBlockedReport(reportId: string, newManagerId: string) {
    await updateEmployee(identity, reportId, { manager_id: newManagerId }, viewMode);
    setBlockedReports((prev) => (prev ? prev.filter((r) => r.id !== reportId) : prev));
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
          <div className="profile-hr-actions" style={{ alignSelf: "flex-start", flexShrink: 0 }}>
            <button className="btn" onClick={startEditingEmployee}>
              Edit profile
            </button>
            {detail.availability_status === "restricted" ? (
              <button className="btn" disabled={restrictBusy} onClick={handleUnrestrict}>
                {restrictBusy ? "Working…" : "Unrestrict profile"}
              </button>
            ) : (
              <button
                className="btn btn-danger-outline" disabled={restrictBusy || !!restrictRequest}
                onClick={handleRequestRestrict}
              >
                {restrictBusy ? "Working…" : restrictRequest ? "Restriction requested" : "Restrict profile"}
              </button>
            )}
            <button
              className="btn btn-danger-outline" disabled={deactivateBusy || !!deactivateRequest}
              onClick={handleRequestDeactivate}
            >
              {deactivateBusy ? "Working…" : deactivateRequest ? "Deactivation requested" : "Deactivate employee"}
            </button>
          </div>
        )}
      </div>
      {restrictError && (
        <p className="bio-error" style={{ marginTop: -12 }}>{restrictError}</p>
      )}
      {restrictRequest && (
        <p className="continuity-meta" style={{ marginTop: -12 }}>
          Restriction requested — pending approval from {restrictRequest.approver_name ?? "your manager"}.
        </p>
      )}
      {deactivateError && !blockedReports && (
        <p className="bio-error" style={{ marginTop: -12 }}>{deactivateError}</p>
      )}
      {deactivateRequest && (
        <p className="continuity-meta" style={{ marginTop: -12 }}>
          Deactivation requested — pending approval from {deactivateRequest.approver_name ?? "your manager"}.
        </p>
      )}

      {blockedReports && blockedReports.length > 0 && (
        <section className="card">
          <div className="card-head">
            <h2>Reassign direct reports first</h2>
          </div>
          <p className="continuity-meta">
            {detail.full_name} can't be deactivated while they still manage {blockedReports.length}{" "}
            active {blockedReports.length === 1 ? "person" : "people"}. Pick a new manager for each, then
            request deactivation again.
          </p>
          <ul className="review-decided-list" style={{ marginTop: 12 }}>
            {blockedReports.map((report) => (
              <li key={report.id} style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
                <span style={{ minWidth: 160 }}>{report.full_name}</span>
                <EmployeeSearchPicker
                  identity={identity} viewMode={viewMode} placeholder="New manager…"
                  onSelect={(p) => reassignBlockedReport(report.id, p.id)}
                />
              </li>
            ))}
          </ul>
        </section>
      )}
      {blockedReports && blockedReports.length === 0 && (
        <p className="continuity-meta" style={{ marginTop: -12 }}>
          Everyone's reassigned — click Deactivate employee again to finish.
        </p>
      )}

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
                      {/* This person's own account of what they did here —
                          EmployeeProject.contribution, distinct from the
                          project's own shared description below. Same
                          absent-vs-empty rule: `!== undefined` rather than a
                          truthiness check, so a caller who can see
                          project_history at all always gets the key, empty
                          or not. Most rows won't have one yet (it's set via
                          accept()/edit() on an AI-extracted or manually
                          typed proposal, not backfilled), which is exactly
                          why "nothing on file" is worth saying explicitly
                          rather than the row silently having one fewer line. */}
                      {p.contribution !== undefined && (
                        <p className="job-desc">
                          <strong>Contribution: </strong>
                          {p.contribution || <span className="muted">No contribution note on file</span>}
                        </p>
                      )}
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
                      {canEditProjectHistory && (
                        <ProjectHistoryRowEditor
                          item={p} personId={personId} identity={identity}
                          viewMode={viewMode} onDone={() => setHistoryToken((t) => t + 1)}
                        />
                      )}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="muted">No projects on file yet.</p>
              )}
              {canEditProjectHistory && (
                <AddProjectForm
                  personId={personId} identity={identity} viewMode={viewMode}
                  onDone={() => setHistoryToken((t) => t + 1)}
                />
              )}
              {/* Why the controls above are absent on your own profile.
                  Without this the section looks identical to one where the
                  feature failed to load — which is exactly how it read the
                  first time someone signed in as IT and opened their own
                  page. Says the rule rather than showing a disabled button,
                  since the rule is not going to change by waiting. */}
              {isOwnProfile && identity.role === "it" && viewMode === "work" && (
                <p className="muted project-history-self-note">
                  You can't edit your own project history — ask another IT
                  colleague to make the change.
                </p>
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
