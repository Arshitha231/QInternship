import { useRef, useState } from "react";
import { ApiError, generateWorkforceReport } from "../api";
import type { Identity, ReportEvidence, ReportFinding, ReportSection, ViewMode, WorkforceReport } from "../types";
import { AlertCircle, Award, Briefcase, GraduationCap, SearchIcon, Sparkles, Users } from "../icons";

// ---------------------------------------------------------------------------
// Workforce Intelligence — a question in, an evidence-backed report out.
//
// Deliberately NOT a chat transcript. A chat log invites follow-up chatter
// and buries the answer in scrollback; this renders one report at a time,
// in fixed sections, so the same question always produces the same shape and
// a reader knows where to look. The query box is a control on that report,
// not a conversation.
//
// Findings are click-through into surfaces that ALREADY EXIST: a skill
// opens the dashboard's own SkillDetailModal, a course opens its overdue
// roster (the one with the reminder controls), a department drives the
// scope selector. Nothing here re-renders data the dashboard already knows
// how to show -- the report's job is to tell you which of those to open.
// ---------------------------------------------------------------------------

const EXAMPLES = [
  "Find the biggest skill gaps in my organization",
  "Where are we overly dependent on a small number of experts?",
  "Which departments have the largest training gaps?",
  "Which active projects have skill or staffing gaps?",
];

const SECTION_ICON: Record<string, React.ReactNode> = {
  Strengths: <Award size={15} />,
  "Skill gaps": <AlertCircle size={15} />,
  "Workforce risks": <Users size={15} />,
  "Training insights": <GraduationCap size={15} />,
  "Project coverage": <Briefcase size={15} />,
  Recommendations: <Sparkles size={15} />,
};

const SEVERITY_LABEL: Record<string, string> = {
  high: "Act now", medium: "Watch", low: "Minor", info: "Context",
};

export interface EvidenceHandlers {
  onOpenSkill: (skillId: number) => void;
  onOpenCourse: (courseCode: string) => void;
  onOpenUnit: (orgUnitId: number) => void;
  onOpenProjects: () => void;
}

/** Is there a surface to open for this piece of evidence? Evidence with no
 *  id is still shown -- it is the number behind the claim -- it just isn't
 *  a button, because a control that looks clickable and does nothing is
 *  worse than plain text. */
function target(e: ReportEvidence, h: EvidenceHandlers): (() => void) | undefined {
  if (e.skill_id != null) return () => h.onOpenSkill(e.skill_id!);
  if (e.course_code) return () => h.onOpenCourse(e.course_code!);
  if (e.org_unit_id != null) return () => h.onOpenUnit(e.org_unit_id!);
  if (e.project_id != null) return h.onOpenProjects;
  return undefined;
}

