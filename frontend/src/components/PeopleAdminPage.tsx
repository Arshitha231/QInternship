import { useEffect, useState } from "react";
import {
  ApiError, listDeactivatedEmployees, listOffices, listOrgUnits, reactivateEmployee,
  requestEmployeeCreation,
} from "../api";
import type { CreateEmployeeFields, DeactivatedEmployee } from "../api";
import type { Identity, OfficeOut, OrgUnitOut, PersonSummary, ViewMode } from "../types";
import { EditField } from "./ProfilePage";
import { EmployeeSearchPicker } from "./ReviewPage";

// HR-only, work mode only — see App.tsx's tab gating and
// app.writes.request_creation's own "create_employee" EDITABLE capability,
// the actual server-side enforcement this UI only mirrors.
//
// Submitting does not create anybody: it stages a request for the HR user's
// own manager to approve, exactly like restricting or deactivating someone.
// The copy below is deliberate about that — an HR user who thinks they just
// added a starter, and doesn't chase the approval, has a new hire with no
// directory entry on day one.

type FormState = {
  full_name: string;
  preferred_name: string;
  job_title: string;
  org_unit_id: string;
  office_id: string;
  manager_id: string;
  manager_name: string;
  mentor_id: string;
  mentor_name: string;
  work_email: string;
  work_phone: string;
  employment_type: "fte" | "contractor" | "intern";
  hire_date: string;
};

const EMPTY_FORM: FormState = {
  full_name: "", preferred_name: "", job_title: "", org_unit_id: "", office_id: "",
  manager_id: "", manager_name: "", mentor_id: "", mentor_name: "",
  work_email: "", work_phone: "", employment_type: "fte", hire_date: "",
};

function unitLabel(unit: OrgUnitOut): string {
  return `${unit.name} — ${unit.unit_type}`;
}

