import { useRef, useState } from "react";
import { ApiError, buildTeam } from "../api";
import type { CandidateMatch, Identity, TeamPlanInput, TeamProposal, ViewMode } from "../types";
import { ProposedTeamGraph } from "./graphs/ProposedTeamGraph";
import { AlertCircle, Check, Loader, Sparkles, X } from "../icons";
import { avatarStyle } from "../avatarHue";
import { initials } from "./graphs/treeShared";

/** Build Team mode on the Graph page.
 *
 *  The whole flow is one POST and one piece of state. Replacement re-posts
 *  the same brief with {roleIndex: employeeId} rather than patching the
 *  proposal client-side, because coverage, gaps and concentration all move
 *  when a person changes and recomputing two of the three in the browser is
 *  how a screen ends up showing a percentage that no longer matches the
 *  team under it. The server owns every number; this component owns none.
 */

const EXAMPLE =
  "Build a team for an Azure cloud migration requiring strong Azure, Terraform, " +
  "data engineering, and cloud security expertise.";

/** The builder's state, held by the PAGE rather than by this component.
 *
 *  Lifted because GraphPage unmounts Build Team whenever you switch to
 *  Current Hierarchy, and a generated team is expensive enough -- a model
 *  call and a full re-rank -- that losing it on a glance at the org chart
 *  is the wrong trade. Comparing the proposal against the real hierarchy is
 *  a thing someone will obviously want to do.
 */
export function useTeamBuilderState() {
  const [brief, setBrief] = useState("");
  const [constraints, setConstraints] = useState("");
  const [proposal, setProposal] = useState<TeamProposal | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [replacing, setReplacing] = useState<number | null>(null);
  const assignments = useRef<Record<number, string>>({});
  const abort = useRef<AbortController | null>(null);
  return { brief, setBrief, constraints, setConstraints, proposal, setProposal,
           busy, setBusy, error, setError, replacing, setReplacing,
           assignments, abort };
}

export type TeamBuilderState = ReturnType<typeof useTeamBuilderState>;

