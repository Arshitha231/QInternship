import { useEffect, useState } from "react";
import { getSkillDetail, type DashboardScopeParams } from "../api";
import type { Identity, SkillDetail, ViewMode } from "../types";
import { AlertCircle, ArrowRight, Briefcase, Users, X } from "../icons";
import { DonutChart, ChartLegend, type Slice } from "./charts/DonutChart";
import { LEVEL_COLORS, VERDICT_LABEL } from "./charts/palette";

// The click-through from a skill slice or a supply/demand row. Everything in
// here comes from one GET /analytics/skills/{id} against the SAME scope the
// chart was drawn in -- a popup that quietly widened to the whole company
// when opened from a manager's team chart would be answering a different
// question than the one clicked.

function levelPillClass(level: string): string {
  return level === "Expert" ? "pill-expert" : level === "Working" ? "pill-working" : "pill-learning";
}

export function SkillDetailModal({
  identity, viewMode, scope, skillId, onClose, onOpenProfile,
}: {
  identity: Identity;
  viewMode: ViewMode;
  scope: DashboardScopeParams;
  skillId: number;
  onClose: () => void;
  onOpenProfile?: (id: string, name: string) => void;
}) {
  const [detail, setDetail] = useState<SkillDetail | null | undefined>(undefined);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setDetail(undefined);
    setFailed(false);
    getSkillDetail(identity, viewMode, skillId, scope, controller.signal)
      .then(setDetail)
      .catch((e) => {
        if (e instanceof DOMException && e.name === "AbortError") return;
        setFailed(true);
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [identity, viewMode, skillId, scope.orgUnitId, scope.managerId]);

  // Escape closes, same as every other overlay in this app.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const levelSlices: Slice[] = detail
    ? [
        { key: "expert", label: "Expert", value: detail.expert_count, color: LEVEL_COLORS.Expert },
        { key: "working", label: "Working", value: detail.working_count, color: LEVEL_COLORS.Working },
        { key: "learning", label: "Learning", value: detail.learning_count, color: LEVEL_COLORS.Learning },
      ]
    : [];

  return (
    <div className="panel-scrim skill-modal-scrim" onClick={onClose} role="presentation">
      <div
        className="skill-modal"
        role="dialog"
        aria-modal="true"
        aria-label={detail ? `${detail.skill} — skill detail` : "Skill detail"}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="panel-head">
          <div>
            <h2 className="skill-modal-title">{detail?.skill ?? "…"}</h2>
            {detail && (
              <p className="skill-modal-sub">
                <span className="skill-modal-category">{detail.category}</span> · {detail.scope.label}
              </p>
            )}
          </div>
          <button className="panel-close" onClick={onClose} aria-label="Close">
            <X />
          </button>
        </div>

        {failed ? (
          <div className="panel-body"><p className="muted">Couldn't load this skill.</p></div>
        ) : detail === undefined ? (
          <div className="panel-body"><div className="skel skel-card" style={{ height: 320 }} /></div>
        ) : detail === null ? (
          <div className="panel-body">
            <p className="muted">Nobody in this scope holds this skill and no active project here needs it.</p>
          </div>
        ) : (
          <div className="panel-body skill-modal-body">
            {/* Level mix, the popup's headline. Counts sit in the callouts
                so the chart is readable without hovering. */}
            <div className="skill-modal-chart">
              <DonutChart
                slices={levelSlices}
                size={190}
                innerRatio={0.6}
                centerValue={detail.holder_count}
                centerLabel={detail.holder_count === 1 ? "person" : "people"}
                ariaLabel={`${detail.skill} by level`}
                formatCallout={(s, pct) => `${s.value} ${s.label} (${Math.round(pct)}%)`}
              />
              <ChartLegend slices={levelSlices} />
            </div>

            <div className="skill-modal-stats">
              <div className="skill-stat">
                <p className="skill-stat-value">{detail.capable_count}</p>
                <p className="skill-stat-label">Capable</p>
                <p className="skill-stat-note">Expert + Working</p>
              </div>
              <div className="skill-stat">
                <p className="skill-stat-value">{detail.coverage_pct}%</p>
                <p className="skill-stat-label">Coverage</p>
                <p className="skill-stat-note">of {detail.scope.headcount} people</p>
              </div>
              <div className="skill-stat">
                <p className="skill-stat-value">{detail.maturity_pct}%</p>
                <p className="skill-stat-label">Maturity</p>
                <p className="skill-stat-note">{detail.maturity_label}</p>
              </div>
              <div className="skill-stat">
                <p className="skill-stat-value">{detail.demand_project_count}</p>
                <p className="skill-stat-label">Active projects</p>
                <p className="skill-stat-note">
                  {detail.supply_per_project !== null ? `${detail.supply_per_project} capable each` : "no demand"}
                </p>
              </div>
            </div>

            {/* The risk line always states the count behind it -- a severity
                with no number is not checkable, so the backend sends both. */}
            <div className={`skill-risk skill-risk-${detail.risk}`}>
              <AlertCircle />
              <div>
                <p className="skill-risk-head">
                  {detail.risk === "high" ? "High skill risk" : detail.risk === "medium" ? "Watch" : "No risk indicated"}
                  <span className={`pill verdict-${detail.verdict}`}>{VERDICT_LABEL[detail.verdict]}</span>
                </p>
                <p className="skill-risk-why">{detail.risk_reason}</p>
              </div>
            </div>

            <section>
              <p className="skill-label">
                <Briefcase /> Active projects using this skill
              </p>
              {detail.projects.length === 0 ? (
                <p className="muted">No active project in this scope depends on it.</p>
              ) : (
                <ul className="skill-modal-projects">
                  {detail.projects.map((p) => (
                    <li key={p.project_id}>
                      <span className="skill-modal-project-name">{p.project_name}</span>
                      <span className="skill-modal-project-meta">
                        {p.capable_member_count === 0 ? (
                          <span className="pill pill-gap">No capable member</span>
                        ) : (
                          <>{p.capable_member_count} of {p.member_count} capable</>
                        )}
                        <span
                          className={`pill continuity-source-${p.basis}`}
                          title={
                            p.basis === "declared"
                              ? "Recorded as a required skill for this project"
                              : "No required skills recorded for this project — inferred from what its members know, may overcount"
                          }
                        >
                          {p.basis === "declared" ? "Declared" : "Inferred"}
                        </span>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section>
              <p className="skill-label">
                <Users /> People with this skill
                {detail.holders_truncated && <span className="muted"> (first {detail.holders.length})</span>}
              </p>
              <ul className="skill-modal-holders">
                {detail.holders.map((h) => (
                  <li key={h.id}>
                    <button
                      type="button"
                      className="skill-modal-holder"
                      onClick={() => onOpenProfile?.(h.id, h.full_name)}
                      disabled={!onOpenProfile}
                    >
                      <span className="skill-modal-holder-who">
                        <span className="skill-modal-holder-name">{h.full_name}</span>
                        <span className="skill-modal-holder-role">{h.job_title} · {h.org_unit}</span>
                      </span>
                      <span className={`pill ${levelPillClass(h.level)}`}>{h.level}</span>
                      {onOpenProfile && <ArrowRight className="skill-modal-holder-go" />}
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          </div>
        )}
      </div>
    </div>
  );
}
