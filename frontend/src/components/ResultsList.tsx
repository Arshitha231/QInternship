import type { PersonSummary } from "../types";
import { PersonCard } from "./PersonCard";
import { SearchIcon } from "../icons";

interface Props {
  loading: boolean;
  error: string | null;
  results: PersonSummary[] | null;
  hasQuery: boolean;
  onSelect: (id: string) => void;
}

export function ResultsList({ loading, error, results, hasQuery, onSelect }: Props) {
  if (error) {
    return (
      <div className="state-block error">
        <strong>Couldn't reach the directory</strong>
        <p>{error}</p>
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

  if (!hasQuery || results === null) {
    return (
      <div className="state-block">
        <SearchIcon size={28} />
        <strong>Search the directory</strong>
        <p>Try a name, a skill like "Terraform", or a description like "who's good with dashboards in Bangalore".</p>
      </div>
    );
  }

  if (results.length === 0) {
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

  return (
    <>
      <p className="result-count">{results.length} {results.length === 1 ? "person" : "people"}</p>
      <div className="results-grid">
        {results.map((p) => (
          <PersonCard key={p.id} person={p} onClick={() => onSelect(p.id)} />
        ))}
      </div>
    </>
  );
}
