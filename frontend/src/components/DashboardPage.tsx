import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from "react";
import {
  findPeople, getDashboardOverview, getOrgUnitOptions, getProjectCoverage, getSkillSupplyDemand,
  getTrainingAnalytics, getTrainingRoster, getWorkforceInsights, sendCourseReminders,
  type DashboardScopeParams,
} from "../api";
import type {
  DashboardOverview, Identity, InsightReport, OrgUnitOption, ProjectCoverage, ReminderResult,
  SkillSupplyDemand, TrainingAnalytics, TrainingPersonRow, TrainingRoster, ViewMode,
} from "../types";
import {
  AlertCircle, Award, Briefcase, Check, ChevronDown, Clock, GraduationCap, Network,
  Send, Sparkles, Users,
} from "../icons";
import { MetricCards, type Metric } from "./MetricCards";
import { BarRow, ChartLegend, DonutChart, StackedBar, type Slice } from "./charts/DonutChart";
import { CapacityGauge, CapacityGaugeKey, LevelStrip } from "./charts/CapacityGauge";
import {
  BUCKET_COLORS, BUCKET_LABELS, LEVEL_COLORS, VERDICT_COLORS, VERDICT_LABEL, categorical,
} from "./charts/palette";
import { SkillDetailModal } from "./SkillDetailModal";
import { WorkforceIntelligence, type EvidenceHandlers } from "./WorkforceIntelligence";

// ---------------------------------------------------------------------------
// One page, two audiences.
//
// HR and a manager get the same sections in a different order, because they
// are looking for different things first: HR opens this to find where the
// organization is thin, a manager opens it to look at their own team's
// skills. What they must NOT differ in is what the numbers mean -- both
// render the identical payload from the identical endpoints, and the
// narrowing is entirely server-side (app/analytics.py's resolve_scope).
//
// This component therefore never decides who may see what. It decides what
// to OFFER -- the department selector only appears for HR because only HR
// can act on it -- and every response carries the scope the server actually
// used, which is what the header renders. Same division of responsibility
// the rest of this frontend follows.
// ---------------------------------------------------------------------------

type Drill =
  | { kind: "none" }
  | { kind: "training"; bucket: string; course?: string }
  | { kind: "skill"; skillId: number };

const SEVERITY_LABEL: Record<string, string> = { high: "Act now", medium: "Watch", low: "For information" };

const INSIGHT_ICON: Record<string, ReactNode> = {
  skill_shortage: <AlertCircle />,
  skill_concentration: <Users />,
  training_compliance: <GraduationCap />,
  project_staffing_gap: <Briefcase />,
  profile_coverage: <Award />,
  bench_capacity: <Sparkles />,
};

function pct(n: number): string {
  return `${n}%`;
}

