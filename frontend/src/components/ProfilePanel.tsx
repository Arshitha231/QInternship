import { useEffect, useState } from "react";
import { ApiError, getOrgChart, getPerson } from "../api";
import type { Identity, OrgChainNode, PersonDetail, SkillOut } from "../types";
import {
  Briefcase, Building, Clock, Mail, MapPin, Phone, Slack, UserReports, Users, X,
} from "../icons";

interface Props {
  personId: string;
  identity: Identity;
  onClose: () => void;
  onNavigate: (id: string) => void;
}

function initials(name: string): string {
  return name.split(" ").map((p) => p[0]).join("").slice(0, 2).toUpperCase();
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

export function ProfilePanel({ personId, identity, onClose, onNavigate }: Props) {
  const [detail, setDetail] = useState<PersonDetail | null | undefined>(undefined);
  const [reports, setReports] = useState<OrgChainNode[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setDetail(undefined);
    setReports([]);
    setError(null);
    Promise.all([getPerson(identity, personId), getOrgChart(identity, personId, "down", 1)])
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
  }, [personId, identity]);

  return (
    <div className="panel-scrim" onClick={onClose}>
      <div className="panel" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal="true">
        <div className="panel-head">
          <strong style={{ fontSize: 14 }}>Profile</strong>
          <button className="panel-close" onClick={onClose} aria-label="Close profile">
            <X />
          </button>
        </div>

        {detail === undefined && (
          <div className="panel-body">
            <div className="skel skel-line" style={{ width: "60%" }} />
            <div className="skel" style={{ height: 76, width: 76, borderRadius: "50%" }} />
            <div className="skel skel-card" />
            <div className="skel skel-card" />
          </div>
        )}

        {detail === null && (
          <div className="state-block error" style={{ padding: "60px 20px" }}>
            <strong>Couldn't load this profile</strong>
            <p>{error ?? "Not found, or not visible to your role."}</p>
          </div>
        )}

        {detail && (
          <div className="panel-body">
            <div className="profile-header">
              <span className="avatar" aria-hidden="true">{initials(detail.full_name)}</span>
              <div style={{ minWidth: 0, flex: 1 }}>
                <div className="p-name-line">
                  <h2 className="p-name">{detail.preferred_name || detail.full_name}</h2>
                </div>
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
                            <> &middot; <button className="link-btn" onClick={() => onNavigate(detail.delegate!.id)}>{detail.delegate.full_name}</button> covering</>
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
                      Reports to: <button className="link-btn" onClick={() => onNavigate(detail.manager!.id)}>{detail.manager.full_name}</button>
                    </li>
                  )}
                  {detail.tenure_band && (
                    <li><Briefcase size={15} /><span>{detail.tenure_band} at Quadrant</span></li>
                  )}
                </ul>
              </div>
            </div>

            {detail.bio && (
              <section className="card">
                <h2>About</h2>
                <p style={{ margin: 0, fontSize: 13.5, color: "var(--muted)" }}>{detail.bio}</p>
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

            {reports.length > 0 && (
              <section className="card">
                <div className="card-head">
                  <h2><Users size={15} style={{ verticalAlign: -2, marginRight: 6 }} />Direct reports</h2>
                  <span className="link-sm" style={{ color: "var(--muted)", fontSize: 12.5 }}>{reports.length}</span>
                </div>
                <ul className="reports-list">
                  {reports.map((r) => (
                    <li key={r.id}>
                      <button onClick={() => onNavigate(r.id)}>
                        <span>{r.full_name}</span>
                        <span className="sub">{r.job_title}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {detail.project_history && detail.project_history.length > 0 && (
              <section className="card">
                <h2>Project history</h2>
                <ul className="timeline">
                  {detail.project_history.map((p, i) => (
                    <li key={i} className={p.current ? "current" : ""}>
                      <p className="job">{p.project_name}</p>
                      <p className="job-meta">
                        {p.role} &middot; {p.project_type} &middot; {p.start_month} &ndash; {p.current ? "Present" : p.end_month}
                      </p>
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
              </ul>
            </section>
          </div>
        )}
      </div>
    </div>
  );
}
