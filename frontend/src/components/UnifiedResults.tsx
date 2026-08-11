import type { PersonSummary, UnifiedSearchResponse } from "../types";
import { PersonCard } from "./PersonCard";
import { AIOverview } from "./AIOverview";
import { SearchIcon } from "../icons";

const STRUCTURED_EXAMPLES = ["Terraform", "Naomi Lewis"];
const QUESTION_EXAMPLES = ["Who could mentor me in Terraform?", "Is there a skill gap in Figma?"];

interface Props {
  loading: boolean;
  error: string | null;
  response: UnifiedSearchResponse | null;
  hasQuery: boolean;
  flashId: string | null;
  onSelect: (id: string, name: string) => void;
  onJumpToCard: (id: string) => void;
  onExampleClick: (text: string) => void;
  onRetry: () => void;
}

export function UnifiedResults({
  loading, error, response, hasQuery, flashId, onSelect, onJumpToCard, onExampleClick, onRetry,
}: Props) {
  if (error) {
    return (
      <div className="state-block error">
        <strong>Couldn't reach the directory</strong>
        <p>{error}</p>
        <button type="button" className="btn" onClick={onRetry}>Try again</button>
      </div>
    );
  }

  if (loading) {
    // No overview skeleton here on purpose -- mode isn't known until the
    // response actually arrives, so showing one would flash AI chrome on
    // a query that turns out to be direct.
    return (
      <div className="results-grid" aria-busy="true" aria-label="Loading results">
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="skel skel-card" />
        ))}
      </div>
    );
  }

  if (!hasQuery || response === null) {
    return (
      <div className="state-block">
        <SearchIcon size={28} />
        <strong>Search the directory</strong>
        <p>Search by name, skill, or team — or ask a real question about people, teams, or projects.</p>
        <div className="empty-example-groups">
          <div className="empty-example-group">
            <p className="skill-label">Try a name or skill</p>
            <div className="ask-example-chips">
              {STRUCTURED_EXAMPLES.map((ex) => (
                <button key={ex} type="button" className="ask-example-chip" onClick={() => onExampleClick(ex)}>
                  {ex}
                </button>
              ))}
            </div>
          </div>
          <div className="empty-example-group">
            <p className="skill-label">Or ask a question</p>
            <div className="ask-example-chips">
              {QUESTION_EXAMPLES.map((ex) => (
                <button key={ex} type="button" className="ask-example-chip" onClick={() => onExampleClick(ex)}>
                  {ex}
                </button>
              ))}
            </div>
          </div>
        </div>
      </div>
    );
  }

  const { mode, results, overview } = response;

  // Truly nothing happened -- only possible in direct mode. Assisted mode
  // always has something to show (the overview's answer), even when
  // there are no person cards to go with it (skill_gap, skill_scarcity).
  if (mode === "direct" && results.length === 0) {
    return (
      <div className="state-block">
        <SearchIcon size={28} />
        <strong>No results</strong>
        <p>
          Nobody matched — or matches exist but aren't visible to your role. The directory shows both cases
          identically on purpose, so a permission boundary is never revealed by what's absent.
        </p>
      </div>
    );
  }

  // Cited people first -- already true by construction on the backend
  // (citations are built directly from the same list as results), this is
  // just a defensive guarantee at render time, not a re-derivation.
  const citedIds = new Set((overview?.citations ?? []).map((c) => c.id));
  const orderedResults = [...results].sort((a, b) => Number(citedIds.has(b.id)) - Number(citedIds.has(a.id)));

  return (
    <>
      {mode === "assisted" && overview && <AIOverview overview={overview} onJumpToCard={onJumpToCard} />}

      {results.length > 0 ? (
        <>
          <p className="result-count">
            {results.length} {results.length === 1 ? "person" : "people"}
            {mode === "direct" && (
              <span className="direct-mode-note"> · answered directly from the directory, no AI involved</span>
            )}
          </p>
          <div className="results-grid">
            {orderedResults.map((p: PersonSummary) => (
              <PersonCard
                key={p.id}
                person={p}
                id={`person-card-${p.id}`}
                flash={flashId === p.id}
                onClick={() => onSelect(p.id, p.full_name)}
              />
            ))}
          </div>
        </>
      ) : (
        <p className="overview-note">No specific people to show for this — see the summary above.</p>
      )}
    </>
  );
}
