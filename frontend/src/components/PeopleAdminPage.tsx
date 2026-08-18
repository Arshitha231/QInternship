import { useEffect, useState } from "react";
import { ApiError, createEmployee, listOffices, listOrgUnits } from "../api";
import type { CreateEmployeeFields } from "../api";
import type { Identity, OfficeOut, OrgUnitOut, PersonSummary, ViewMode } from "../types";
import { EditField } from "./ProfilePage";
import { EmployeeSearchPicker } from "./ReviewPage";

// HR-only, work mode only — see App.tsx's tab gating and
// app.writes.create_employee's own "create_employee" EDITABLE capability,
// the actual server-side enforcement this UI only mirrors.

type FormState = {
  full_name: string;
  preferred_name: string;
  job_title: string;
  org_unit_id: string;
  office_id: string;
  manager_id: string;
  manager_name: string;
  work_email: string;
  work_phone: string;
  employment_type: "fte" | "contractor" | "intern";
  hire_date: string;
};

const EMPTY_FORM: FormState = {
  full_name: "", preferred_name: "", job_title: "", org_unit_id: "", office_id: "",
  manager_id: "", manager_name: "", work_email: "", work_phone: "", employment_type: "fte", hire_date: "",
};

function unitLabel(unit: OrgUnitOut): string {
  return `${unit.name} — ${unit.unit_type}`;
}

export function PeopleAdminPage({ identity, viewMode }: { identity: Identity; viewMode: ViewMode }) {
  const [orgUnits, setOrgUnits] = useState<OrgUnitOut[] | null>(null);
  const [offices, setOffices] = useState<OfficeOut[] | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<{ id: string; full_name: string } | null>(null);

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
      if (form.work_phone.trim()) fields.work_phone = form.work_phone.trim();
      if (form.hire_date) fields.hire_date = form.hire_date;

      const result = await createEmployee(identity, fields, viewMode);
      setCreated({ id: result.id, full_name: result.full_name });
      setForm(EMPTY_FORM);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't create this employee — try again.");
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
          birth, cost centre, and so on — can be filled in from their profile afterward.
        </p>

        {created && (
          <p className="review-upload-summary">
            <strong>{created.full_name}</strong> was created.
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
        </div>

        <div className="bio-actions" style={{ marginTop: 16 }}>
          <button className="btn btn-primary" disabled={saving} onClick={handleSubmit}>
            {saving ? "Creating…" : "Create employee"}
          </button>
        </div>
      </section>
    </div>
  );
}