function Finding({ finding, handlers }: { finding: ReportFinding; handlers: EvidenceHandlers }) {
  return (
    <li className={`wi-finding wi-finding-${finding.severity}`}>
      <p className="wi-finding-head">
        <span className="wi-finding-title">{finding.title}</span>
        <span className={`pill severity-${finding.severity === "info" ? "low" : finding.severity}`}>
          {SEVERITY_LABEL[finding.severity]}
        </span>
      </p>
      <p className="wi-finding-detail">{finding.detail}</p>
      {finding.evidence.length > 0 && (
        <ul className="wi-evidence">
          {finding.evidence.map((e, i) => {
            const go = target(e, handlers);
            return (
              <li key={i}>
                {go ? (
                  <button type="button" className="wi-evidence-chip is-link" onClick={go}>
                    {e.label}
                  </button>
                ) : (
                  <span className="wi-evidence-chip">{e.label}</span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </li>
  );
}

function Section({ section, handlers }: { section: ReportSection; handlers: EvidenceHandlers }) {
  if (section.findings.length === 0) return null;
  return (
    <section className="wi-section">
      <h3 className="wi-section-head">
        <span aria-hidden="true">{SECTION_ICON[section.heading]}</span>
        {section.heading}
        <span className="wi-section-count">{section.findings.length}</span>
      </h3>
      <ul className="wi-finding-list">
        {section.findings.map((f, i) => <Finding key={i} finding={f} handlers={handlers} />)}
      </ul>
    </section>
  );
}

export function WorkforceIntelligence({
  identity, viewMode, handlers,
}: {
  identity: Identity;
  viewMode: ViewMode;
  handlers: EvidenceHandlers;
}) {
  const [query, setQuery] = useState("");
  const [report, setReport] = useState<WorkforceReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  function run(q: string) {
    const text = q.trim();
    if (!text) return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    setError(null);
    generateWorkforceReport(identity, text, viewMode, controller.signal)
      .then((r) => {
        setReport(r);
        setLoading(false);
      })
      .catch((e) => {
        if (e instanceof DOMException && e.name === "AbortError") return;
        setError(e instanceof ApiError ? e.message : "Couldn't generate that report.");
        setLoading(false);
      });
  }

  return (
    <section className="card dashboard-card wi">
      <div className="card-head">
        <div>
          <h2>Workforce intelligence</h2>
          <p className="dashboard-card-sub">
            Ask a workforce question and get an evidence-backed report. Scope is your own — the
            same one the dashboard above uses — and every finding links to the data behind it.
          </p>
        </div>
      </div>

      <form
        className="wi-ask"
        onSubmit={(e) => {
          e.preventDefault();
          run(query);
        }}
      >
        <span className="wi-ask-icon" aria-hidden="true"><SearchIcon size={16} /></span>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="e.g. Where are we overly dependent on a small number of experts?"
          aria-label="Workforce question"
          maxLength={500}
        />
        <button className="btn btn-primary" type="submit" disabled={loading || !query.trim()}>
          {loading ? "Generating…" : "Generate report"}
        </button>
      </form>

      {!report && !loading && (
        <ul className="wi-examples">
          {EXAMPLES.map((ex) => (
            <li key={ex}>
              <button type="button" className="suggest-chip" onClick={() => { setQuery(ex); run(ex); }}>
                <span className="suggest-chip-name">{ex}</span>
              </button>
            </li>
          ))}
        </ul>
      )}

      {loading && <div className="skel skel-card" style={{ height: 200 }} />}
      {error && <p className="state-block error" style={{ padding: 16 }}>{error}</p>}

      {report && !loading && (
        <article className="wi-report">
          <header className="wi-report-head">
            <div>
              <h3 className="wi-report-title">{report.title}</h3>
              <p className="muted wi-report-meta">
                {report.scope.label} · {report.scope.headcount} people ·{" "}
                {report.analyses.length} analys{report.analyses.length === 1 ? "is" : "es"}
              </p>
            </div>
          </header>

          <div className="wi-summary">
            <p className="wi-summary-text">{report.executive_summary}</p>
            {/* Provenance on the label, not in a tooltip. A summary a model
                wrote and one a format string wrote are different things to
                trust, and the reader should not have to hover to find out
                which they got. */}
            <p className="wi-summary-source">
              {report.narrative_source === "model"
                ? "Written by a model over the findings below. It sees only these findings — never employee records — and every figure it used was checked back against them."
                : "Assembled from the findings below. No model was involved."}
            </p>
          </div>

          {report.unsupported.length > 0 && (
            <p className="dashboard-note">
              This question also asked for {report.unsupported.join(", ")}, which this build
              doesn't analyse yet — so nothing below covers it.
            </p>
          )}

          <Section section={report.strengths} handlers={handlers} />
          <Section section={report.skill_gaps} handlers={handlers} />
          <Section section={report.risks} handlers={handlers} />
          <Section section={report.training_insights} handlers={handlers} />
          <Section section={report.project_insights} handlers={handlers} />
          <Section section={report.recommendations} handlers={handlers} />

          {report.evidence.length > 0 && (
            <details className="wi-evidence-all">
              <summary>Supporting evidence ({report.evidence.length})</summary>
              <ul className="wi-evidence">
                {report.evidence.map((e, i) => {
                  const go = target(e, handlers);
                  return (
                    <li key={i}>
                      {go ? (
                        <button type="button" className="wi-evidence-chip is-link" onClick={go}>{e.label}</button>
                      ) : (
                        <span className="wi-evidence-chip">{e.label}</span>
                      )}
                    </li>
                  );
                })}
              </ul>
            </details>
          )}

          {report.strengths.findings.length === 0 && report.skill_gaps.findings.length === 0
            && report.risks.findings.length === 0 && report.training_insights.findings.length === 0
            && report.project_insights.findings.length === 0 && (
            <p className="muted">
              Nothing in this scope crossed a threshold worth reporting for that question.
            </p>
          )}
        </article>
      )}
    </section>
  );
}
