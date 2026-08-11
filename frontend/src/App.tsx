import { useEffect, useState } from "react";
import { TopBar } from "./components/TopBar";
import { Filters } from "./components/Filters";
import { UnifiedResults } from "./components/UnifiedResults";
import { ProfilePanel } from "./components/ProfilePanel";
import { GraphPage } from "./components/GraphPage";
import { useDebouncedValue } from "./hooks";
import { ApiError, unifiedSearch, type SearchFilters } from "./api";
import { DEV_IDENTITIES } from "./identities";
import type { Identity, UnifiedSearchResponse } from "./types";

type Mode = "search" | "graphs";

function initialQuery(): string {
  return new URLSearchParams(window.location.search).get("q") ?? "";
}

export default function App() {
  const [identity, setIdentity] = useState<Identity>(DEV_IDENTITIES[0]);
  const [mode, setMode] = useState<Mode>("search");
  const [query, setQuery] = useState(initialQuery);
  const [filters, setFilters] = useState<SearchFilters>({});
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [graphFocusId, setGraphFocusId] = useState<string>(DEV_IDENTITIES[0].id);
  const [flashId, setFlashId] = useState<string | null>(null);
  const [retryToken, setRetryToken] = useState(0);

  const debouncedQuery = useDebouncedValue(query, 300);
  const debouncedFilters = useDebouncedValue(filters, 300);

  const [response, setResponse] = useState<UnifiedSearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const hasQuery = debouncedQuery.trim() !== "" || Object.keys(debouncedFilters).length > 0;

  // Keeps the URL shareable/bookmarkable (?q=...) without spamming browser
  // history on every keystroke -- replaceState, not pushState. A copied
  // link at any point in time reproduces the same search.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (debouncedQuery.trim()) params.set("q", debouncedQuery.trim());
    else params.delete("q");
    const qs = params.toString();
    window.history.replaceState(null, "", qs ? `${window.location.pathname}?${qs}` : window.location.pathname);
  }, [debouncedQuery]);

  useEffect(() => {
    if (!hasQuery) {
      setResponse(null);
      setLoading(false);
      setError(null);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    unifiedSearch(identity, { q: debouncedQuery.trim() || undefined, ...debouncedFilters }, controller.signal)
      .then((res) => {
        setResponse(res);
        setLoading(false);
      })
      .catch((e) => {
        if (e instanceof DOMException && e.name === "AbortError") return;
        setError(e instanceof ApiError ? e.message : "Unknown error");
        setLoading(false);
      });
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debouncedQuery, debouncedFilters, identity, retryToken]);

  function jumpToCard(id: string) {
    const el = document.getElementById(`person-card-${id}`);
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    setFlashId(id);
    window.setTimeout(() => setFlashId((cur) => (cur === id ? null : cur)), 1200);
  }

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
      </div>

      <main className="content">
        {mode === "search" ? (
          <>
            <Filters filters={filters} onChange={setFilters} />
            <UnifiedResults
              loading={loading}
              error={error}
              response={response}
              hasQuery={hasQuery}
              flashId={flashId}
              onSelect={(id) => setSelectedId(id)}
              onJumpToCard={jumpToCard}
              onExampleClick={(text) => setQuery(text)}
              onRetry={() => setRetryToken((t) => t + 1)}
            />
          </>
        ) : (
          <GraphPage
            identity={identity}
            focusId={graphFocusId}
            onFocusChange={setGraphFocusId}
            onOpenProfile={(id) => setSelectedId(id)}
          />
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
