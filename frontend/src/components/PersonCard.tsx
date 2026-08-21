import type { MatchExplanation, PersonSummary } from "../types";
import { Building, MapPin } from "../icons";
import { avatarStyle } from "../avatarHue";

function initials(name: string): string {
  return name.split(" ").map((p) => p[0]).join("").slice(0, 2).toUpperCase();
}

const STATUS_LABEL: Record<string, string> = {
  available: "Available",
  away: "Away",
  restricted: "Restricted",
};

export function PersonCard({
  person, onClick, id, flash,
}: { person: PersonSummary; onClick: () => void; id?: string; flash?: boolean }) {
  const status = person.availability_status;
  return (
    <button id={id} className={`result-card ${flash ? "flash" : ""}`} onClick={onClick}>
      <span className="avatar" style={{ width: 42, height: 42, fontSize: "var(--fs-md)", ...avatarStyle(person.full_name) }} aria-hidden="true">
        {initials(person.full_name)}
      </span>
      <span style={{ minWidth: 0, flex: 1 }}>
        <p className="result-name">{person.preferred_name || person.full_name}</p>
        <p className="result-role">{person.job_title}</p>
        <span className="result-meta">
          <span><Building size={13} />{person.org_unit}</span>
          {person.office && <span><MapPin size={13} />{person.office.city}</span>}
          <span>
            <span className={`status-dot status-${status}`} />
            {STATUS_LABEL[status] ?? status}
          </span>
        </span>
        {person.match && <MatchLine match={person.match} />}
      </span>
    </button>
  );
}

// SEARCH_RANKING_IMPLEMENTATION_PLAN.md step 5. Labeled "Query match", never
// "Rank"/"Score"/"Best", and never phrased against another card -- this
// explains this one person's fit against the query, not a comparison
// (CLAUDE.md §7: the system compares and surfaces, it does not rank people
// against each other). `matched`/`missing` are already-phrased evidence the
// backend built from the same values that produced score_pct -- rendered
// verbatim, never re-templated from raw fields client-side.
function MatchLine({ match }: { match: MatchExplanation }) {
  const evidence = [
    ...match.matched,
    ...match.missing.map((m) => `missing ${m}`),
  ].join(" · ");
  return (
    <p className="result-match">
      <span className="result-match-bar" aria-hidden="true">
        <span className="result-match-bar-fill" style={{ width: `${match.score_pct}%` }} />
      </span>
      <span className="result-match-label">Query match{evidence ? `: ${evidence}` : ""}</span>
    </p>
  );
}
