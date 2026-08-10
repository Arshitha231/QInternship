import { useEffect, useState } from "react";
import { TopBar } from "./components/TopBar";
import { Filters } from "./components/Filters";
import { ResultsList } from "./components/ResultsList";
import { ProfilePanel } from "./components/ProfilePanel";
import { GraphPage } from "./components/GraphPage";
import { AskPanel } from "./components/AskPanel";
import { useDebouncedValue } from "./hooks";
import { ApiError, findPeople, type SearchFilters } from "./api";
import { DEV_IDENTITIES } from "./identities";
import type { Identity, PersonSummary } from "./types";

type Mode = "search" | "graphs" | "ask";

export default function App() {
  const [identity, setIdentity] = useState<Identity>(DEV_IDENTITIES[0]);
  const [mode, setMode] = useState<Mode>("search");
  const [query, setQuery] = useState("");
  const [filters, setFilters] = useState<SearchFilters>({});
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [graphFocusId, setGraphFocusId] = useState<string>(DEV_IDENTITIES[0].id);

  const debouncedQuery = useDebouncedValue(query, 300);
  const debouncedFilters = useDebouncedValue(filters, 300);

  const [results, setResults] = useState<PersonSummary[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const hasQuery = debouncedQuery.trim() !== "" || Object.keys(debouncedFilters).length > 0;

  useEffect(() => {
    if (!hasQuery) {
      setResults(null);
      setLoading(false);
      setError(null);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    findPeople(identity, { query: debouncedQuery.trim() || undefined, ...debouncedFilters }, controller.signal)
      .then((people) => {
        setResults(people);
        setLoading(false);
      })
      .catch((e) => {
        if (e instanceof DOMException && e.name === "AbortError") return;
        setError(e instanceof ApiError ? e.message : "Unknown error");
        setLoading(false);
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debouncedQuery, debouncedFilters, identity]);

  return (
    <div className="app">
      <TopBar
        query={query}
        onQueryChange={(q) => {
          setQuery(q);
          if (q) setMode("search");
        }}
        identity={identity}
        onIdentityChange={(next) => {
          setIdentity(next);
          setGraphFocusId(next.id);
        }}
      />

      <div className="tabs" role="tablist" aria-label="Section">
        <button role="tab" aria-selected={mode === "search"} className={`tab ${mode === "search" ? "active" : ""}`} onClick={() => setMode("search")}>
          Search
        </button>
        <button role="tab" aria-selected={mode === "graphs"} className={`tab ${mode === "graphs" ? "active" : ""}`} onClick={() => setMode("graphs")}>
          Graphs
        </button>
        <button role="tab" aria-selected={mode === "ask"} className={`tab ${mode === "ask" ? "active" : ""}`} onClick={() => setMode("ask")}>
          Ask
        </button>
      </div>

      <main className="content">
        {mode === "search" ? (
          <>
            <Filters filters={filters} onChange={setFilters} />
            <ResultsList
              loading={loading}
              error={error}
              results={results}
              hasQuery={hasQuery}
              onSelect={(id) => setSelectedId(id)}
            />
          </>
        ) : mode === "graphs" ? (
          <GraphPage
            identity={identity}
            focusId={graphFocusId}
            onFocusChange={setGraphFocusId}
            onOpenProfile={(id) => setSelectedId(id)}
          />
        ) : (
          <AskPanel identity={identity} onOpenProfile={(id) => setSelectedId(id)} />
        )}
      </main>

      {selectedId && (
        <ProfilePanel
          personId={selectedId}
          identity={identity}
          onClose={() => setSelectedId(null)}
          onNavigate={(id) => setSelectedId(id)}
        />
      )}
    </div>
  );
}
