import type { ReactNode } from "react";
import type { PersonSummary, UnifiedSearchResponse } from "../types";
import { PersonCard } from "./PersonCard";
import { AIOverview } from "./AIOverview";
import { AgentGallery } from "./AgentGallery";
import { SearchIcon } from "../icons";

const EMPTY_RESULT_EXAMPLES = [
  "Who reports to Sean Wilson?",
  "Who does Diego Hernandez report to?",
  "Brief me on Diego Hernandez",
];

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
  /** Assisted mode: sit follow-up ask under the overview, before people cards. */
  afterOverview?: ReactNode;
}

export function UnifiedResults({
  loading, error, response, hasQuery, flashId, onSelect, onJumpToCard, onExampleClick, onRetry,
  afterOverview,
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
    return (
      <div className="results-grid" aria-busy="true" aria-label="Loading results">
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="skel skel-card" />
        ))}
      </div>
    );
  }

  if (!hasQuery || response === null) {
    return <AgentGallery onPick={onExampleClick} />;
  }

  const { mode, results, overview, note } = response;

  if (mode === "direct" && results.length === 0) {
    return (
      <div className="state-block">
        <SearchIcon size={28} />
        <strong>No results</strong>
        {note && <p className="overview-note">{note}</p>}
        <p>
          Nobody matched — or matches exist but aren't visible to your role. The directory shows both cases
          identically on purpose, so a permission boundary is never revealed by what's absent.
        </p>
      </div>
    );
  }

  const citedIds = new Set((overview?.citations ?? []).map((c) => c.id));
  const orderedResults = [...results].sort((a, b) => Number(citedIds.has(b.id)) - Number(citedIds.has(a.id)));
  const showAssistedEmptyState = mode === "assisted" && !!overview && results.length === 0;

  return (
    <>
      {mode === "assisted" && overview ? (
        <div className="assisted-answer">
          <AIOverview overview={overview} onJumpToCard={onJumpToCard} />
          {afterOverview}
        </div>
      ) : null}
      {mode === "direct" && note && <p className="overview-note">{note}</p>}

      {showAssistedEmptyState ? (
        <section className="search-empty-state" aria-live="polite">
          <p className="search-empty-state__body">
            Try a full name, check the spelling, or pick a suggestion below.
          </p>
          <div className="search-empty-state__actions">
            <p className="search-empty-state__label">Try one of these</p>
            <div className="ask-example-chips search-empty-state__chips">
              {EMPTY_RESULT_EXAMPLES.map((ex) => (
                <button key={ex} type="button" className="ask-example-chip" onClick={() => onExampleClick(ex)}>
                  {ex}
                </button>
              ))}
            </div>
          </div>
        </section>
      ) : results.length > 0 ? (
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
      ) : null}
    </>
  );
}
