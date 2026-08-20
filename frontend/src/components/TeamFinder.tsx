import { useRef, useState } from "react";
import { ApiError, findTeams } from "../api";
import type { Identity, TeamRecommendation, TeamRecommendationResult, ViewMode } from "../types";
import { AlertCircle, Loader, Mail, Network, SearchIcon, Users } from "../icons";
import { avatarStyle } from "../avatarHue";
import { initials } from "./graphs/treeShared";

/** Find the Right Team mode on the Graph page.
 *
 *  Answers a different question from the two features either side of it, and
 *  the copy works hard to keep them apart, because three search boxes that
 *  look alike is how a user ends up in the wrong one:
 *
 *      Find People   "who can help me?"       -> the top search bar
 *      Build Team    "who would I staff?"     -> Build Team mode
 *      Find a Team   "who do I go and ask?"   -> here
 *
 *  Every number rendered here was computed server-side over the employees
 *  this caller is permitted to discover. Nothing is recalculated in the
 *  browser, so nothing on screen can drift from what the permission layer
 *  actually allowed.
 */

const EXAMPLES = [
  "Which team has the strongest Kubernetes expertise?",
  "Which team should I talk to about an Azure networking problem?",
  "Which department has the most data engineering experience?",
];

export function useTeamFinderState() {
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<TeamRecommendationResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);
  return { query, setQuery, result, setResult, busy, setBusy, error, setError, abort };
}

export type TeamFinderState = ReturnType<typeof useTeamFinderState>;