export function PeopleAdminPage({
  identity, viewMode, onOpenPerson,
}: {
  identity: Identity;
  viewMode: ViewMode;
  // Reactivating makes a profile reachable again, so the flow ends by
  // offering to go look at it — same shape GraphPage's onOpenProfile uses.
  onOpenPerson?: (id: string, name: string) => void;
}) {
  const [orgUnits, setOrgUnits] = useState<OrgUnitOut[] | null>(null);
  const [offices, setOffices] = useState<OfficeOut[] | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [staged, setStaged] = useState<{ full_name: string; approver_name: string | null } | null>(null);

  const [deactivated, setDeactivated] = useState<DeactivatedEmployee[] | null>(null);
  const [reactivatingId, setReactivatingId] = useState<string | null>(null);
  const [deactivatedError, setDeactivatedError] = useState<string | null>(null);
  const [reactivated, setReactivated] = useState<{ id: string; full_name: string } | null>(null);
  const [deactivatedToken, setDeactivatedToken] = useState(0);

  useEffect(() => {
    let cancelled = false;
    Promise.all([listOrgUnits(identity), listOffices(identity)]).then(([units, offs]) => {
      if (cancelled) return;
      setOrgUnits(units);
      setOffices(offs);
    });
    return () => {
      cancelled = true;
    };
  }, [identity]);

  useEffect(() => {
    let cancelled = false;
    listDeactivatedEmployees(identity, viewMode)
      .then((r) => {
        if (!cancelled) setDeactivated(r.employees);
      })
      .catch((e) => {
        if (!cancelled) {
          setDeactivated([]);
          setDeactivatedError(e instanceof ApiError ? e.message : "Couldn't load deactivated employees.");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [identity, viewMode, deactivatedToken]);

  async function handleReactivate(employee: DeactivatedEmployee) {
    if (!window.confirm(`Reactivate ${employee.full_name}? Their profile becomes visible again.`)) return;
    setReactivatingId(employee.id);
    setDeactivatedError(null);
    try {
      await reactivateEmployee(identity, employee.id, viewMode);
      setReactivated({ id: employee.id, full_name: employee.full_name });
      setDeactivatedToken((t) => t + 1);
    } catch (e) {
      setDeactivatedError(e instanceof ApiError ? e.message : "Couldn't reactivate — try again.");
    } finally {
      setReactivatingId(null);
    }
  }

  function set<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((prev) => ({ ...prev, [key]: value }));
  }

  function valid(): boolean {
    return !!(form.full_name.trim() && form.job_title.trim() && form.org_unit_id && form.work_email.trim());
  }

  async function handleSubmit() {
    if (!valid()) {
      setError("Full name, job title, org unit, and work email are required.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const fields: CreateEmployeeFields = {
        full_name: form.full_name.trim(),
        job_title: form.job_title.trim(),
        org_unit_id: Number(form.org_unit_id),
        work_email: form.work_email.trim(),
        employment_type: form.employment_type,
      };
      if (form.preferred_name.trim()) fields.preferred_name = form.preferred_name.trim();
      if (form.office_id) fields.office_id = Number(form.office_id);
      if (form.manager_id) fields.manager_id = form.manager_id;
      if (form.mentor_id) fields.mentor_id = form.mentor_id;
      if (form.work_phone.trim()) fields.work_phone = form.work_phone.trim();
      if (form.hire_date) fields.hire_date = form.hire_date;

      const result = await requestEmployeeCreation(identity, fields, viewMode);
      setStaged({ full_name: result.target_name, approver_name: result.approver_name });
      setForm(EMPTY_FORM);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't send this for approval — try again.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="people-admin-page">
      <section className="card">
        <h2>Add an employee</h2>
        <p className="continuity-meta">
          Full name, job title, org unit, and work email are required. Everything else — salary, date of
          birth, cost centre, and so on — can be filled in from their profile afterward. Adding someone
          needs a second pair of eyes, so this goes to your manager for approval before the profile
          exists.
        </p>

        {staged && (
          <p className="review-upload-summary">
            <strong>{staged.full_name}</strong> was sent for approval
            {staged.approver_name ? <> to <strong>{staged.approver_name}</strong></> : null}. The profile
            is created once they approve it.
          </p>
        )}
        {error && <p className="bio-error">{error}</p>}

        <div className="edit-grid" style={{ marginTop: 16 }}>
          <EditField label="Full name">
            <input className="edit-input" value={form.full_name} onChange={(e) => set("full_name", e.target.value)} />
          </EditField>
          <EditField label="Preferred name">
            <input
              className="edit-input" placeholder="(none)" value={form.preferred_name}
              onChange={(e) => set("preferred_name", e.target.value)}
            />
          </EditField>
          <EditField label="Job title">
            <input className="edit-input" value={form.job_title} onChange={(e) => set("job_title", e.target.value)} />
          </EditField>
          <EditField label="Employment type">
            <select
              className="edit-input" value={form.employment_type}
              onChange={(e) => set("employment_type", e.target.value as FormState["employment_type"])}
            >
              <option value="fte">FTE</option>
              <option value="contractor">Contractor</option>
              <option value="intern">Intern</option>
            </select>
          </EditField>
          <EditField label="Org unit">
            <select className="edit-input" value={form.org_unit_id} onChange={(e) => set("org_unit_id", e.target.value)}>
              <option value="">Select…</option>
              {(orgUnits ?? []).map((u) => (
                <option key={u.id} value={u.id}>{unitLabel(u)}</option>
              ))}
            </select>
          </EditField>
          <EditField label="Office">
            <select className="edit-input" value={form.office_id} onChange={(e) => set("office_id", e.target.value)}>
              <option value="">(none)</option>
              {(offices ?? []).map((o) => (
                <option key={o.id} value={o.id}>{o.name} — {o.city}</option>
              ))}
            </select>
          </EditField>
          <EditField label="Work email">
            <input
              className="edit-input" type="email" value={form.work_email}
              onChange={(e) => set("work_email", e.target.value)}
            />
          </EditField>
          <EditField label="Work phone">
            <input
              className="edit-input" placeholder="(none)" value={form.work_phone}
              onChange={(e) => set("work_phone", e.target.value)}
            />
          </EditField>
          <EditField label="Hire date">
            <input
              className="edit-input" type="date" value={form.hire_date}
              onChange={(e) => set("hire_date", e.target.value)}
            />
          </EditField>
          <EditField label="Manager">
            {form.manager_name ? (
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span>{form.manager_name}</span>
                <button type="button" className="link-btn" onClick={() => { set("manager_id", ""); set("manager_name", ""); }}>
                  Clear
                </button>
              </div>
            ) : (
              <EmployeeSearchPicker
                identity={identity} viewMode={viewMode} placeholder="Search for a manager… (optional)"
                onSelect={(p: PersonSummary) => { set("manager_id", p.id); set("manager_name", p.full_name); }}
              />
            )}
          </EditField>
          {/* Not a field on the person — this becomes an official link in
              the new hire's own community graph on approval. Left blank,
              app.community_links.auto_assign_mentors' sweep will pick
              somebody for them later; naming one here is HR overriding
              that guess with a real decision. */}
          <EditField label="Mentor">
            {form.mentor_name ? (
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span>{form.mentor_name}</span>
                <button type="button" className="link-btn" onClick={() => { set("mentor_id", ""); set("mentor_name", ""); }}>
                  Clear
                </button>
              </div>
            ) : (
              <EmployeeSearchPicker
                identity={identity} viewMode={viewMode}
                placeholder="Do they have a mentor? (optional)"
                onSelect={(p: PersonSummary) => { set("mentor_id", p.id); set("mentor_name", p.full_name); }}
              />
            )}
          </EditField>
        </div>

        {form.mentor_name && (
          <p className="continuity-meta" style={{ marginTop: 12 }}>
            {form.mentor_name} will appear in their community graph as their mentor. If you leave this
            blank, one gets suggested automatically during their first few weeks.
          </p>
        )}

        <div className="bio-actions" style={{ marginTop: 16 }}>
          <button className="btn btn-primary" disabled={saving} onClick={handleSubmit}>
            {saving ? "Sending…" : "Send for approval"}
          </button>
        </div>
      </section>

      <section className="card">
        <div className="card-head">
          <h2>Deactivated employees</h2>
          {deactivated && deactivated.length > 0 && (
            <span className="continuity-meta">{deactivated.length} deactivated</span>
          )}
        </div>
        <p className="continuity-meta">
          Deactivated profiles are invisible everywhere else in the directory — including to HR — so this is
          the only place they can be found and put back.
        </p>

        {reactivated && (
          <p className="review-upload-summary">
            <strong>{reactivated.full_name}</strong> is active again.
            {onOpenPerson && (
              <>
                {" "}
                <button
                  type="button" className="link-btn"
                  onClick={() => onOpenPerson(reactivated.id, reactivated.full_name)}
                >
                  View profile
                </button>
              </>
            )}
          </p>
        )}
        {deactivatedError && <p className="bio-error">{deactivatedError}</p>}

        {deactivated === null ? (
          <div className="skel skel-card" style={{ height: 80 }} />
        ) : deactivated.length === 0 ? (
          <p className="continuity-meta">Nobody is deactivated.</p>
        ) : (
          <ul className="deactivated-list">
            {deactivated.map((employee) => (
              <li key={employee.id} className="deactivated-row">
                <div className="deactivated-who">
                  <p className="job">{employee.full_name}</p>
                  <p className="job-meta">
                    {employee.job_title}
                    {employee.org_unit ? ` · ${employee.org_unit}` : ""}
                    {" · "}
                    {employee.deactivated_at
                      ? `deactivated ${new Date(employee.deactivated_at).toLocaleDateString()}`
                      : "deactivation date not on file"}
                  </p>
                </div>
                <button
                  className="btn" disabled={reactivatingId === employee.id}
                  onClick={() => handleReactivate(employee)}
                >
                  {reactivatingId === employee.id ? "Reactivating…" : "Reactivate"}
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