export function DashboardPage({
  identity, viewMode, onOpenProfile, onOpenGraph,
}: {
  identity: Identity;
  viewMode: ViewMode;
  onOpenProfile: (id: string, name: string) => void;
  onOpenGraph: (employeeId: string) => void;
}) {
  const isHr = identity.role === "hr" && viewMode === "work";

  // The scope the USER asked for. What the server actually used comes back
  // on every response as `scope` and is what the header renders -- these two
  // are deliberately separate variables, because for a manager they differ.
  const [orgUnitId, setOrgUnitId] = useState<number | null>(null);
  const scope: DashboardScopeParams = useMemo(() => ({ orgUnitId }), [orgUnitId]);

  const [units, setUnits] = useState<OrgUnitOption[]>([]);
  const [overview, setOverview] = useState<DashboardOverview | null>(null);
  const [skills, setSkills] = useState<SkillSupplyDemand[] | null>(null);
  const [training, setTraining] = useState<TrainingAnalytics | null>(null);
  const [projects, setProjects] = useState<ProjectCoverage[] | null>(null);
  const [insights, setInsights] = useState<InsightReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [drill, setDrill] = useState<Drill>({ kind: "none" });
  const [openSkillId, setOpenSkillId] = useState<number | null>(null);

  const skillsRef = useRef<HTMLDivElement>(null);
  const projectsRef = useRef<HTMLDivElement>(null);
  const trainingRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    getOrgUnitOptions(identity, viewMode, controller.signal).then(setUnits).catch(() => setUnits([]));
    return () => controller.abort();
  }, [identity, viewMode]);

  useEffect(() => {
    const controller = new AbortController();
    setOverview(null); setSkills(null); setTraining(null); setProjects(null); setInsights(null);
    setError(null);
    setDrill({ kind: "none" });

    Promise.all([
      getDashboardOverview(identity, viewMode, scope, controller.signal).then(setOverview),
      getSkillSupplyDemand(identity, viewMode, scope, { limit: 120 }, controller.signal).then(setSkills),
      getTrainingAnalytics(identity, viewMode, scope, {}, controller.signal).then(setTraining),
      getProjectCoverage(identity, viewMode, scope, controller.signal).then(setProjects),
      getWorkforceInsights(identity, viewMode, scope, controller.signal).then(setInsights),
    ]).catch((e) => {
      if (e instanceof DOMException && e.name === "AbortError") return;
      setError(e?.message ?? "Couldn't load the dashboard.");
    });
    return () => controller.abort();
  }, [identity, viewMode, scope]);

  const scrollTo = useCallback((ref: RefObject<HTMLDivElement | null>) => {
    ref.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, []);

  // What a report finding opens. Every one of these targets a surface this
  // page already owns, which is the whole point: the report says where to
  // look, the dashboard does the looking.
  const reportHandlers: EvidenceHandlers = useMemo(() => ({
    onOpenSkill: (skillId) => setOpenSkillId(skillId),
    onOpenCourse: (courseCode) => {
      setDrill({ kind: "training", bucket: "overdue", course: courseCode });
      scrollTo(trainingRef);
    },
    // Only HR can act on this -- a manager's scope is pinned to their own
    // line, so re-scoping would be a control that silently does nothing.
    onOpenUnit: (orgUnitId) => {
      if (isHr) setOrgUnitId(orgUnitId);
    },
    onOpenProjects: () => scrollTo(projectsRef),
  }), [scrollTo, isHr]);

  // "Open in graph" needs a PERSON to centre on -- the graph is a people
  // graph, and org units aren't nodes in it. A team scope already names its
  // manager; a department scope resolves to somebody in that department.
  const openGraphForScope = useCallback(async () => {
    if (overview?.scope.manager_id) {
      onOpenGraph(overview.scope.manager_id);
      return;
    }
    const unitName = overview?.scope.org_unit;
    if (!unitName) {
      onOpenGraph(identity.id);
      return;
    }
    const people = await findPeople(identity, { org_unit: unitName }, viewMode).catch(() => []);
    onOpenGraph(people[0]?.id ?? identity.id);
  }, [overview, identity, viewMode, onOpenGraph]);

  if (error) {
    return (
      <div className="card">
        <p className="muted">{error}</p>
      </div>
    );
  }

  const resolved = overview?.scope;

  return (
    <div className="dashboard">
      <header className="dashboard-head">
        <div className="dashboard-head-who">
          <h1 className="dashboard-title">{isHr ? "Workforce dashboard" : "Team dashboard"}</h1>
          <p className="dashboard-scope">
            {resolved ? resolved.label : "…"}
            {resolved && <span className="dashboard-scope-count"> · {resolved.headcount} people</span>}
          </p>
          {resolved?.substituted && (
            <p className="dashboard-substituted">
              This dashboard always shows your own reporting line — the wider selection was not applied.
            </p>
          )}
        </div>

        <div className="dashboard-head-controls">
          {isHr && (
            <label className="dashboard-unit-picker">
              <span className="visually-hidden">Department</span>
              <select
                value={orgUnitId ?? ""}
                onChange={(e) => setOrgUnitId(e.target.value === "" ? null : Number(e.target.value))}
              >
                <option value="">Organization-wide</option>
                {units.map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.unit_type === "team" ? "  " : ""}{u.name} ({u.headcount})
                  </option>
                ))}
              </select>
              <ChevronDown />
            </label>
          )}
          <button className="btn" onClick={openGraphForScope} disabled={!overview}>
            <Network /> Open in graph
          </button>
        </div>
      </header>

      {!overview ? (
        <div className="metric-cards">
          {[0, 1, 2, 3, 4].map((i) => <div key={i} className="skel skel-card" style={{ height: 96 }} />)}
        </div>
      ) : (
        <HeadlineMetrics
          overview={overview}
          isHr={isHr}
          onDrillSkills={() => scrollTo(skillsRef)}
          onDrillProjects={() => scrollTo(projectsRef)}
          onDrillTraining={() => {
            setDrill({ kind: "training", bucket: "incomplete" });
            scrollTo(trainingRef);
          }}
        />
      )}

      {/* One column, full width. A two-column grid looked tidier in the
          abstract and was wrong in practice: these cards differ in height by
          a factor of four (a donut against a fourteen-row list), so the
          short one left most of a screen of dead space beside the tall one.
          Every card here is content-rich enough to use the full width, and
          the ones that would otherwise be too sparse carry a second panel
          instead of empty space. */}
      <div className="dashboard-stack">
        {/* Report first: it is the "what should I look at" surface, and the
            cards below are what it links INTO. Findings drive the same
            skill modal, training drill-down and scope selector the rest of
            this page already owns -- the report never re-renders data the
            dashboard can already show. */}
        <WorkforceIntelligence
          identity={identity}
          viewMode={viewMode}
          handlers={reportHandlers}
        />

        <TeamSkillsCard
          skills={skills}
          onOpenSkill={setOpenSkillId}
          headcount={resolved?.headcount ?? 0}
        />

        <div ref={skillsRef}>
          <SupplyDemandCard
            skills={skills}
            onOpenSkill={setOpenSkillId}
          />
        </div>

        <div ref={trainingRef}>
          <TrainingCard
            identity={identity}
            viewMode={viewMode}
            scope={scope}
            training={training}
            drill={drill.kind === "training" ? drill : null}
            onDrill={(bucket, course) => setDrill({ kind: "training", bucket, course })}
            onCloseDrill={() => setDrill({ kind: "none" })}
            onOpenProfile={onOpenProfile}
          />
        </div>

        <div ref={projectsRef}>
          <ProjectCoverageCard projects={projects} />
        </div>

        <InsightsCard
          insights={insights}
          isHr={isHr}
          onOpenSkill={setOpenSkillId}
          onGoToTraining={() => {
            setDrill({ kind: "training", bucket: "overdue" });
            scrollTo(trainingRef);
          }}
        />
      </div>

      {openSkillId !== null && (
        <SkillDetailModal
          identity={identity}
          viewMode={viewMode}
          scope={scope}
          skillId={openSkillId}
          onClose={() => setOpenSkillId(null)}
          onOpenProfile={onOpenProfile}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Headline row
// ---------------------------------------------------------------------------

function HeadlineMetrics({
  overview, isHr, onDrillSkills, onDrillProjects, onDrillTraining,
}: {
  overview: DashboardOverview;
  isHr: boolean;
  onDrillSkills: () => void;
  onDrillProjects: () => void;
  onDrillTraining: () => void;
}) {
  const t = overview.training;
  const metrics: Metric[] = [
    {
      id: "headcount",
      icon: <Users />,
      value: overview.headcount,
      label: isHr ? "Employees" : "People in your team",
      note: isHr
        ? `${overview.department_count} departments`
        : overview.manager_count !== null
          ? `${overview.manager_count} managers`
          : undefined,
    },
    {
      id: "projects",
      icon: <Briefcase />,
      value: overview.active_project_count,
      label: "Active projects",
      note: overview.client_engagement_count > 0 ? `${overview.client_engagement_count} client` : undefined,
      onClick: onDrillProjects,
    },
    {
      id: "skills",
      icon: <Award />,
      value: overview.skill_count,
      label: "Distinct skills",
      note: `${overview.avg_skills_per_person} avg per person`,
      onClick: onDrillSkills,
    },
    {
      id: "shortage",
      icon: <AlertCircle />,
      value: overview.understaffed_skill_count,
      label: "Understaffed skills",
      note: overview.single_point_skill_count > 0 ? `${overview.single_point_skill_count} on one person` : "none on one person",
      tone: overview.understaffed_skill_count > 0 ? "high" : "low",
      onClick: onDrillSkills,
    },
    {
      id: "compliance",
      icon: <GraduationCap />,
      value: pct(t.compliance_pct),
      label: "Course compliance",
      note: t.overdue > 0 ? `${t.overdue} overdue` : `${t.due_soon} due soon`,
      tone: t.overdue > 0 ? "high" : t.due_soon > 0 ? "medium" : "low",
      onClick: onDrillTraining,
    },
  ];
  return <MetricCards metrics={metrics} />;
}

// ---------------------------------------------------------------------------
// Team skills donut
// ---------------------------------------------------------------------------

function TeamSkillsCard({
  skills, onOpenSkill, headcount,
}: {
  skills: SkillSupplyDemand[] | null;
  onOpenSkill: (id: number) => void;
  headcount: number;
}) {
  const [mode, setMode] = useState<"level" | "skill">("level");
  const [selected, setSelected] = useState<string | null>(null);

  if (skills === null) {
    return <div className="card"><div className="skel skel-card" style={{ height: 320 }} /></div>;
  }

  const held = skills.filter((s) => s.holder_count > 0);

  // Two ways to slice the same people. "By level" is the maturity question
  // -- how deep is this team overall; "by skill" is the composition question
  // -- what is this team made of. They answer different things, so they are
  // a toggle rather than two cards competing for the same space.
  const levelSlices: Slice[] = [
    { key: "Expert", label: "Expert", value: held.reduce((n, s) => n + s.expert_count, 0), color: LEVEL_COLORS.Expert },
    { key: "Working", label: "Working", value: held.reduce((n, s) => n + s.working_count, 0), color: LEVEL_COLORS.Working },
    { key: "Learning", label: "Learning", value: held.reduce((n, s) => n + s.learning_count, 0), color: LEVEL_COLORS.Learning },
  ];

  // Top six by holders, everything else folded into one slice. Past six a
  // donut stops being readable -- see the palette module's note.
  const ranked = [...held].sort((a, b) => b.holder_count - a.holder_count);
  const top = ranked.slice(0, 6);
  const rest = ranked.slice(6);
  const skillSlices: Slice[] = [
    ...top.map((s, i) => ({
      key: String(s.skill_id), label: s.skill, value: s.holder_count, color: categorical(i),
    })),
    ...(rest.length > 0
      ? [{
          key: "rest",
          label: `${rest.length} other skills`,
          value: rest.reduce((n, s) => n + s.holder_count, 0),
          color: "var(--status-idle)",
        }]
      : []),
  ];

  const slices = mode === "level" ? levelSlices : skillSlices;
  const total = slices.reduce((n, s) => n + s.value, 0);

  return (
    <section className="card dashboard-card">
      <div className="card-head">
        <div>
          <h2>Team skills</h2>
          <p className="dashboard-card-sub">
            {mode === "level"
              ? `${total} skill records across ${headcount} people`
              : "Click a slice for who holds it, the projects it covers, and its risk"}
          </p>
        </div>
        <div className="tabs tabs-sm" role="tablist" aria-label="Team skills view">
          <button role="tab" aria-selected={mode === "level"}
                  className={`tab ${mode === "level" ? "active" : ""}`}
                  onClick={() => { setMode("level"); setSelected(null); }}>
            By level
          </button>
          <button role="tab" aria-selected={mode === "skill"}
                  className={`tab ${mode === "skill" ? "active" : ""}`}
                  onClick={() => { setMode("skill"); setSelected(null); }}>
            By skill
          </button>
        </div>
      </div>

      <div className="team-skills-body">
        <div className="dashboard-donut-wrap">
          <DonutChart
            slices={slices}
            size={210}
            innerRatio={0.58}
            centerValue={mode === "level" ? held.length : total}
            centerLabel={mode === "level" ? "skills" : "people"}
            selectedKey={selected}
            ariaLabel={mode === "level" ? "Team skills by level" : "Team skills by skill"}
            onSelect={(key) => {
              setSelected((cur) => (cur === key ? null : key));
              if (mode === "skill" && key !== "rest") onOpenSkill(Number(key));
            }}
            formatCallout={(s, p) =>
              mode === "level" ? `${s.value} ${s.label} (${Math.round(p)}%)` : `${s.value} (${Math.round(p)}%)`
            }
          />
          <ChartLegend
            slices={slices}
            selectedKey={selected}
            onSelect={(key) => {
              setSelected((cur) => (cur === key ? null : key));
              if (mode === "skill" && key !== "rest") onOpenSkill(Number(key));
            }}
          />
        </div>

        {/* The donut answers "what is the shape of this team"; this answers
            "which skills specifically". Bars rather than more slices because
            past six categories a donut is unreadable, and because the reader
            here is comparing lengths, not reading shares of a whole. */}
        <div className="team-skills-top">
          <p className="skill-label">Most widely held</p>
          <div className="bar-rows">
            {ranked.slice(0, 10).map((s, i) => (
              <BarRow
                key={s.skill_id}
                label={s.skill}
                value={s.holder_count}
                max={ranked[0]?.holder_count ?? 1}
                color={categorical(i)}
                note={`${s.holder_count}`}
                onClick={() => onOpenSkill(s.skill_id)}
                title={`${s.expert_count} Expert, ${s.working_count} Working, ${s.learning_count} Learning`}
              />
            ))}
          </div>
          <p className="dashboard-note dashboard-note-quiet">
            Select any skill for who holds it, the projects it covers, and its risk.
          </p>
        </div>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Supply vs demand
// ---------------------------------------------------------------------------

const VERDICTS = ["understaffed", "healthy", "overrepresented", "unused"] as const;

function SupplyDemandCard({
  skills, onOpenSkill,
}: {
  skills: SkillSupplyDemand[] | null;
  onOpenSkill: (id: number) => void;
}) {
  const [filter, setFilter] = useState<string | null>(null);

  if (skills === null) {
    return <div className="card"><div className="skel skel-card" style={{ height: 320 }} /></div>;
  }

  const counts = Object.fromEntries(
    VERDICTS.map((v) => [v, skills.filter((s) => s.verdict === v).length]),
  ) as Record<string, number>;
  const rows = (filter ? skills.filter((s) => s.verdict === filter) : skills).slice(0, 12);
  const anyInferred = rows.some((r) => r.demand_basis === "inferred");

  return (
    <section className="card dashboard-card" data-help="dashboard-skills">
      <div className="card-head">
        <div>
          <h2>Skill supply vs demand</h2>
          <p className="dashboard-card-sub">
            People who can do it, against active projects that need it
          </p>
        </div>
      </div>

      <div className="verdict-filters">
        <button
          className={`verdict-chip ${filter === null ? "on" : ""}`}
          onClick={() => setFilter(null)}
        >
          All <span>{skills.length}</span>
        </button>
        {VERDICTS.map((v) => (
          <button
            key={v}
            className={`verdict-chip verdict-chip-${v} ${filter === v ? "on" : ""}`}
            onClick={() => setFilter((cur) => (cur === v ? null : v))}
            disabled={counts[v] === 0}
          >
            <i style={{ background: VERDICT_COLORS[v] }} aria-hidden="true" />
            {VERDICT_LABEL[v]} <span>{counts[v]}</span>
          </button>
        ))}
      </div>

      <CapacityGaugeKey />

      {rows.length === 0 ? (
        <p className="muted">Nothing in this band.</p>
      ) : (
        <ul className="gauge-list gauge-list-split">
          {rows.map((s) => (
            <li key={s.skill_id}>
              <button type="button" className="gauge-row" onClick={() => onOpenSkill(s.skill_id)}>
                <span className="gauge-row-head">
                  <span className="gauge-row-name">
                    {s.skill}
                    <LevelStrip
                      expert={s.expert_count}
                      working={s.working_count}
                      learning={s.learning_count}
                    />
                  </span>
                  <span className="gauge-row-tags">
                    {s.single_point_of_failure && (
                      <span className="pill pill-spof" title="One capable person covers every project needing this">
                        Single point
                      </span>
                    )}
                    {s.demand_basis === "inferred" && (
                      <span
                        className="pill pill-inferred"
                        title="No required-skill list recorded for these projects — inferred from what assigned people know, may overcount"
                      >
                        Inferred
                      </span>
                    )}
                    <span className={`pill verdict-${s.verdict}`}>{VERDICT_LABEL[s.verdict]}</span>
                  </span>
                </span>
                <CapacityGauge
                  supply={s.capable_count}
                  demand={s.demand_project_count}
                  color={VERDICT_COLORS[s.verdict]}
                />
              </button>
            </li>
          ))}
        </ul>
      )}

      {anyInferred && (
        <p className="dashboard-note">
          Demand marked <em>inferred</em> is read off what assigned people happen to know, because those
          projects have no recorded required skills — it can overcount. Record required skills on a project
          for an exact figure.
        </p>
      )}
      {rows.length > 0 && (
        <p className="dashboard-note dashboard-note-quiet">
          Showing {rows.length} of {filter ? counts[filter] : skills.length}, widest shortfall first.
          Select a skill for its people, projects and risk.
        </p>
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Training / compliance
// ---------------------------------------------------------------------------

function TrainingCard({
  identity, viewMode, scope, training, drill, onDrill, onCloseDrill, onOpenProfile,
}: {
  identity: Identity;
  viewMode: ViewMode;
  scope: DashboardScopeParams;
  training: TrainingAnalytics | null;
  drill: { bucket: string; course?: string } | null;
  onDrill: (bucket: string, course?: string) => void;
  onCloseDrill: () => void;
  onOpenProfile: (id: string, name: string) => void;
}) {
  const [course, setCourse] = useState<string>("");

  if (training === null) {
    return <div className="card"><div className="skel skel-card" style={{ height: 300 }} /></div>;
  }

  const shown = course
    ? training.by_course.find((c) => c.key === course)?.buckets ?? training.buckets
    : training.buckets;

  const slices: Slice[] = (["completed", "overdue", "due_soon", "outstanding"] as const).map((k) => ({
    key: k,
    label: BUCKET_LABELS[k],
    value: shown[k === "due_soon" ? "due_soon" : k],
    color: BUCKET_COLORS[k],
  }));

  const worstUnits = training.by_unit.filter((u) => u.buckets.incomplete > 0).slice(0, 6);
  const maxUnit = Math.max(1, ...worstUnits.map((u) => u.buckets.incomplete));

  return (
    <section className="card dashboard-card" data-help="dashboard-training">
      <div className="card-head">
        <div>
          <h2>Training &amp; course compliance</h2>
          <p className="dashboard-card-sub">
            {shown.expected} course expectations across {training.employee_count} people ·
            {" "}due soon means within {training.due_soon_days} days
          </p>
        </div>
        <label className="dashboard-unit-picker">
          <span className="visually-hidden">Course</span>
          <select value={course} onChange={(e) => { setCourse(e.target.value); onCloseDrill(); }}>
            <option value="">All courses</option>
            {training.courses.map((c) => (
              <option key={c.key} value={c.key}>{c.label}</option>
            ))}
          </select>
          <ChevronDown />
        </label>
      </div>

      <div className="training-body">
        <div className="dashboard-donut-wrap">
          <DonutChart
            slices={slices}
            size={200}
            innerRatio={0.6}
            centerValue={pct(shown.compliance_pct)}
            centerLabel="complete"
            selectedKey={drill?.bucket === "incomplete" ? null : drill?.bucket ?? null}
            ariaLabel="Course completion"
            onSelect={(key) => onDrill(key, course || undefined)}
          />
          <ChartLegend
            slices={slices}
            selectedKey={drill?.bucket ?? null}
            onSelect={(key) => onDrill(key, course || undefined)}
          />
          <p className="dashboard-note dashboard-note-quiet">
            {training.no_record_count > 0 && (
              <>{training.no_record_count} of these have no reported status and are read as not started. </>
            )}
            {training.no_deadline_count > 0 && (
              <>{training.no_deadline_count} have no deadline recorded and can never be overdue.</>
            )}
          </p>
        </div>

        <div className="training-breakdown">
          <p className="skill-label">Where the outstanding work sits</p>
          {worstUnits.length === 0 ? (
            <p className="muted">Everything expected in this scope is complete.</p>
          ) : (
            <div className="bar-rows">
              {worstUnits.map((u) => (
                <BarRow
                  key={u.key}
                  label={u.label}
                  value={u.buckets.incomplete}
                  max={maxUnit}
                  color={u.buckets.overdue > 0 ? BUCKET_COLORS.overdue : BUCKET_COLORS.due_soon}
                  note={`${u.buckets.incomplete}${u.buckets.overdue > 0 ? ` · ${u.buckets.overdue} overdue` : ""}`}
                  title={`${u.label}: ${u.buckets.completed} complete of ${u.buckets.expected}`}
                />
              ))}
            </div>
          )}

          <div className="training-actions">
            <button className="btn btn-primary" onClick={() => onDrill("overdue", course || undefined)}
                    disabled={shown.overdue === 0}>
              <Clock /> {shown.overdue} overdue
            </button>
            <button className="btn" onClick={() => onDrill("due_soon", course || undefined)}
                    disabled={shown.due_soon === 0}>
              {shown.due_soon} due soon
            </button>
            <button className="btn" onClick={() => onDrill("incomplete", course || undefined)}
                    disabled={shown.incomplete === 0}>
              All {shown.incomplete} incomplete
            </button>
          </div>
        </div>
      </div>

      {drill && (
        <TrainingDrillDown
          identity={identity}
          viewMode={viewMode}
          scope={scope}
          bucket={drill.bucket}
          course={drill.course}
          onClose={onCloseDrill}
          onOpenProfile={onOpenProfile}
        />
      )}
    </section>
  );
}

function TrainingDrillDown({
  identity, viewMode, scope, bucket, course, onClose, onOpenProfile,
}: {
  identity: Identity;
  viewMode: ViewMode;
  scope: DashboardScopeParams;
  bucket: string;
  course?: string;
  onClose: () => void;
  onOpenProfile: (id: string, name: string) => void;
}) {
  const [roster, setRoster] = useState<TrainingRoster | null>(null);
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [sending, setSending] = useState(false);
  const [result, setResult] = useState<ReminderResult | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setRoster(null); setChosen(new Set()); setResult(null);
    getTrainingRoster(identity, viewMode, scope, { bucket, course, limit: 500 }, controller.signal)
      .then(setRoster)
      .catch(() => setRoster(null));
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [identity, viewMode, scope.orgUnitId, scope.managerId, bucket, course]);

  // Selection is by PERSON, but a person can appear on several rows (one per
  // outstanding course). Reminding them means reminding them about each --
  // which is what the backend does with no course filter -- so the checkbox
  // belongs to the person, not the row.
  //
  // Keyed on `roster`, not on a `roster?.rows ?? []` local: the fallback
  // array is a fresh identity every render, so the memo never held.
  const people = useMemo(() => {
    const seen = new Map<string, TrainingPersonRow[]>();
    for (const r of roster?.rows ?? []) {
      const list = seen.get(r.employee_id) ?? [];
      list.push(r);
      seen.set(r.employee_id, list);
    }
    return [...seen.entries()];
  }, [roster]);
  const rowCount = roster?.rows.length ?? 0;

  const allSelected = people.length > 0 && chosen.size === people.length;
  const canRemind = bucket !== "completed";

  async function send() {
    setSending(true);
    try {
      const res = await sendCourseReminders(identity, viewMode, scope, [...chosen], course);
      setResult(res);
      setChosen(new Set());
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="training-drill">
      <div className="training-drill-head">
        <p className="skill-label">
          {BUCKET_LABELS[bucket] ?? "Incomplete"}
          {roster && <span className="muted"> · {people.length} people, {roster.total} course records</span>}
        </p>
        <button className="btn btn-quiet" onClick={onClose}>Close</button>
      </div>

      {roster === null ? (
        <div className="skel skel-card" style={{ height: 160 }} />
      ) : people.length === 0 ? (
        <p className="muted">Nobody in this bucket.</p>
      ) : (
        <>
          {canRemind && (
            <div className="training-drill-actions">
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={allSelected}
                  onChange={() =>
                    setChosen(allSelected ? new Set() : new Set(people.map(([id]) => id)))
                  }
                />
                Select all {people.length}
              </label>
              <button
                className="btn btn-primary"
                disabled={chosen.size === 0 || sending}
                onClick={send}
              >
                <Send /> {sending ? "Sending…" : `Send reminder to ${chosen.size}`}
              </button>
              {result && (
                <span className="training-drill-result">
                  <Check /> Sent {result.sent} reminder{result.sent === 1 ? "" : "s"} to{" "}
                  {result.recipients_notified} {result.recipients_notified === 1 ? "person" : "people"}
                  {result.skipped > 0 && <span className="muted"> · {result.skipped} skipped ({result.detail})</span>}
                  {result.out_of_scope > 0 && <span className="muted"> · {result.out_of_scope} outside your scope</span>}
                </span>
              )}
            </div>
          )}

          <div className="training-drill-scroll">
            <table className="dashboard-table">
              <thead>
                <tr>
                  {canRemind && <th className="chk" />}
                  <th>Name</th>
                  <th>Department</th>
                  <th>Course</th>
                  <th>Due</th>
                </tr>
              </thead>
              <tbody>
                {people.map(([employeeId, personRows]) => {
                  const first = personRows[0];
                  return personRows.map((r, i) => (
                    <tr key={`${employeeId}-${r.course_code}`}>
                      {canRemind && (
                        <td className="chk">
                          {i === 0 && (
                            <input
                              type="checkbox"
                              aria-label={`Select ${first.full_name}`}
                              checked={chosen.has(employeeId)}
                              onChange={() =>
                                setChosen((cur) => {
                                  const next = new Set(cur);
                                  if (next.has(employeeId)) next.delete(employeeId);
                                  else next.add(employeeId);
                                  return next;
                                })
                              }
                            />
                          )}
                        </td>
                      )}
                      <td>
                        {i === 0 ? (
                          <button className="linklike" onClick={() => onOpenProfile(employeeId, first.full_name)}>
                            {first.full_name}
                          </button>
                        ) : (
                          <span className="training-drill-cont" aria-hidden="true">↳</span>
                        )}
                        {i === 0 && <span className="training-drill-role">{first.job_title}</span>}
                      </td>
                      <td>{i === 0 ? first.org_unit : ""}</td>
                      <td>{r.course_name}</td>
                      <td>
                        {r.due_on ?? <span className="muted">no deadline</span>}
                        {r.days_overdue !== null && (
                          <span className="pill pill-overdue">{r.days_overdue}d late</span>
                        )}
                      </td>
                    </tr>
                  ));
                })}
              </tbody>
            </table>
          </div>
          {roster.truncated && (
            <p className="dashboard-note dashboard-note-quiet">
              Showing the first {rowCount} of {roster.total} records — narrow by course to see the rest.
            </p>
          )}
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Project coverage
// ---------------------------------------------------------------------------

function ProjectCoverageCard({ projects }: { projects: ProjectCoverage[] | null }) {
  const [showAll, setShowAll] = useState(false);

  if (projects === null) {
    return <div className="card"><div className="skel skel-card" style={{ height: 200 }} /></div>;
  }
  if (projects.length === 0) {
    return (
      <section className="card dashboard-card">
        <div className="card-head"><h2>Project coverage</h2></div>
        <p className="muted">No active projects in this scope.</p>
      </section>
    );
  }

  const judged = projects.filter((p) => p.requirements_recorded);
  const unrecorded = projects.length - judged.length;
  const gaps = judged.filter((p) => p.gap_skills.length > 0);
  const rows = showAll ? judged : judged.slice(0, 8);

  return (
    <section className="card dashboard-card" data-help="dashboard-projects">
      <div className="card-head">
        <div>
          <h2>Project coverage &amp; skill risk</h2>
          <p className="dashboard-card-sub">
            {gaps.length} of {judged.length} active projects with recorded requirements are missing a
            skill their current members can cover
          </p>
        </div>
      </div>

      <table className="dashboard-table">
        <thead>
          <tr>
            <th>Project</th>
            <th className="num">People</th>
            <th>Required skills covered</th>
            <th>Gaps</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((p) => (
            <tr key={p.project_id}>
              <td>
                <span className="supply-skill">{p.project_name}</span>
                {p.is_client_engagement && <span className="pill pill-client">Client</span>}
              </td>
              <td className="num">{p.member_count}</td>
              <td className="coverage-cell">
                <StackedBar
                  segments={[
                    { key: "ok", label: "Covered", value: p.covered_skill_count, color: BUCKET_COLORS.completed },
                    {
                      key: "gap", label: "Gap",
                      value: p.required_skill_count - p.covered_skill_count,
                      color: BUCKET_COLORS.overdue,
                    },
                  ]}
                />
                <span className="coverage-num">
                  {p.covered_skill_count}/{p.required_skill_count}
                </span>
              </td>
              <td>
                {p.gap_skills.length > 0 ? (
                  <span className="gap-skills">{p.gap_skills.join(", ")}</span>
                ) : p.single_cover_skills.length > 0 ? (
                  <span className="muted" title={p.single_cover_skills.join(", ")}>
                    covered, {p.single_cover_skills.length} by one person
                  </span>
                ) : (
                  <span className="muted">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="dashboard-card-foot">
        {judged.length > 8 && (
          <button className="btn btn-quiet" onClick={() => setShowAll((v) => !v)}>
            {showAll ? "Show fewer" : `Show all ${judged.length}`}
          </button>
        )}
        {unrecorded > 0 && (
          <p className="dashboard-note">
            {unrecorded} more active project{unrecorded === 1 ? " has" : "s have"} no recorded required
            skills, so coverage can't be judged for {unrecorded === 1 ? "it" : "them"}. Recording required
            skills on a project is what turns this from a guess into a measurement.
          </p>
        )}
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Insights
// ---------------------------------------------------------------------------

function InsightsCard({
  insights, isHr, onOpenSkill, onGoToTraining,
}: {
  insights: InsightReport | null;
  isHr: boolean;
  onOpenSkill: (id: number) => void;
  onGoToTraining: () => void;
}) {
  if (insights === null) {
    return <div className="card"><div className="skel skel-card" style={{ height: 200 }} /></div>;
  }

  const { summary, insights: items } = insights;

  return (
    <section className="card dashboard-card" data-help="dashboard-insights">
      <div className="card-head">
        <div>
          <h2>{isHr ? "Workforce risk & insights" : "Team development"}</h2>
          {/* Said plainly rather than dressed up as AI: every FINDING below
              is a rule over data already in this directory, and the counts
              are printed so the claim can be checked. The summary above them
              is the one part a model touches, and it says so. */}
          <p className="dashboard-card-sub">
            Derived from this directory's own skills, projects and training records — each item states
            the figures behind it
          </p>
        </div>
      </div>

      {/* The narrative sits above the cards because its whole job is
          telling you which card to read first. Its provenance is on the
          label, not buried in a tooltip: a summary a model wrote and one a
          format string wrote are different things to trust. */}
      <div className="insight-summary">
        <span className="insight-summary-mark" aria-hidden="true"><Sparkles /></span>
        <div>
          <p className="insight-summary-text">{summary.text}</p>
          <p className="insight-summary-source">
            {summary.source === "model"
              ? "Written by a model over the findings below — it sees only these findings, never employee records, and every figure it used was checked back against them."
              : "Assembled from the findings below. No model was involved."}
          </p>
        </div>
      </div>

      {items.length === 0 ? (
        <p className="muted">
          Nothing crosses a threshold in this scope. That is the finding — no items are padded in here to
          fill the section.
        </p>
      ) : (
        <ul className="insight-list">
          {items.map((insight, i) => (
            <li key={`${insight.kind}-${i}`} className={`insight insight-${insight.severity}`}>
              <span className="insight-icon" aria-hidden="true">{INSIGHT_ICON[insight.kind]}</span>
              <div className="insight-body">
                <p className="insight-head">
                  {insight.title}
                  <span className={`pill severity-${insight.severity}`}>{SEVERITY_LABEL[insight.severity]}</span>
                </p>
                <p className="insight-detail">{insight.detail}</p>

                {insight.evidence.length > 0 && (
                  <ul className="insight-evidence">
                    {insight.evidence.map((e, j) => {
                      const skillId = insight.skill_ids[j];
                      return (
                        <li key={j}>
                          {skillId !== undefined ? (
                            <button className="linklike" onClick={() => onOpenSkill(skillId)}>{e}</button>
                          ) : (
                            e
                          )}
                        </li>
                      );
                    })}
                  </ul>
                )}

                {insight.recommendation && (
                  <p className="insight-rec">
                    {insight.recommendation}
                    {insight.kind === "training_compliance" && (
                      <button className="linklike insight-rec-go" onClick={onGoToTraining}>
                        Open the overdue list
                      </button>
                    )}
                  </p>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