export function TeamFinder({
  identity,
  viewMode,
  state,
  onViewTeamGraph,
}: {
  identity: Identity;
  viewMode: ViewMode;
  state: TeamFinderState;
  /** Hands the unit's manager back to the page, which re-centres the real
   *  Team graph on them. Reuses the existing hierarchy view rather than
   *  drawing a second one — this feature recommends an EXISTING team, so
   *  the existing picture of it is the correct picture. */
  onViewTeamGraph: (employeeId: string, name: string) => void;
}) {
  const { query, setQuery, result, setResult, busy, setBusy, error, setError, abort } = state;

  async function run() {
    const text = query.trim();
    if (!text || busy) return;
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;
    setBusy(true);
    setError(null);
    try {
      const found = await findTeams(identity, viewMode, text, controller.signal);
      if (controller.signal.aborted) return;
      setResult(found);
    } catch (e) {
      if (controller.signal.aborted) return;
      setError(e instanceof ApiError && e.status === 403
        ? "You do not have access to team search."
        : "Could not search for a team just now. Try again.");
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  return (
    <div className="team-finder">
      <div className="tb-form">
        <label className="tb-label" htmlFor="tf-query">
          Describe the technical problem or capability you need
        </label>
        {/* Says what this is NOT, because the top search bar answers the
            other question and the two are easy to confuse. */}
        <p className="tf-hint">
          Finds an existing team to go and ask. To find an individual, use the
          search bar at the top.
        </p>
        <div className="tf-row">
          <SearchIcon size={16} />
          <input
            id="tf-query"
            className="tb-input tb-input-line tf-query"
            value={query}
            placeholder={EXAMPLES[0]}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") void run(); }}
          />
          <button className="btn btn-primary" onClick={() => void run()} disabled={!query.trim() || busy}>
            {busy ? <Loader size={15} /> : <SearchIcon size={15} />}
            {busy ? "Searching…" : "Find Team"}
          </button>
        </div>
        {!query.trim() && (
          <p className="tf-examples">
            {EXAMPLES.map((ex) => (
              <button key={ex} className="tf-example" onClick={() => setQuery(ex)}>{ex}</button>
            ))}
          </p>
        )}
      </div>

      {error && <p className="tb-error"><AlertCircle size={15} />{error}</p>}
      {busy && !result && <div className="skel skel-card" style={{ height: 220 }} />}

      {result && (
        <div className={`tf-result ${busy ? "tb-result-busy" : ""}`}>
          {result.teams.length === 0 ? (
            <div className="tb-empty">
              <p><strong>No team matched that.</strong></p>
              <p>
                {result.skills.length === 0
                  ? "Name the capability you need — \"Kubernetes\", \"Azure networking\", \"data engineering\"."
                  : `Nobody you can see holds ${result.skills.join(" or ")}.`}
              </p>
              {result.unrecognised_skills.length > 0 && (
                <p className="tb-unrecognised">
                  Not tracked in this directory: {result.unrecognised_skills.join(", ")}
                </p>
              )}
            </div>
          ) : (
            <>
              <p className="tb-scope">
                Read as{" "}
                {result.skills.map((s) => <strong key={s}>{s}</strong>)
                  .reduce<React.ReactNode[]>((acc, el, i) => i === 0 ? [el] : [...acc, ", ", el], [])}
                {result.preferred_unit_type && ` · showing ${result.preferred_unit_type}s first`}
              </p>
              {result.teams.map((team) => (
                <TeamCard key={team.org_unit_id} team={team} onViewTeamGraph={onViewTeamGraph} />
              ))}
              {result.unrecognised_skills.length > 0 && (
                <p className="tb-unrecognised">
                  Named but not tracked here: {result.unrecognised_skills.join(", ")}.
                </p>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

function TeamCard({
  team,
  onViewTeamGraph,
}: {
  team: TeamRecommendation;
  onViewTeamGraph: (employeeId: string, name: string) => void;
}) {
  const held = team.skills.filter((s) => s.total > 0);

  return (
    <article className="tf-card">
      <header className="tf-card-head">
        <div>
          <h3>
            {team.name}
            <span className="tf-unit-type">{team.unit_type}</span>
          </h3>
          <p className="tf-why">{team.why}</p>
        </div>
        <div className="tf-match">
          <span className="tf-match-pct">{team.match_pct}%</span>
          <span className="tf-match-label">match</span>
        </div>
      </header>

      <div className="tf-card-body">
        <div className="tf-skills">
          {held.map((s) => (
            <div key={s.skill} className="tf-skill">
              <p className="tf-skill-name">
                {s.skill}
                <span className="tf-skill-count">
                  {s.total} {s.total === 1 ? "person" : "people"}
                </span>
              </p>
              <p className="tf-levels">
                <span className="lvl lvl-expert">Expert {s.expert}</span>
                <span className="lvl lvl-working">Working {s.working}</span>
                <span className="lvl lvl-learning">Learning {s.learning}</span>
              </p>
            </div>
          ))}
        </div>

        {team.projects.length > 0 && (
          <div className="tf-projects">
            <h4>Relevant current projects</h4>
            <ul>{team.projects.map((p) => <li key={p}>{p}</li>)}</ul>
          </div>
        )}
      </div>

      <footer className="tf-card-foot">
        {team.manager ? (
          <div className="tf-manager">
            <span className="avatar" style={avatarStyle(team.manager.full_name)} aria-hidden="true">
              {initials(team.manager.full_name)}
            </span>
            <span className="tf-manager-text">
              <strong>{team.manager.full_name}</strong>
              <em>{team.manager.job_title}</em>
            </span>
            <a className="tf-contact" href={`mailto:${team.manager.work_email}`}>
              <Mail size={14} />
              {team.manager.work_email}
            </a>
          </div>
        ) : (
          <p className="tf-no-manager">No visible team contact.</p>
        )}

        <span className="tf-headcount">
          <Users size={14} />
          {team.relevant_people} of {team.headcount} relevant
        </span>

        {team.manager && (
          <button
            className="btn tf-graph-btn"
            onClick={() => onViewTeamGraph(team.manager!.employee_id, team.manager!.full_name)}
          >
            <Network size={14} />
            View Team Graph
          </button>
        )}
      </footer>
    </article>
  );
}