export function TeamBuilder({
  identity,
  viewMode,
  onOpenProfile,
  state,
}: {
  identity: Identity;
  viewMode: ViewMode;
  onOpenProfile: (id: string, name: string) => void;
  state: TeamBuilderState;
}) {
  const {
    brief, setBrief, constraints, setConstraints, proposal, setProposal,
    busy, setBusy, error, setError, replacing, setReplacing, assignments, abort,
  } = state;

  async function run(nextAssignments: Record<number, string>, keepPlan: TeamPlanInput | null) {
    const text = brief.trim();
    if (!text || busy) return;
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;

    setBusy(true);
    setError(null);
    try {
      const result = await buildTeam(identity, viewMode, text,
        {
          constraints: constraints.trim(),
          assignments: nextAssignments,
          ...(keepPlan ? { plan: keepPlan } : {}),
        }, controller.signal);
      if (controller.signal.aborted) return;
      setProposal(result);
      setReplacing(null);
    } catch (e) {
      if (controller.signal.aborted) return;
      setError(
        e instanceof ApiError && e.status === 403
          ? "Team Builder is for HR and for managers with direct reports."
          : "Could not build a team just now. Try again.",
      );
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  function generate() {
    // A new brief starts from a clean slate -- keeping pins from the last
    // project would silently staff someone onto work nobody put them on --
    // and with no plan, so the server plans this brief fresh.
    assignments.current = {};
    void run({}, null);
  }

  function replaceWith(roleIndex: number, employeeId: string) {
    assignments.current = { ...assignments.current, [roleIndex]: employeeId };
    // Send the CURRENT plan back. Replacing a person must not re-plan the
    // project: the roles have to be the same roles, or the swap silently
    // restructures the team around the person you were trying to change.
    const plan: TeamPlanInput | null = proposal
      ? {
          project_type: proposal.project_type,
          roles: proposal.roles.map((r) => ({
            role: r.role,
            required_skills: r.required_skills,
          })),
        }
      : null;
    void run(assignments.current, plan);
  }

  return (
    <div className="team-builder">
      <div className="tb-form">
        <label className="tb-label" htmlFor="tb-brief">
          Describe the project or team you need to build
        </label>
        <textarea
          id="tb-brief"
          className="tb-input"
          rows={3}
          value={brief}
          placeholder={EXAMPLE}
          onChange={(e) => setBrief(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) generate();
          }}
        />

        <details className="tb-constraints">
          <summary>Constraints (optional)</summary>
          <input
            className="tb-input tb-input-line"
            value={constraints}
            placeholder="Prioritize Expert-level skills. No more than two people from the same department."
            onChange={(e) => setConstraints(e.target.value)}
          />
        </details>

        {/* Shown on the form, before anything has been generated, and again
            at the head of the result. Deliberately not a tooltip and not
            collapsed: the whole risk this addresses is somebody acting on a
            ranked list of colleagues as though it were a staffing decision,
            and a caveat you have to hover to find does not reach them. */}
        <TechnicalOnlyNotice />

        <div className="tb-actions">
          <button className="btn btn-primary" onClick={generate} disabled={!brief.trim() || busy}>
            {busy ? <Loader size={15} /> : <Sparkles size={15} />}
            {busy ? "Generating…" : "Generate Team"}
          </button>
          {!brief.trim() && (
            <button className="tb-example" onClick={() => setBrief(EXAMPLE)}>
              Use an example
            </button>
          )}
        </div>
      </div>

      {error && (
        <p className="tb-error"><AlertCircle size={15} />{error}</p>
      )}

      {busy && !proposal && <div className="skel skel-card" style={{ height: 320 }} />}

      {proposal && (
        <ProposalView
          proposal={proposal}
          busy={busy}
          replacing={replacing}
          onReplaceOpen={setReplacing}
          onReplacePick={replaceWith}
          onOpenProfile={onOpenProfile}
        />
      )}
    </div>
  );
}

/** The standing caveat on every proposed team.
 *
 *  Rendered twice on purpose -- once on the form and once at the head of the
 *  result -- because the two moments are different. On the form it sets
 *  expectations before anyone invests in a brief; on the result it is in
 *  front of the reader at the moment they might act on a list of named
 *  colleagues.
 *
 *  Not a tooltip, not a <details>, not dismissible. The failure this guards
 *  against is someone treating a skills ranking as a staffing decision, and
 *  a warning behind an interaction does not reach the person making it.
 */
function TechnicalOnlyNotice({ prominent = false }: { prominent?: boolean }) {
  return (
    <aside className={`tb-notice ${prominent ? "tb-notice-prominent" : ""}`} role="note">
      <AlertCircle size={15} />
      <div>
        <p className="tb-notice-title">Technical recommendation only</p>
        <p className="tb-notice-body">
          This recommendation is based primarily on technical skills, proficiency,
          and relevant project experience. Team formation involves many other
          factors — including availability, collaboration, communication, business
          needs, project context, and individual preferences — that may not be
          represented here. Do not rely solely on this recommendation when forming
          a team.
        </p>
      </div>
    </aside>
  );
}


function ProposalView({
  proposal,
  busy,
  replacing,
  onReplaceOpen,
  onReplacePick,
  onOpenProfile,
}: {
  proposal: TeamProposal;
  busy: boolean;
  replacing: number | null;
  onReplaceOpen: (i: number | null) => void;
  onReplacePick: (roleIndex: number, employeeId: string) => void;
  onOpenProfile: (id: string, name: string) => void;
}) {
  const { coverage } = proposal;

  if (proposal.roles.length === 0) {
    return (
      <div className="tb-empty">
        <p><strong>No roles could be read from that brief.</strong></p>
        <p>
          Name the skills the work needs — "Azure, Terraform and data engineering" —
          rather than describing the outcome.
        </p>
        {proposal.unrecognised_skills.length > 0 && (
          <p className="tb-unrecognised">
            Not tracked in this directory: {proposal.unrecognised_skills.join(", ")}
          </p>
        )}
      </div>
    );
  }

  return (
    <div className={`tb-result ${busy ? "tb-result-busy" : ""}`}>
      {/* Repeated here rather than relying on the copy up in the form: by
          the time a team is on screen the form has usually been scrolled
          past, and this is the moment the caveat is actually needed. */}
      <TechnicalOnlyNotice prominent />

      {/* Scope next. Which people this was drawn from is the single most
          load-bearing fact on the screen, and it is decided server-side
          from the caller -- never from the brief. */}
      <p className="tb-scope">
        Ranked across <strong>{proposal.candidate_pool_size}</strong> people in{" "}
        <strong>{proposal.scope.label}</strong>
        {proposal.scope.kind === "team" && " — your reporting line"}
      </p>

      {proposal.narrative && (
        <p className="tb-narrative">
          {proposal.narrative}
          {proposal.narrative_source === "derived" && (
            <span className="tb-source" title="Written from the computed figures, not by the model.">
              computed
            </span>
          )}
        </p>
      )}

      <CoveragePanel proposal={proposal} />

      <ProposedTeamGraph
        proposal={proposal}
        onOpenProfile={onOpenProfile}
        onReplace={(i) => onReplaceOpen(replacing === i ? null : i)}
      />

      {replacing !== null && proposal.roles[replacing] && (
        <ReplacePanel
          roleName={proposal.roles[replacing].role}
          alternatives={proposal.roles[replacing].alternatives}
          current={proposal.roles[replacing].candidate}
          onPick={(id) => onReplacePick(replacing, id)}
          onClose={() => onReplaceOpen(null)}
        />
      )}

      <RoleDetail proposal={proposal} onOpenProfile={onOpenProfile} />

      {coverage.risks.length > 0 && (
        <section className="tb-section">
          <h3>Risks</h3>
          <ul className="tb-risks">
            {coverage.risks.map((r) => (
              <li key={r.skill}>
                <AlertCircle size={14} />
                <span>
                  <strong>{r.share_pct}%</strong> of the team's {r.skill} capability sits with{" "}
                  {r.full_name}
                  {r.holder_count === 1 && " — the only holder on this team"}.
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {proposal.unrecognised_skills.length > 0 && (
        <p className="tb-unrecognised">
          Named in the brief but not tracked in this directory:{" "}
          {proposal.unrecognised_skills.join(", ")}. Nobody can be matched on these.
        </p>
      )}
    </div>
  );
}

function CoveragePanel({ proposal }: { proposal: TeamProposal }) {
  const { coverage, constraints } = proposal;
  const filled = proposal.roles.filter((r) => r.candidate).length;

  return (
    <section className="tb-coverage">
      <div className="tb-coverage-headline">
        <div className="tb-gauge">
          <span className="tb-gauge-pct">{coverage.coverage_pct}%</span>
          <span className="tb-gauge-label">Team coverage</span>
          <span className="tb-gauge-bar" aria-hidden="true">
            <span style={{ width: `${Math.min(100, coverage.coverage_pct)}%` }} />
          </span>
        </div>
        <dl className="tb-stats">
          <div><dt>Roles filled</dt><dd>{filled} / {proposal.roles.length}</dd></div>
          <div><dt>Expert</dt><dd>{coverage.level_counts.Expert ?? 0}</dd></div>
          <div><dt>Working</dt><dd>{coverage.level_counts.Working ?? 0}</dd></div>
          <div><dt>Learning</dt><dd>{coverage.level_counts.Learning ?? 0}</dd></div>
        </dl>
      </div>

      <div className="tb-skill-lists">
        {coverage.covered.length > 0 && (
          <div className="tb-skill-group">
            <h4>Covered</h4>
            <ul>
              {coverage.covered.map((s) => (
                <li key={s}><Check size={13} />{s}</li>
              ))}
            </ul>
          </div>
        )}
        {coverage.missing.length > 0 && (
          <div className="tb-skill-group tb-skill-gap">
            <h4>Gaps</h4>
            <ul>
              {coverage.missing.map((s) => (
                <li key={s}><X size={13} />{s}</li>
              ))}
            </ul>
          </div>
        )}
        {/* Held only at Learning: neither covered nor missing, and the one
            state a reader will otherwise assume is fine. */}
        {coverage.skills.some((s) => s.best_level === "Learning") && (
          <div className="tb-skill-group tb-skill-thin">
            <h4>Learning only</h4>
            <ul>
              {coverage.skills.filter((s) => s.best_level === "Learning").map((s) => (
                <li key={s.skill}><AlertCircle size={13} />{s.skill}</li>
              ))}
            </ul>
          </div>
        )}
      </div>

      {constraints.applied && (
        <p className="tb-applied">
          Applied:{" "}
          {[
            constraints.prefer_expert && "prioritising Expert level",
            constraints.minimize_concentration && "spreading skill concentration",
            constraints.max_per_department !== null &&
              `max ${constraints.max_per_department} per department`,
            constraints.prefer_experience_with.length > 0 &&
              `preferring ${constraints.prefer_experience_with.join(", ")} experience`,
          ].filter(Boolean).join(" · ")}
        </p>
      )}
    </section>
  );
}

function RoleDetail({
  proposal,
  onOpenProfile,
}: {
  proposal: TeamProposal;
  onOpenProfile: (id: string, name: string) => void;
}) {
  return (
    <section className="tb-section">
      <h3>Why these people</h3>
      <div className="tb-role-list">
        {proposal.roles.map((role, i) => (
          <article key={i} className="tb-role">
            <header>
              <h4>{role.role}</h4>
              <p className="tb-role-skills">{role.required_skills.join(" · ")}</p>
            </header>
            {role.candidate ? (
              <>
                <button
                  className="tb-role-person"
                  onClick={() => onOpenProfile(role.candidate!.employee_id, role.candidate!.full_name)}
                >
                  <span className="avatar" style={avatarStyle(role.candidate.full_name)} aria-hidden="true">
                    {initials(role.candidate.full_name)}
                  </span>
                  <span>
                    <strong>{role.candidate.full_name}</strong>
                    <em>{role.candidate.job_title} · {role.candidate.org_unit}</em>
                  </span>
                  <span className="tb-role-pct">{role.candidate.match_pct}%</span>
                </button>
                <ul className="tb-why">
                  {role.candidate.explanation.map((line, j) => (
                    <li key={j}>{line}</li>
                  ))}
                </ul>
              </>
            ) : (
              <p className="tb-role-vacant">
                Nobody in {proposal.scope.label} holds any of these skills.
              </p>
            )}
          </article>
        ))}
      </div>
    </section>
  );
}

function ReplacePanel({
  roleName,
  alternatives,
  current,
  onPick,
  onClose,
}: {
  roleName: string;
  alternatives: CandidateMatch[];
  current: CandidateMatch | null;
  onPick: (employeeId: string) => void;
  onClose: () => void;
}) {
  return (
    <section className="tb-replace">
      <header>
        <h3>Replace for {roleName}</h3>
        <button className="tb-replace-close" onClick={onClose} aria-label="Close">
          <X size={15} />
        </button>
      </header>
      {current && (
        <p className="tb-replace-current">
          Currently {current.full_name} at {current.match_pct}%
        </p>
      )}
      {alternatives.length === 0 ? (
        <p className="tb-replace-none">
          No other authorized candidate holds these skills.
        </p>
      ) : (
        <ul className="tb-alts">
          {alternatives.map((alt) => (
            <li key={alt.employee_id}>
              <button onClick={() => onPick(alt.employee_id)}>
                <span className="avatar" style={avatarStyle(alt.full_name)} aria-hidden="true">
                  {initials(alt.full_name)}
                </span>
                <span className="tb-alt-text">
                  <strong>{alt.full_name}</strong>
                  <em>{alt.job_title} · {alt.org_unit}</em>
                  <em className="tb-alt-why">
                    {alt.matched_skills.filter((s) => s.required)
                      .map((s) => `${s.level}: ${s.skill}`).join(" · ") || "No required skills"}
                  </em>
                </span>
                <span className="tb-alt-pct">{alt.match_pct}%</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
