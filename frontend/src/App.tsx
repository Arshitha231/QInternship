import { useEffect, useState } from "react";
import { TopBar } from "./components/TopBar";
import { Filters } from "./components/Filters";
import { UnifiedResults } from "./components/UnifiedResults";
import { ProfilePage, type ProfileStackEntry } from "./components/ProfilePage";
import { GraphPage } from "./components/GraphPage";
import { ContinuityPage } from "./components/ContinuityPage";
import { useDebouncedValue } from "./hooks";
import { ApiError, unifiedSearch, type SearchFilters } from "./api";
import { DEV_IDENTITIES } from "./identities";
import type { Identity, UnifiedSearchResponse } from "./types";

type Mode = "profile" | "graphs" | "continuity";

function initialQuery(): string {
  return new URLSearchParams(window.location.search).get("q") ?? "";
}

function profileIdFromPath(): string | null {
  const m = window.location.pathname.match(/^\/profile\/([^/]+)$/);
  return m ? decodeURIComponent(m[1]) : null;
}

function profileUrl(id: string): string {
  return `/profile/${encodeURIComponent(id)}${window.location.search}`;
}

export default function App() {
  const [identity, setIdentity] = useState<Identity>(DEV_IDENTITIES[0]);
  const [mode, setMode] = useState<Mode>("profile");
  const [query, setQuery] = useState(initialQuery);
  const [filters, setFilters] = useState<SearchFilters>({});
  const [profileStack, setProfileStack] = useState<ProfileStackEntry[]>(() => {
    const urlId = profileIdFromPath();
    return urlId ? [{ id: urlId, name: "" }] : [{ id: DEV_IDENTITIES[0].id, name: DEV_IDENTITIES[0].name }];
  });
  const profileId = profileStack[profileStack.length - 1].id;
  const [graphFocusId, setGraphFocusId] = useState<string>(DEV_IDENTITIES[0].id);
  const [flashId, setFlashId] = useState<string | null>(null);
  const [retryToken, setRetryToken] = useState(0);
  const [savedSearch, setSavedSearch] = useState<{ query: string; filters: SearchFilters } | null>(null);

  // Normalize the URL to reflect the initial profile on first mount, without
  // creating a spurious history entry.
  useEffect(() => {
    window.history.replaceState({ stack: profileStack }, "", profileUrl(profileStack[profileStack.length - 1].id));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Browser back/forward restores whichever stack was pushed at that point
  // in history; a bare URL with no app state (e.g. a fresh navigation from
  // outside) falls back to a single-entry stack for that id.
  useEffect(() => {
    function onPopState(e: PopStateEvent) {
      const state = e.state as { stack?: ProfileStackEntry[] } | null;
      if (state?.stack && state.stack.length > 0) {
        setProfileStack(state.stack);
        setMode("profile");
      } else {
        const urlId = profileIdFromPath();
        if (urlId) {
          setProfileStack([{ id: urlId, name: "" }]);
          setMode("profile");
        }
      }
    }
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  function goToStack(nextStack: ProfileStackEntry[]) {
    setProfileStack(nextStack);
    window.history.pushState({ stack: nextStack }, "", profileUrl(nextStack[nextStack.length - 1].id));
  }

  function pushProfile(id: string, name: string) {
    goToStack([...profileStack, { id, name }]);
  }

  function resetProfile(id: string, name: string) {
    goToStack([{ id, name }]);
  }

  function backOneProfile() {
    if (profileStack.length > 1) goToStack(profileStack.slice(0, -1));
  }

  function jumpToProfileIndex(index: number) {
    if (index < profileStack.length - 1) goToStack(profileStack.slice(0, index + 1));
  }

  function backToSearch() {
    if (!savedSearch) return;
    setQuery(savedSearch.query);
    setFilters(savedSearch.filters);
    setSavedSearch(null);
  }

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
        onQueryChange={setQuery}
        identity={identity}
        onIdentityChange={(next) => {
          setIdentity(next);
          setGraphFocusId(next.id);
          setSavedSearch(null);
          resetProfile(next.id, next.name);
          // The Continuity tab doesn't exist at all for non-hr identities
          // (see the tab bar below) -- if it was open when switching to
          // one, there'd be no tab left to click to get back out.
          if (next.role !== "hr" && mode === "continuity") setMode("profile");
        }}
        onOpenPerson={(id, name) => {
          resetProfile(id, name);
          setMode("profile");
          setQuery("");
        }}
      />

      <div className="tabs" role="tablist" aria-label="Section">
        <button
          role="tab"
          aria-selected={mode === "profile"}
          className={`tab ${mode === "profile" ? "active" : ""}`}
          onClick={() => {
            setMode("profile");
            setQuery("");
            setSavedSearch(null);
            resetProfile(identity.id, identity.name);
          }}
        >
          Profile
        </button>
        <button
          role="tab"
          aria-selected={mode === "graphs"}
          className={`tab ${mode === "graphs" ? "active" : ""}`}
          onClick={() => {
            setMode("graphs");
            setQuery("");
          }}
        >
          Graphs
        </button>
        {identity.role === "hr" && (
          <button
            role="tab"
            aria-selected={mode === "continuity"}
            className={`tab ${mode === "continuity" ? "active" : ""}`}
            onClick={() => {
              setMode("continuity");
              setQuery("");
            }}
          >
            Continuity
          </button>
        )}
      </div>

      <main className="content">
        {hasQuery ? (
          <>
            <Filters filters={filters} onChange={setFilters} />
            <UnifiedResults
              loading={loading}
              error={error}
              response={response}
              hasQuery={hasQuery}
              flashId={flashId}
              onSelect={(id, name) => {
                setSavedSearch({ query: debouncedQuery, filters: debouncedFilters });
                resetProfile(id, name);
                setMode("profile");
                setQuery("");
              }}
              onJumpToCard={jumpToCard}
              onExampleClick={(text) => setQuery(text)}
              onRetry={() => setRetryToken((t) => t + 1)}
            />
          </>
        ) : mode === "profile" ? (
          <ProfilePage
            personId={profileId}
            identity={identity}
            stack={profileStack}
            onNavigate={pushProfile}
            onBack={backOneProfile}
            onBreadcrumb={jumpToProfileIndex}
            onBackToSearch={savedSearch ? backToSearch : undefined}
          />
        ) : mode === "graphs" ? (
          <GraphPage
            identity={identity}
            focusId={graphFocusId}
            onFocusChange={setGraphFocusId}
            onOpenProfile={(id, name) => {
              resetProfile(id, name);
              setMode("profile");
            }}
          />
        ) : identity.role === "hr" ? (
          <ContinuityPage identity={identity} />
        ) : null}
      </main>
    </div>
  );
}
